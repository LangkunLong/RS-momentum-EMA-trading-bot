"""Reconciled causal diagnostics for PIT optimizer V5 panel results."""

from __future__ import annotations

import hashlib
import math
import re
from collections import Counter, defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import fields
from decimal import Decimal, localcontext
from itertools import pairwise

import pandas as pd

from core.backtest_engine import (
    PortfolioObservationV5,
    PositionEpisodeV5,
    SimulationResultV5,
)
from core.backtest_fills import FrictionScenario, apply_friction
from core.pit_optimizer_evaluation import EvaluationPanelSpec

from .contracts import (
    DistributionSummaryV5,
    EvaluationReportV5,
    EvaluationSliceV5,
    MAX_ROLE_EVIDENCE_ITEMS_V5,
    MetricCountV5,
    RoleEvidenceItemV5,
    RoleEvidenceV5,
    RollingReturnV5,
    SliceMetricsV5,
)


_METRIC_QUANTUM = Decimal("0.000001")
_MONEY_QUANTUM = Decimal("0.01")
_FILL_COLUMNS = frozenset(
    {
        "Date",
        "Ticker",
        "Action",
        "Reason",
        "EpisodeId",
        "ReferencePrice",
        "ExecutionPrice",
        "Quantity",
        "GrossValue",
        "CommissionUSD",
        "SlippageUSD",
        "SpreadCostUSD",
        "MarketImpactCostUSD",
        "CashDelta",
        "TotalFrictionUSD",
        "StopReferenceKind",
        "StopPrice",
    }
)
_POLICY_OUTCOME_IDS = frozenset(
    {
        "queued",
        "executed_next_open",
        "delayed_missing_open_sessions",
        "preempted_by_stop",
        "unexecuted_terminal",
    }
)
_ENTRY_OUTCOME_IDS = frozenset(
    {
        "entries_executed",
        "entry_rejected_already_open",
        "entry_rejected_capacity",
        "entry_rejected_missing_data",
        "entry_rejected_invalid_price",
        "entry_rejected_next_open_buy_zone",
        "entry_rejected_invalid_risk",
        "entry_rejected_no_cash",
    }
)
_SIGNAL_FUNNEL_COLUMNS = frozenset(
    {
        "signal_date",
        "symbol",
        "rs_score",
        "market_is_bullish",
        "has_breakout",
        "has_volume_surge",
        "in_buy_zone",
        "has_peg_today",
        "technical_score",
        "buy_signal",
        "technical_blocking_reasons",
        "entry_blocking_reasons",
    }
)


def _d(value: object, label: str, *, positive: bool = False) -> Decimal:
    if isinstance(value, bool) or not isinstance(value, (int, float, Decimal)):
        raise ValueError(f"{label} must be finite numeric evidence")
    try:
        number = Decimal(str(value))
    except Exception as exc:
        raise ValueError(f"{label} must be finite numeric evidence") from exc
    if not number.is_finite() or (positive and number <= 0):
        raise ValueError(f"{label} must be finite numeric evidence")
    return number


def _q(value: Decimal) -> Decimal:
    if not value.is_finite():
        raise ValueError("diagnostic calculation is not finite")
    return value.quantize(_METRIC_QUANTUM)


def _money(value: Decimal) -> Decimal:
    if not value.is_finite():
        raise ValueError("diagnostic money calculation is not finite")
    return value.quantize(_MONEY_QUANTUM)


def _series(
    value: object,
    *,
    sessions: tuple[str, ...],
    label: str,
) -> tuple[Decimal, ...]:
    if type(value) is not pd.Series or len(value) != len(sessions):
        raise ValueError(f"{label} does not cover every panel session")
    try:
        index = tuple(pd.Timestamp(item).date().isoformat() for item in value.index)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label} session index is invalid") from exc
    if index != sessions or len(set(index)) != len(index):
        raise ValueError(f"{label} sessions differ from the authenticated panel")
    numbers = tuple(_d(item, label, positive=True) for item in value.tolist())
    return numbers


def _annualized_return(start: Decimal, end: Decimal, days: int) -> Decimal:
    if start <= 0 or end <= 0 or days <= 0:
        raise ValueError("annualized return evidence is invalid")
    with localcontext() as context:
        context.prec = 40
        return _q(((end / start) ** (Decimal(365) / Decimal(days)) - 1) * 100)


def _return_metrics(
    values: Sequence[Decimal],
    sessions: Sequence[str],
) -> tuple[Decimal, Decimal, Decimal, Decimal]:
    if len(values) != len(sessions) or len(values) < 2:
        raise ValueError("return metrics require aligned multi-session evidence")
    total = _q((values[-1] / values[0] - 1) * 100)
    elapsed = (pd.Timestamp(sessions[-1]) - pd.Timestamp(sessions[0])).days
    annualized = _annualized_return(values[0], values[-1], elapsed)
    peak = values[0]
    maximum_drawdown = Decimal(0)
    returns: list[Decimal] = []
    for previous, current in pairwise(values):
        peak = max(peak, current)
        maximum_drawdown = min(maximum_drawdown, (current / peak - 1) * 100)
        returns.append(current / previous - 1)
    if len(returns) < 2:
        sharpe = Decimal(0)
    else:
        mean = sum(returns, Decimal(0)) / Decimal(len(returns))
        variance = sum((item - mean) ** 2 for item in returns) / Decimal(
            len(returns) - 1
        )
        sharpe = (
            Decimal(0)
            if variance == 0
            else mean / variance.sqrt() * Decimal(252).sqrt()
        )
    return total, annualized, _q(maximum_drawdown), _q(sharpe)


