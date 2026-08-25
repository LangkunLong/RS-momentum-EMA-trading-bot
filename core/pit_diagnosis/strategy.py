"""Fact-cache backed strategy and isolated exit variants for diagnosis only."""

from __future__ import annotations

import json
import math
from typing import Any, Mapping

import pandas as pd

from config import settings
from core.backtest_engine import CanslimStrategy, PortfolioSimulator

from .fact_cache import FactCache, SessionFact
from .models import ExperimentDefinition, RuleOutcome, Rulebook


def _number(value: object) -> float | None:
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def _ids(value: object) -> tuple[str, ...]:
    try:
        parsed = json.loads(str(value))
    except (TypeError, ValueError, json.JSONDecodeError):
        return ()
    return tuple(sorted(item for item in parsed if isinstance(item, str) and item)) if isinstance(parsed, list) else ()


def evaluate_session_rules(
    fact: SessionFact,
    rulebook: Rulebook,
    experiment: ExperimentDefinition,
    *,
    strict_canslim: bool = False,
) -> tuple[RuleOutcome, ...]:
    """Evaluate one immutable cache row; no history/provider access is permitted."""
    if type(strict_canslim) is not bool:
        raise ValueError("strict_canslim must be a bool")
    values = fact.values
    def passed(rule_id: str, condition: bool, *ids: str) -> RuleOutcome:
        return RuleOutcome.passed(rule_id, *ids) if condition else RuleOutcome.failed(rule_id, *ids)
    def unavailable(rule_id: str, ids: tuple[str, ...] = ()) -> RuleOutcome:
        return RuleOutcome.unavailable(rule_id, *ids)
    annual = [_number(values.get(f"annual_eps_{index}")) for index in range(1, 5)]
    annual_ok = all(value is not None and value > 0.0 for value in annual) and bool(annual[0] and annual[-1] and annual[0] / annual[-1] - 1.0 >= 0.25)
    base_ok = bool(values.get("base_kind")) and _number(values.get("pivot")) is not None
    extension = _number(values.get("extension_pct"))
    new_high_observed = base_ok and extension is not None
    new_high_outcome = (
        passed("N.NEW_HIGH", extension >= 0.0)
        if new_high_observed
        else unavailable("N.NEW_HIGH")
    )
    catalyst_outcome = unavailable("N.CATALYST")
    if catalyst_outcome.status == "passed" or new_high_outcome.status == "passed":
        newness_outcome = RuleOutcome.passed("N.NEWNESS")
    elif catalyst_outcome.status == "failed" and new_high_outcome.status == "failed":
        newness_outcome = RuleOutcome.failed("N.NEWNESS")
    else:
        newness_outcome = RuleOutcome.unavailable("N.NEWNESS")
    roe_floor = _number(rulebook.rules["A.ROE"].parameter_policy.get("minimum"))
    if roe_floor is None:
        raise ValueError("A.ROE rulebook minimum is required")
    market_uptrend = values.get("market_regime") == "uptrend"
    if experiment.experiment_id == "D3.M_BASELINE_OFF":
        market_uptrend = True
    group_ids = _ids(values.get("industry_evidence_ids"))
    institution_ids = _ids(values.get("institutional_evidence_ids"))
    if strict_canslim:
        industry_policy = rulebook.rules["L.INDUSTRY_GROUP"].parameter_policy
        industry_max_rank = _number(industry_policy.get("maximum_rank"))
        if industry_max_rank is None or not math.isfinite(industry_max_rank) or industry_max_rank <= 0.0:
            raise ValueError("L.INDUSTRY_GROUP maximum_rank is required for strict CANSLIM")
        institution_policy = rulebook.rules["I.SPONSORSHIP"].parameter_policy
        ownership_floor = _number(institution_policy.get("ownership_floor"))
        if ownership_floor is None or not math.isfinite(ownership_floor) or not 0.0 <= ownership_floor <= 1.0:
            raise ValueError("I.SPONSORSHIP ownership_floor is required for strict CANSLIM")
        require_increasing_holders = institution_policy.get("require_increasing_holders")
        if type(require_increasing_holders) is not bool:
            raise ValueError("I.SPONSORSHIP require_increasing_holders is required for strict CANSLIM")
        industry_rank = _number(values.get("industry_rank"))
        if industry_rank is None or not math.isfinite(industry_rank) or not group_ids:
            industry_outcome = unavailable("L.INDUSTRY_GROUP", group_ids)
        else:
            industry_outcome = passed(
                "L.INDUSTRY_GROUP",
                industry_rank.is_integer() and 0.0 < industry_rank <= industry_max_rank,
                *group_ids,
            )
        ownership = _number(values.get("institutional_ownership_percent"))
        holder_count = _number(values.get("institutional_holder_count"))
        previous_holder_count = _number(values.get("institutional_previous_holder_count"))
        complete_sponsorship = (
            bool(institution_ids)
            and ownership is not None
            and math.isfinite(ownership)
            and holder_count is not None
            and math.isfinite(holder_count)
            and previous_holder_count is not None
            and math.isfinite(previous_holder_count)
        )
        if not complete_sponsorship:
            institution_outcome = unavailable("I.SPONSORSHIP", institution_ids)
        else:
            institution_outcome = passed(
                "I.SPONSORSHIP",
                0.0 <= ownership <= 1.0
                and ownership >= ownership_floor
                and holder_count.is_integer()
                and previous_holder_count.is_integer()
                and holder_count >= 0.0
                and previous_holder_count >= 0.0
                and (not require_increasing_holders or holder_count > previous_holder_count),
                *institution_ids,
            )
    else:
        industry_outcome = passed(
            "L.INDUSTRY_GROUP",
            _number(values.get("industry_rank")) is not None,
            *group_ids,
        ) if group_ids else unavailable("L.INDUSTRY_GROUP")
        institution_outcome = passed(
            "I.SPONSORSHIP",
            _number(values.get("institutional_holder_count")) is not None
            and _number(values.get("institutional_previous_holder_count")) is not None,
            *institution_ids,
        ) if institution_ids else unavailable("I.SPONSORSHIP")
    rs_floor = 85.0 if experiment.experiment_id == "D2.RS_85_CONFORMANCE" else 80.0
    outcomes: dict[str, RuleOutcome] = {
        "C.EPS_YOY": passed("C.EPS_YOY", (_number(values.get("current_eps_yoy")) or -1.0) >= 0.25),
        "C.SALES_YOY": passed("C.SALES_YOY", (_number(values.get("sales_yoy")) or -1.0) >= 0.25),
        "C.ACCELERATION": unavailable("C.ACCELERATION"),
        "A.EPS_MULTIYEAR": passed("A.EPS_MULTIYEAR", annual_ok),
        "A.ROE": passed("A.ROE", (_number(values.get("roe")) or 0.0) >= roe_floor),
        "N.CATALYST": catalyst_outcome,
        "N.NEW_HIGH": new_high_outcome,
        "N.NEWNESS": newness_outcome,
        "S.VOLUME_CONFIRMATION": passed("S.VOLUME_CONFIRMATION", (_number(values.get("event_volume_ratio")) or 0.0) >= 1.30),
        "S.SUPPLY": unavailable("S.SUPPLY"),
        "L.RS": passed("L.RS", (_number(values.get("rs_rating")) or -1.0) >= rs_floor),
        "L.INDUSTRY_GROUP": industry_outcome,
        "I.SPONSORSHIP": institution_outcome,
        "M.CONFIRMED_UPTREND": passed("M.CONFIRMED_UPTREND", market_uptrend),
        "M.DISTRIBUTION_EXPOSURE": passed("M.DISTRIBUTION_EXPOSURE", (_number(values.get("distribution_count")) or 0.0) < 5.0),
        "E.PROPER_BASE": passed("E.PROPER_BASE", base_ok),
        "E.PIVOT": passed("E.PIVOT", base_ok),
        "E.BUY_ZONE": passed("E.BUY_ZONE", 0.0 <= (_number(values.get("extension_pct")) or -1.0) <= 0.05),
        "E.NEXT_OPEN": passed("E.NEXT_OPEN", (_number(values.get("open")) or 0.0) > 0.0),
        "X.LOSS_LIMIT": RuleOutcome.passed("X.LOSS_LIMIT"),
        "X.PROFIT_ZONE": RuleOutcome.passed("X.PROFIT_ZONE"),
        "X.EIGHT_WEEK_HOLD": RuleOutcome.passed("X.EIGHT_WEEK_HOLD"),
        "X.STRUCTURAL_SELL": RuleOutcome.passed("X.STRUCTURAL_SELL"),
    }
    return tuple(outcomes[rule_id] for rule_id in rulebook.rules)


class CachedDiagnosisStrategy(CanslimStrategy):
    """A strategy whose only data source is the read-only diagnosis fact cache."""

    def __init__(
        self,
        fact_cache: FactCache,
        rulebook: Rulebook,
        experiment: ExperimentDefinition,
        *,
        strict_canslim: bool = False,
    ) -> None:
        if type(strict_canslim) is not bool:
            raise ValueError("strict_canslim must be a bool")
        super().__init__(fundamental_provider=None, require_bullish_market=False)
        self.fact_cache, self.rulebook, self.experiment = fact_cache, rulebook, experiment
        self.strict_canslim = strict_canslim
        self.fundamental_provider = None

    def evaluate_symbol(self, *, ticker: str, ticker_ohlcv: dict[str, pd.DataFrame], all_closes: pd.DataFrame, eval_date: pd.Timestamp, market_state: dict, rs_score: float | None = None) -> dict | None:
        del ticker_ohlcv, all_closes, rs_score
        try:
            fact = self.fact_cache.session_fact(ticker, str(eval_date.date()))
        except KeyError:
            return None
        outcomes = {
            outcome.rule_id: outcome
            for outcome in evaluate_session_rules(
                fact, self.rulebook, self.experiment, strict_canslim=self.strict_canslim,
            )
        }
        required = [rule_id for rule_id, rule in self.rulebook.rules.items() if rule.classification.value == "required"]
        proxy_exclusions = frozenset() if self.strict_canslim else frozenset({"I.SPONSORSHIP", "L.INDUSTRY_GROUP"})
        observed_eligible = all(
            outcomes[rule_id].status == "passed"
            for rule_id in required
            if rule_id not in proxy_exclusions
        )
        market_ok = outcomes["M.CONFIRMED_UPTREND"].status == "passed"
        return {
            "symbol": str(fact.symbol), "signal_date": str(fact.session), "close": _number(fact.values.get("close")),
            "current_growth": _number(fact.values.get("current_eps_yoy")), "annual_growth": None,
            "rs_score": _number(fact.values.get("rs_rating")), "pivot": _number(fact.values.get("pivot")),
            "entry_composite_score": 100.0, "canslim_score": 100.0, "market_is_bullish": market_ok,
            "market_regime_is_bullish": market_ok, "buy_signal_without_market": observed_eligible,
            "has_breakout": outcomes["E.PROPER_BASE"].status == "passed", "has_volume_surge": outcomes["S.VOLUME_CONFIRMATION"].status == "passed",
            "in_buy_zone": outcomes["E.BUY_ZONE"].status == "passed", "technical_setup_eligible": observed_eligible,
            "entry_contract_eligible": observed_eligible, "buy_signal": observed_eligible and market_ok,
            "signal_reason": "PIT cached CANSLIM", "technical_only": False,
        }