def _validate_observations(
    result: SimulationResultV5,
    *,
    sessions: tuple[str, ...],
    equity: Sequence[Decimal],
) -> tuple[PortfolioObservationV5, ...]:
    observations = result.portfolio_observations
    if (
        type(observations) is not tuple
        or len(observations) != len(sessions)
        or any(type(item) is not PortfolioObservationV5 for item in observations)
        or tuple(item.session for item in observations) != sessions
    ):
        raise ValueError("V5 portfolio observations do not cover the panel")
    for expected, observation in zip(equity, observations, strict=True):
        cash = _d(observation.cash, "observation cash")
        gross = _d(observation.gross_long_notional, "observation exposure")
        total = _d(observation.total_equity, "observation equity", positive=True)
        if cash < 0 or gross < 0 or _q(cash + gross) != _q(total):
            raise ValueError("V5 cash and exposure do not reconcile to observation equity")
        if _q(total) != _q(expected) or not observation.oneil_regime:
            raise ValueError("V5 observation equity or regime evidence differs")
    terminal = observations[-1]
    if _q(_d(terminal.gross_long_notional, "terminal exposure")) != 0:
        raise ValueError("V5 result was not terminally liquidated")
    if _q(_d(terminal.cash, "terminal cash")) != _q(equity[-1]):
        raise ValueError("V5 terminal cash differs from ending equity")
    return observations


def _validate_fills(
    result: SimulationResultV5,
    scenario: FrictionScenario,
) -> tuple[pd.DataFrame, Decimal]:
    fills = result.fill_log
    if type(fills) is not pd.DataFrame:
        raise ValueError("V5 fill log is unavailable")
    totals = result.fill_cost_totals
    expected_total_fields = {
        "commission_usd", "spread_cost_usd", "market_impact_cost_usd",
        "total_friction_usd",
    }
    if not isinstance(totals, Mapping) or set(totals) != expected_total_fields:
        raise ValueError("V5 fill-cost totals are incomplete")
    decimal_totals = {
        key: _money(_d(value, f"fill total {key}")) for key, value in totals.items()
    }
    if any(value < 0 for value in decimal_totals.values()):
        raise ValueError("V5 fill-cost totals cannot be negative")
    config = result.config
    expected_scenario = {
        "scenario_id": scenario.scenario_id,
        "half_spread_bps": scenario.half_spread_bps,
        "market_impact_bps": scenario.market_impact_bps,
        "commission_bps": scenario.commission_bps,
    }
    if not isinstance(config, Mapping) or config.get("friction_scenario") != expected_scenario:
        raise ValueError("V5 result friction identity differs from the report request")
    if config.get("fill_cost_totals") != dict(totals):
        raise ValueError("V5 result config and fill-cost totals differ")
    if fills.empty:
        if any(decimal_totals.values()) or result.position_episodes:
            raise ValueError("empty V5 fill log disagrees with costs or episodes")
        return fills, Decimal(0)
    if not _FILL_COLUMNS.issubset(fills.columns):
        raise ValueError("V5 fill log schema is incomplete")

    sums = defaultdict(Decimal)
    for row in fills.to_dict("records"):
        action = row["Action"]
        if action not in {"BUY", "SELL"}:
            raise ValueError("V5 fill action is invalid")
        reference = _d(row["ReferencePrice"], "fill reference", positive=True)
        quantity = _d(row["Quantity"], "fill quantity", positive=True)
        expected = apply_friction(
            side=action,
            reference_price=float(reference),
            quantity=float(quantity),
            scenario=scenario,
        )
        checks = {
            "ExecutionPrice": expected.execution_price,
            "GrossValue": expected.gross_value,
            "CommissionUSD": expected.commission_usd,
            "SpreadCostUSD": expected.spread_cost_usd,
            "MarketImpactCostUSD": expected.market_impact_cost_usd,
            "CashDelta": expected.cash_delta,
            "TotalFrictionUSD": round(
                expected.commission_usd
                + expected.spread_cost_usd
                + expected.market_impact_cost_usd,
                2,
            ),
        }
        for name, expected_value in checks.items():
            actual = _d(row[name], f"fill {name}")
            quantum = _MONEY_QUANTUM if name != "ExecutionPrice" else Decimal("0.000000000001")
            if actual.quantize(quantum) != _d(expected_value, name).quantize(quantum):
                raise ValueError(f"V5 fill {name} differs from friction inputs")
        slippage = _money(
            _d(row["SpreadCostUSD"], "spread cost")
            + _d(row["MarketImpactCostUSD"], "impact cost")
        )
        if slippage != _money(_d(row["SlippageUSD"], "slippage cost")):
            raise ValueError("V5 fill slippage components do not reconcile")
        sums["commission_usd"] += _d(row["CommissionUSD"], "commission")
        sums["spread_cost_usd"] += _d(row["SpreadCostUSD"], "spread")
        sums["market_impact_cost_usd"] += _d(
            row["MarketImpactCostUSD"], "impact"
        )
        sums["total_friction_usd"] += _d(
            row["TotalFrictionUSD"], "total friction"
        )
    if any(_money(sums[key]) != decimal_totals[key] for key in expected_total_fields):
        raise ValueError("V5 fill rows do not reconcile to fill-cost totals")
    return fills, decimal_totals["total_friction_usd"]


def _validate_episodes(
    result: SimulationResultV5,
    fills: pd.DataFrame,
    sessions: tuple[str, ...],
) -> tuple[PositionEpisodeV5, ...]:
    episodes = result.position_episodes
    if type(episodes) is not tuple or any(
        type(item) is not PositionEpisodeV5 for item in episodes
    ):
        raise ValueError("V5 position episodes are invalid")
    if len(episodes) != len(result.closed_trades) or result.open_trades:
        raise ValueError("V5 position episodes do not match terminal trade evidence")
    ids = tuple(item.episode_id for item in episodes)
    if len(set(ids)) != len(ids):
        raise ValueError("V5 position episode IDs are not unique")
    if fills.empty:
        if episodes:
            raise ValueError("V5 episodes exist without fills")
        if _money(_d(result.initial_capital, "initial capital")) != _money(
            _d(result.equity_curve.iloc[-1], "ending equity")
        ):
            raise ValueError("V5 no-fill result changed terminal cash")
        return episodes
    session_set = set(sessions)
    session_positions = {session: index for index, session in enumerate(sessions)}
    if any(str(item) not in session_set for item in fills["Date"]):
        raise ValueError("V5 fill session falls outside the authenticated panel")
    if fills["EpisodeId"].isna().any() or set(fills["EpisodeId"]) != set(ids):
        raise ValueError("V5 fills do not bind exactly to position episodes")
    for episode, trade in zip(episodes, result.closed_trades, strict=True):
        if (
            episode.entry_session not in session_set
            or episode.exit_session not in session_set
            or episode.entry_session > episode.exit_session
            or episode.exit_symbol != trade.symbol
            or episode.entry_session != trade.entry_date
            or episode.exit_session != trade.exit_date
            or episode.exit_reason != trade.exit_reason
            or not math.isclose(
                episode.entry_price, trade.entry_price, rel_tol=0.0, abs_tol=1e-10
            )
            or not math.isclose(
                episode.exit_price, float(trade.exit_price), rel_tol=0.0, abs_tol=1e-10
            )
        ):
            raise ValueError("V5 episode identity differs from closed trade evidence")
        episode_fills = fills[fills["EpisodeId"] == episode.episode_id]
        _validate_episode_quantity_path(
            episode=episode,
            episode_fills=episode_fills,
            session_positions=session_positions,
        )
        buys = episode_fills[episode_fills["Action"] == "BUY"]
        sells = episode_fills[episode_fills["Action"] == "SELL"]
        if len(buys) != episode.add_on_count + 1 or sells.empty:
            raise ValueError("V5 episode buy/sell lifecycle is incomplete")
        bought = sum(_d(item, "episode buy quantity") for item in buys["Quantity"])
        sold = sum(_d(item, "episode sell quantity") for item in sells["Quantity"])
        if _q(bought) != _q(sold):
            raise ValueError("V5 episode quantities do not reconcile")
        buy_cash = -sum(_d(item, "episode buy cash") for item in buys["CashDelta"])
        net_cash = sum(
            (_d(item, "episode cash") for item in episode_fills["CashDelta"]),
            Decimal(0),
        )
        expected_return = _q(net_cash / buy_cash * 100)
        if expected_return != _q(_d(episode.realized_return_pct, "episode return")):
            raise ValueError("V5 episode return does not reconcile to fill cash")
        if (
            not episode.quantity_path
            or episode.quantity_path[0].action != "entry"
            or episode.quantity_path[-1].quantity_after > 1e-9
            or episode.holding_sessions < 0
            or sum(item.action == "add_on" for item in episode.quantity_path)
            != episode.add_on_count
            or any(
                item.session not in session_set
                for item in episode.quantity_path
            )
        ):
            raise ValueError("V5 episode quantity or holding evidence is incomplete")
    initial = _money(_d(result.initial_capital, "initial capital", positive=True))
    ending = _money(_d(result.equity_curve.iloc[-1], "ending equity", positive=True))
    cash_change = _money(
        sum((_d(item, "fill cash") for item in fills["CashDelta"]), Decimal(0))
    )
    if _money(initial + cash_change) != ending:
        raise ValueError("V5 fill cash does not reconcile initial and ending equity")
    return episodes