class DiagnosisPortfolioSimulator(PortfolioSimulator):
    """Production exit behavior plus the predeclared diagnosis-only variants."""

    def __init__(self, *args: Any, experiment_id: str, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.experiment_id = experiment_id
        self.exit_decisions: list[Mapping[str, object]] = []

    def _record_exit(self, rule_id: str, symbol: str, eval_date: pd.Timestamp) -> None:
        self.exit_decisions.append({"rule_id": rule_id, "evidence_ids": (f"session:{eval_date.date()}",), "symbol": symbol})

    def _check_exits(self, symbol: str, ohlcv: pd.DataFrame, eval_date: pd.Timestamp, *_args: Any, **_kwargs: Any) -> None:
        if self.experiment_id == "D4.CURRENT_EXIT_PACKAGE":
            return super()._check_exits(symbol, ohlcv, eval_date)
        trade = self._open_positions.get(symbol)
        bar = ohlcv.loc[eval_date:eval_date]
        if trade is None or bar.empty:
            return
        low, high, close = float(bar["Low"].iloc[0]), float(bar["High"].iloc[0]), float(bar["Close"].iloc[0])
        trade.days_held += 1
        gain = close / trade.entry_price - 1.0
        if low <= trade.stop_price:
            self._record_exit("X.LOSS_LIMIT", symbol, eval_date)
            self._close_trade(symbol, trade.stop_price, "stop_loss", str(eval_date.date()))
            return
        if self.experiment_id == "D4.PROFIT_ZONE":
            if not trade.eight_week_hold and trade.days_held <= 15 and gain >= 0.20:
                trade.eight_week_hold = True
            if not trade.eight_week_hold and high >= trade.entry_price * 1.20:
                self._record_exit("X.PROFIT_ZONE", symbol, eval_date)
                self._close_trade(symbol, trade.entry_price * 1.20, "profit_zone", str(eval_date.date()))
                return
        if self.experiment_id == "D4.REMOVE_UNVERIFIED_EXITS":
            # Keep the production defensive package intact: only the uncited
            # stagnation time stop and 21-day EMA exit are removed.
            if not trade.eight_week_hold and (trade.remaining_qty or 0.0) > 0:
                while trade.scale_out_tier < len(settings.SCALE_OUT_TIERS):
                    gain_target, fraction = settings.SCALE_OUT_TIERS[trade.scale_out_tier]
                    tier_price = trade.entry_price * (1 + gain_target)
                    if high < tier_price:
                        break
                    sell_qty = trade.qty * fraction
                    if sell_qty > 0.0 and (trade.remaining_qty or 0.0) >= sell_qty:
                        self._scale_out_trade(symbol, tier_price, str(eval_date.date()), "take_profit_scale_out", sell_qty=sell_qty)
                    trade.scale_out_tier += 1
            self._update_protective_stop(trade, ohlcv.loc[:eval_date], high)
            return
        if self.experiment_id == "D4.EIGHT_WEEK_HOLD" and trade.days_held <= 15 and gain >= 0.20:
            trade.eight_week_hold = True
            self._record_exit("X.EIGHT_WEEK_HOLD", symbol, eval_date)
        if self.experiment_id == "D4.STRUCTURAL_SELL" and len(ohlcv.loc[:eval_date]) >= self.ma_exit_period:
            ema = ohlcv.loc[:eval_date, "Close"].ewm(span=self.ma_exit_period, adjust=False).mean().iloc[-1]
            if close < float(ema):
                self._record_exit("X.STRUCTURAL_SELL", symbol, eval_date)
                self._close_trade(symbol, close, "structural_sell", str(eval_date.date()))