def _validate_episode_quantity_path(
    *,
    episode: PositionEpisodeV5,
    episode_fills: pd.DataFrame,
    session_positions: Mapping[str, int],
) -> None:
    """Require one immutable quantity transition for every ordered episode fill."""

    rows = episode_fills.to_dict("records")
    path = episode.quantity_path
    if len(rows) != len(path) or not rows:
        raise ValueError("V5 episode quantity path does not match its fill count")
    duplicate_keys: set[tuple[object, ...]] = set()
    previous_session_position = -1
    cumulative_quantity = Decimal(0)
    add_on_count = 0
    for index, (row, change) in enumerate(zip(rows, path, strict=True)):
        session = str(row["Date"])
        session_position = session_positions.get(session)
        if session_position is None or session_position < previous_session_position:
            raise ValueError("V5 episode fills are not in causal session order")
        previous_session_position = session_position
        duplicate_key = (
            session,
            str(row["Ticker"]),
            str(row["Action"]),
            str(row["Reason"]),
            str(row["ReferencePrice"]),
            str(row["ExecutionPrice"]),
            str(row["Quantity"]),
            str(row["CashDelta"]),
        )
        if duplicate_key in duplicate_keys:
            raise ValueError("V5 episode contains a duplicate fill transition")
        duplicate_keys.add(duplicate_key)

        action = str(row["Action"])
        quantity = _d(row["Quantity"], "episode transition quantity", positive=True)
        if action == "BUY":
            expected_action = "entry" if index == 0 else "add_on"
            cumulative_quantity += quantity
            if expected_action == "add_on":
                add_on_count += 1
        elif action == "SELL":
            if index == 0 or quantity > cumulative_quantity:
                raise ValueError("V5 episode sell exceeds its causal open quantity")
            cumulative_quantity -= quantity
            expected_action = "exit" if _q(cumulative_quantity) == 0 else "scale_out"
        else:
            raise ValueError("V5 episode fill action is invalid")

        if (
            change.action != expected_action
            or change.session != session
            or change.reason != str(row["Reason"])
            or not math.isclose(
                change.execution_price,
                float(row["ExecutionPrice"]),
                rel_tol=0.0,
                abs_tol=1e-10,
            )
            or _q(_d(change.quantity_after, "episode cumulative quantity"))
            != _q(cumulative_quantity)
        ):
            raise ValueError("V5 episode quantity transition differs from fill evidence")

    first = rows[0]
    last = rows[-1]
    if (
        str(first["Action"]) != "BUY"
        or str(last["Action"]) != "SELL"
        or _q(cumulative_quantity) != 0
        or add_on_count != episode.add_on_count
        or episode.entry_session != str(first["Date"])
        or episode.exit_session != str(last["Date"])
        or episode.entry_symbol != str(first["Ticker"])
        or episode.exit_symbol != str(last["Ticker"])
        or episode.exit_reason != str(last["Reason"])
        or not math.isclose(
            episode.entry_price,
            float(first["ExecutionPrice"]),
            rel_tol=0.0,
            abs_tol=1e-10,
        )
        or not math.isclose(
            episode.exit_price,
            float(last["ExecutionPrice"]),
            rel_tol=0.0,
            abs_tol=1e-10,
        )
    ):
        raise ValueError("V5 episode endpoints do not reconcile to ordered fills")


def _distribution(values: Iterable[Decimal]) -> DistributionSummaryV5 | None:
    ordered = tuple(sorted(values))
    if not ordered:
        return None

    def percentile(numerator: int) -> Decimal:
        if len(ordered) == 1:
            return _q(ordered[0])
        position = Decimal(len(ordered) - 1) * Decimal(numerator) / Decimal(4)
        lower = int(position)
        upper = min(lower + 1, len(ordered) - 1)
        fraction = position - lower
        return _q(ordered[lower] + (ordered[upper] - ordered[lower]) * fraction)

    return DistributionSummaryV5(
        minimum=_q(ordered[0]),
        p25=percentile(1),
        median=percentile(2),
        p75=percentile(3),
        maximum=_q(ordered[-1]),
    )


def _metric_counts(counts: Mapping[str, int]) -> tuple[MetricCountV5, ...]:
    return tuple(
        MetricCountV5(metric_id=key, count=value)
        for key, value in sorted(counts.items())
    )


def _entry_funnel(
    result: SimulationResultV5,
    panel: EvaluationPanelSpec,
    observations: Sequence[PortfolioObservationV5],
) -> tuple[MetricCountV5, ...]:
    outcome_counts = Counter(item.outcome for item in result.entry_outcomes)
    if any(key not in _ENTRY_OUTCOME_IDS for key in outcome_counts):
        raise ValueError("V5 entry outcome evidence contains an unknown outcome")
    diagnostics = result.execution_diagnostics
    if not isinstance(diagnostics, Mapping) or any(
        type(diagnostics.get(key)) is not int
        or int(diagnostics[key]) != outcome_counts[key]
        for key in _ENTRY_OUTCOME_IDS
    ):
        raise ValueError("V5 entry outcomes do not reconcile to engine diagnostics")
    if outcome_counts["entries_executed"] != len(result.position_episodes):
        raise ValueError("V5 executed entries do not reconcile to position episodes")
    counts = {f"stage.{key}": int(value) for key, value in result.signal_funnel.items()}
    counts.update(
        {
            f"outcome.{key}": int(value)
            for key, value in outcome_counts.items()
        }
    )
    counts[f"episode.{panel.purpose}.evaluated_rows"] = len(result.signal_log)
    counts[f"episode.{panel.purpose}.entries_executed"] = sum(
        item.outcome == "entries_executed" for item in result.entry_outcomes
    )
    if not result.signal_log.empty:
        if not _SIGNAL_FUNNEL_COLUMNS.issubset(result.signal_log.columns):
            raise ValueError("V5 signal evidence is incomplete for entry attribution")
        regime_by_session = {item.session: item.oneil_regime for item in observations}
        affiliations_by_symbol = {
            ticker: lineage.source_affiliations
            for lineage in panel.lineages
            for ticker in lineage.executable_tickers
        }
        for row in result.signal_log.to_dict("records"):
            session = str(row["signal_date"])
            symbol = str(row["symbol"])
            if session not in regime_by_session or symbol not in affiliations_by_symbol:
                raise ValueError("V5 signal evidence differs from panel or regime sessions")
            regime = regime_by_session[session]
            counts[f"regime.{regime}.evaluated_rows"] = (
                counts.get(f"regime.{regime}.evaluated_rows", 0) + 1
            )
            if bool(row["buy_signal"]):
                counts[f"regime.{regime}.buy_signal_count"] = (
                    counts.get(f"regime.{regime}.buy_signal_count", 0) + 1
                )
            for affiliation in affiliations_by_symbol[symbol]:
                key = f"source_universe.{affiliation}.evaluated_rows"
                counts[key] = counts.get(key, 0) + 1
                if bool(row["buy_signal"]):
                    buy_key = f"source_universe.{affiliation}.buy_signal_count"
                    counts[buy_key] = counts.get(buy_key, 0) + 1
    return _metric_counts(counts)


def _compounded_slice_metrics(
    *,
    indexes: Sequence[int],
    equity: Sequence[Decimal],
    observations: Sequence[PortfolioObservationV5],
    closed_trades: int,
) -> SliceMetricsV5:
    returns = [equity[index] / equity[index - 1] for index in indexes if index > 0]
    total_factor = math.prod(returns) if returns else Decimal(1)
    total = _q((total_factor - 1) * 100)
    annualized = (
        Decimal(0)
        if not returns or total_factor <= 0
        else _q((total_factor ** (Decimal(252) / Decimal(len(returns))) - 1) * 100)
    )
    synthetic = [Decimal(1)]
    for item in returns:
        synthetic.append(synthetic[-1] * item)
    peak = synthetic[0]
    drawdown = Decimal(0)
    for item in synthetic:
        peak = max(peak, item)
        drawdown = min(drawdown, (item / peak - 1) * 100)
    exposure = (
        Decimal(0)
        if not indexes
        else sum(
            _d(observations[index].gross_long_notional, "slice exposure")
            / _d(observations[index].total_equity, "slice equity", positive=True)
            for index in indexes
        )
        / Decimal(len(indexes))
        * 100
    )
    return SliceMetricsV5(
        annualized_return_pct=_q(annualized),
        total_return_pct=total,
        max_drawdown_pct=_q(drawdown),
        closed_trades=closed_trades,
        average_exposure_pct=_q(exposure),
    )


def _slices(
    *,
    panel: EvaluationPanelSpec,
    equity: Sequence[Decimal],
    observations: Sequence[PortfolioObservationV5],
    episodes: Sequence[PositionEpisodeV5],
    whole_metrics: tuple[Decimal, Decimal, Decimal, Decimal],
    average_exposure: Decimal,
) -> tuple[
    tuple[EvaluationSliceV5, ...],
    tuple[EvaluationSliceV5, ...],
    tuple[EvaluationSliceV5, ...],
]:
    regime_indexes: dict[str, list[int]] = defaultdict(list)
    year_indexes: dict[str, list[int]] = defaultdict(list)
    for index, observation in enumerate(observations):
        regime_indexes[observation.oneil_regime].append(index)
        year_indexes[observation.session[:4]].append(index)
    regime_by_session = {item.session: item.oneil_regime for item in observations}
    regime_closed = Counter(regime_by_session[item.exit_session] for item in episodes)
    year_closed = Counter(item.exit_session[:4] for item in episodes)
    regime_slices = tuple(
        EvaluationSliceV5(
            dimension="regime",
            label=label,
            metrics=_compounded_slice_metrics(
                indexes=indexes,
                equity=equity,
                observations=observations,
                closed_trades=regime_closed[label],
            ),
        )
        for label, indexes in sorted(regime_indexes.items())
    )
    total, annualized, drawdown, _ = whole_metrics
    episode_slices = (
        EvaluationSliceV5(
            dimension="episode",
            label=panel.purpose,
            metrics=SliceMetricsV5(
                annualized_return_pct=annualized,
                total_return_pct=total,
                max_drawdown_pct=drawdown,
                closed_trades=len(episodes),
                average_exposure_pct=average_exposure,
            ),
        ),
    )
    year_slices = tuple(
        EvaluationSliceV5(
            dimension="calendar_year",
            label=label,
            metrics=_compounded_slice_metrics(
                indexes=indexes,
                equity=equity,
                observations=observations,
                closed_trades=year_closed[label],
            ),
        )
        for label, indexes in sorted(year_indexes.items())
    )
    return regime_slices, episode_slices, year_slices


def _rolling_returns(
    equity: Sequence[Decimal], sessions: tuple[str, ...]
) -> tuple[RollingReturnV5, ...]:
    dates = pd.DatetimeIndex(pd.to_datetime(sessions))
    month_ends = [
        index
        for index in range(len(dates))
        if index == len(dates) - 1
        or (dates[index].year, dates[index].month)
        != (dates[index + 1].year, dates[index + 1].month)
    ]
    rows: list[RollingReturnV5] = []
    for months in (12, 24, 36):
        for end_index in month_ends:
            cutoff = dates[end_index] - pd.DateOffset(months=months)
            if cutoff < dates[0]:
                continue
            start_index = int(dates.searchsorted(cutoff, side="right")) - 1
            if start_index < 0 or start_index >= end_index:
                continue
            elapsed = (dates[end_index] - dates[start_index]).days
            total = _q((equity[end_index] / equity[start_index] - 1) * 100)
            rows.append(
                RollingReturnV5(
                    window_months=months,  # type: ignore[arg-type]
                    end_date=dates[end_index].date().isoformat(),
                    total_return_pct=total,
                    annualized_return_pct=_annualized_return(
                        equity[start_index], equity[end_index], elapsed
                    ),
                )
            )
    return tuple(rows)


def summarize_panel_result(
    *,
    panel: EvaluationPanelSpec,
    scenario: FrictionScenario,
    result: SimulationResultV5,
) -> EvaluationReportV5:
    """Build one complete V5 report from reconciled engine and panel evidence."""

    if type(panel) is not EvaluationPanelSpec:
        raise ValueError("diagnostics require an authenticated evaluation panel")
    if type(scenario) is not FrictionScenario:
        raise ValueError("diagnostics require a friction scenario")
    if type(result) is not SimulationResultV5:
        raise ValueError("diagnostics require SimulationResultV5 evidence")
    equity = _series(result.equity_curve, sessions=panel.sessions, label="equity curve")
    benchmark = _series(
        result.benchmark_curve, sessions=panel.sessions, label="benchmark curve"
    )
    observations = _validate_observations(
        result, sessions=panel.sessions, equity=equity
    )
    fills, total_friction = _validate_fills(result, scenario)
    episodes = _validate_episodes(result, fills, panel.sessions)
    policy_outcomes = result.policy_intent_outcomes
    if (
        not isinstance(policy_outcomes, Mapping)
        or set(policy_outcomes) != _POLICY_OUTCOME_IDS
        or any(type(value) is not int or value < 0 for value in policy_outcomes.values())
    ):
        raise ValueError("V5 policy-intent outcomes are incomplete")
    if policy_outcomes["queued"] != (
        policy_outcomes["executed_next_open"]
        + policy_outcomes["preempted_by_stop"]
        + policy_outcomes["unexecuted_terminal"]
    ):
        raise ValueError("V5 policy intents do not reconcile to terminal outcomes")

    net_metrics = _return_metrics(equity, panel.sessions)
    benchmark_metrics = _return_metrics(benchmark, panel.sessions)
    costs_by_session: dict[str, Decimal] = defaultdict(Decimal)
    if not fills.empty:
        for row in fills.to_dict("records"):
            costs_by_session[str(row["Date"])] += _d(
                row["TotalFrictionUSD"], "dated friction"
            )
    cumulative = Decimal(0)
    gross_curve: list[Decimal] = []
    for session, value in zip(panel.sessions, equity, strict=True):
        cumulative += costs_by_session[session]
        gross_curve.append(value + cumulative)
    gross_metrics = _return_metrics(gross_curve, panel.sessions)
    friction_drag = _q(gross_metrics[0] - net_metrics[0])

    exposure_fractions = [
        _d(item.gross_long_notional, "gross exposure")
        / _d(item.total_equity, "observation equity", positive=True)
        for item in observations
    ]
    cash_fractions = [
        _d(item.cash, "cash")
        / _d(item.total_equity, "observation equity", positive=True)
        for item in observations
    ]
    average_exposure = _q(
        sum(exposure_fractions, Decimal(0)) / Decimal(len(exposure_fractions)) * 100
    )
    average_cash = _q(
        sum(cash_fractions, Decimal(0)) / Decimal(len(cash_fractions)) * 100
    )
    average_equity = sum(equity, Decimal(0)) / Decimal(len(equity))
    turnover = (
        Decimal(0)
        if fills.empty
        else sum((_d(item, "fill gross value") for item in fills["GrossValue"]), Decimal(0))
        / average_equity
        * 100
    )

    realized_returns = [
        _d(item.realized_return_pct, "episode realized return") for item in episodes
    ]
    wins = [item for item in realized_returns if item > 0]
    losses = [item for item in realized_returns if item <= 0]
    average_win = None if not wins else _q(sum(wins) / Decimal(len(wins)))
    average_loss = None if not losses else _q(sum(losses) / Decimal(len(losses)))
    win_rate = (
        None if not episodes else _q(Decimal(len(wins)) / Decimal(len(episodes)) * 100)
    )
    payoff = (
        None
        if average_win is None or average_loss is None or average_loss == 0
        else _q(average_win / abs(average_loss))
    )
    expectancy = (
        None
        if not realized_returns
        else _q(sum(realized_returns) / Decimal(len(realized_returns)))
    )
    median_holding = (
        None
        if not episodes
        else _distribution(_d(item.holding_sessions, "holding sessions") for item in episodes).median
    )

    mfe_values = [
        max(
            (
                _d(item.maximum_completed_bar_price, "maximum mark", positive=True)
                / _d(item.entry_price, "episode entry", positive=True)
                - 1
            )
            * 100,
            Decimal(0),
        )
        for item in episodes
        if item.maximum_completed_bar_price is not None
    ]
    mae_values = [
        min(
            (
                _d(item.minimum_completed_bar_price, "minimum mark", positive=True)
                / _d(item.entry_price, "episode entry", positive=True)
                - 1
            )
            * 100,
            Decimal(0),
        )
        for item in episodes
        if item.minimum_completed_bar_price is not None
    ]

    average_exposure_fraction = average_exposure / 100
    invested_sleeve = (
        None
        if average_exposure_fraction == 0
        else _q(net_metrics[1] / average_exposure_fraction)
    )
    idle_cash_drag = (
        Decimal(0)
        if invested_sleeve is None
        else _q(invested_sleeve - net_metrics[1])
    )

    stop_gap_shortfall = Decimal(0)
    if not fills.empty:
        for row in fills.to_dict("records"):
            kind = row["StopReferenceKind"]
            stop = row["StopPrice"]
            kind_missing = kind is None or bool(pd.isna(kind))
            stop_missing = stop is None or bool(pd.isna(stop))
            if kind_missing:
                if not stop_missing:
                    raise ValueError("V5 non-stop fill unexpectedly carries a stop price")
                continue
            if kind not in {"gap_stop", "intraday_stop"} or stop_missing:
                raise ValueError("V5 stop fill evidence is incomplete")
            if kind == "gap_stop":
                shortfall = (
                    _d(stop, "gap stop price", positive=True)
                    - _d(row["ReferencePrice"], "gap fill reference", positive=True)
                ) * _d(row["Quantity"], "gap fill quantity", positive=True)
                if shortfall < 0:
                    raise ValueError("V5 gap-stop shortfall cannot be negative")
                stop_gap_shortfall += shortfall

    transaction_log = result.transaction_log
    if type(transaction_log) is not pd.DataFrame:
        raise ValueError("V5 transaction log is unavailable")
    transition_count = (
        0
        if transaction_log.empty
        else int((transaction_log["Action"] == "TRANSFER").sum())
        if "Action" in transaction_log.columns
        else (_ for _ in ()).throw(ValueError("V5 transaction action evidence is absent"))
    )

    opportunity_numerator = Decimal(0)
    opportunity_denominator = Decimal(0)
    for episode in episodes:
        maximum = episode.maximum_completed_bar_price
        if maximum is None:
            continue
        prior_quantity = _d(
            episode.quantity_path[0].quantity_after, "initial episode quantity"
        )
        for change in episode.quantity_path[1:]:
            after = _d(change.quantity_after, "quantity after change")
            sold = prior_quantity - after
            if change.action == "scale_out":
                if sold <= 0:
                    raise ValueError("V5 scale-out quantity path is invalid")
                price = _d(change.execution_price, "scale-out price", positive=True)
                opportunity_numerator += max(
                    _d(maximum, "maximum completed mark") - price, Decimal(0)
                ) * sold
                opportunity_denominator += price * sold
            prior_quantity = after
    scale_out_opportunity = (
        None
        if opportunity_denominator == 0
        else _q(opportunity_numerator / opportunity_denominator * 100)
    )

    regime_slices, episode_slices, calendar_slices = _slices(
        panel=panel,
        equity=equity,
        observations=observations,
        episodes=episodes,
        whole_metrics=net_metrics,
        average_exposure=average_exposure,
    )
    return EvaluationReportV5(
        portfolio_annualized_return_pct=net_metrics[1],
        portfolio_total_return_pct=net_metrics[0],
        gross_annualized_return_pct=gross_metrics[1],
        benchmark_annualized_return_pct=benchmark_metrics[1],
        benchmark_total_return_pct=benchmark_metrics[0],
        max_drawdown_pct=net_metrics[2],
        sharpe_ratio=net_metrics[3],
        closed_trades=len(episodes),
        average_exposure_pct=average_exposure,
        average_cash_pct=average_cash,
        turnover_pct=_q(turnover),
        total_friction_usd=_money(total_friction),
        friction_drag_pct=friction_drag,
        win_rate_pct=win_rate,
        average_win_pct=average_win,
        average_loss_pct=average_loss,
        payoff_ratio=payoff,
        expectancy_pct=expectancy,
        median_holding_sessions=median_holding,
        invested_sleeve_annualized_return_pct=invested_sleeve,
        estimated_idle_cash_drag_pct=idle_cash_drag,
        stop_gap_shortfall_usd=_money(stop_gap_shortfall),
        identity_transition_count=transition_count,
        scale_out_opportunity_cost_pct=scale_out_opportunity,
        maximum_favorable_excursion_pct=_distribution(mfe_values),
        maximum_adverse_excursion_pct=_distribution(mae_values),
        entry_funnel=_entry_funnel(result, panel, observations),
        exit_attribution=_metric_counts(Counter(item.exit_reason for item in episodes)),
        policy_intent_outcomes=_metric_counts(
            {str(key): int(value) for key, value in policy_outcomes.items()}
        ),
        regime_slices=regime_slices,
        episode_slices=episode_slices,
        calendar_year_slices=calendar_slices,
        rolling_returns=_rolling_returns(equity, panel.sessions),
    )


def _evidence_id(metric_id: str) -> str:
    fragment = re.sub(r"[^a-z0-9_.-]+", "-", metric_id.lower()).strip("-.")
    if not fragment:
        fragment = hashlib.sha256(metric_id.encode("utf-8")).hexdigest()[:16]
    if len(fragment) > 110:
        suffix = hashlib.sha256(metric_id.encode("utf-8")).hexdigest()[:12]
        fragment = f"{fragment[:97]}.{suffix}"
    return f"v5.{fragment}"


def to_role_evidence(report: EvaluationReportV5) -> RoleEvidenceV5:
    """Project a report into stable, bounded, symbol-neutral aggregate evidence."""

    if type(report) is not EvaluationReportV5:
        raise ValueError("role evidence requires an EvaluationReportV5")
    candidates: list[tuple[str, Decimal | int | None]] = []
    excluded = {
        "maximum_favorable_excursion_pct",
        "maximum_adverse_excursion_pct",
        "entry_funnel",
        "exit_attribution",
        "policy_intent_outcomes",
        "regime_slices",
        "episode_slices",
        "calendar_year_slices",
        "rolling_returns",
    }
    for item in fields(report):
        if item.name not in excluded:
            candidates.append((f"report.{item.name}", getattr(report, item.name)))
    for prefix, distribution in (
        ("mfe", report.maximum_favorable_excursion_pct),
        ("mae", report.maximum_adverse_excursion_pct),
    ):
        if distribution is None:
            candidates.append((f"distribution.{prefix}.undefined", None))
        else:
            for item in fields(distribution):
                candidates.append(
                    (f"distribution.{prefix}.{item.name}", getattr(distribution, item.name))
                )
    for category, metrics in (
        ("exit", report.exit_attribution),
        ("policy", report.policy_intent_outcomes),
    ):
        for metric in metrics:
            candidates.append((f"{category}.{metric.metric_id}", metric.count))
    for dimension, slices in (
        ("regime", report.regime_slices),
        ("episode", report.episode_slices),
        ("calendar_year", report.calendar_year_slices),
    ):
        for slice_item in slices:
            for metric in fields(slice_item.metrics):
                candidates.append(
                    (
                        f"slice.{dimension}.{slice_item.label}.{metric.name}",
                        getattr(slice_item.metrics, metric.name),
                    )
                )
    latest_rolling: dict[int, RollingReturnV5] = {}
    for row in report.rolling_returns:
        latest_rolling[row.window_months] = row
    for months, row in sorted(latest_rolling.items()):
        candidates.extend(
            (
                (f"rolling.{months}m.latest.total_return_pct", row.total_return_pct),
                (
                    f"rolling.{months}m.latest.annualized_return_pct",
                    row.annualized_return_pct,
                ),
            )
        )
    for metric in report.entry_funnel:
        candidates.append((f"entry.{metric.metric_id}", metric.count))

    items: list[RoleEvidenceItemV5] = []
    seen: set[str] = set()
    for metric_id, value in candidates:
        evidence_id = _evidence_id(metric_id)
        if evidence_id in seen:
            raise ValueError("role evidence projection produced a duplicate stable ID")
        seen.add(evidence_id)
        candidate = RoleEvidenceItemV5(evidence_id, metric_id, value)
        if len(items) == MAX_ROLE_EVIDENCE_ITEMS_V5:
            break
        tentative = RoleEvidenceV5(schema_version=5, items=tuple([*items, candidate]))
        items = list(tentative.items)
    return RoleEvidenceV5(schema_version=5, items=tuple(items))


__all__ = ["summarize_panel_result", "to_role_evidence"]
