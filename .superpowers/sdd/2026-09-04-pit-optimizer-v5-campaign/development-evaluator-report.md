# Development evaluator implementation report

Implemented in the existing `pit-optimizer-v5-architecture` worktree. Ready for
the controller's source-bound image rebuild and real container execution.

## Changed files

- `core/pit_optimizer_v5/container_protocol.py`: canonical
  `pit_data_scope="production" | "development_sp500_v2"` request field, default
  production; development rejects baseline-capture authority and qualification /
  held-out panels. Existing complete input SHA binding carries scope into output.
- `core/pit_optimizer_v5/container_entry.py`: passes the request scope to the
  existing evaluator and emits a bounded, sanitized stderr failure diagnostic
  containing integration stage, source filename/line, exception class/message;
  failure remains exit code 3.
- `core/pit_optimizer_v5/evaluator.py`: validates production schema 3 versus
  explicit development schema 2, rejects development baseline authority and
  held-out evaluation, passes scope to the existing simulator, and verifies result
  configuration scope.
- `core/backtest_engine.py`: accepts explicit development schema-V2 execution,
  forwards helper compatibility arguments and the authenticated transition
  contract, records scope in result configuration, preserves existing simulator,
  worker, fill and report paths.
- `core/engine_policy.py`: tiny fixed causal-identity field binding the simulator
  data scope to its effective policy/checkpoint identity.
- `core/industry_group.py`: only explicit schema-V2 development returns an empty
  classification mapping, after validating its request. No provider fallback.
- `core/pit_feature_snapshot.py`: explicit development compatibility for existing
  S&P affiliations, causal price/fundamental features, missing group facts, and
  partial causal RS. All supplied RS values still pass finite/range validation.
  Production complete-union RS checks remain. Schema-V2 holdings use the existing
  hash-bound `PriceIdentityTransitionContract.resolve_open_holding` instead of
  `membership_v3`.
- `docs/pit-optimizer-v5-development.md`: request selection, canonical hash and
  rebuild requirements, observed data limitations, and absence of production
  readiness claims.

No changes to `pit_data.py`, production CLI, operations, runtime, production
preparation/authorization gates, or policy sources. No tests were read, created,
modified, or run. No network/provider calls, Docker operations, commits, or
subagents were used.

## Commands and actual evidence

1. Compiled the seven changed Python source files with Python 3.13.14 using
   `compile(Path(name).read_text(encoding="utf-8"), name, "exec")`, without writing
   bytecode. Final pass succeeded.
2. Ran `python -m ruff check core/backtest_engine.py core/engine_policy.py
   core/industry_group.py core/pit_feature_snapshot.py
   core/pit_optimizer_v5/container_protocol.py
   core/pit_optimizer_v5/container_entry.py core/pit_optimizer_v5/evaluator.py`.
   Final result: `All checks passed!`.
3. Ran `git diff --check`. No whitespace errors; Git emitted ordinary LF/CRLF
   working-copy warnings.
4. Ran an inline, read-only real-bundle helper invocation using `PITDataBundle`,
   `load_price_identity_transition_contract`, `fetch_closes`, `fetch_price_data`,
   the existing engine `_calculate_rs_snapshot`, `build_entry_features_v3`,
   `build_holding_features_v3`, and `load_pit_industry_assignments_as_of`.
   Authenticated actual schema-V2 bundle SHA:
   `cf729b47d762ae86287bb8a87c28a1664e7475c74ef1aca736fbc240111349de`.
   On 2021-03-31 there were **505 active members, 504 causal RS scores**, and zero
   industry assignments. The only absent active RS was BBWI. Reference symbols
   remained IWM, QQQ, SPY.
5. The first real helper attempt failed on V3 complete-union RS coverage for BBWI.
   Read-only SQLite/provenance inspection found BBWI membership beginning
   2021-01-01 but real price rows beginning 2021-08-03. The controller explicitly
   authorized partial causal RS for development, preserving absent values.
   The subsequent invocation passed without changing membership or prices.
6. Real AAPL entry features returned `affiliations=("sp500",)`, industry and sector
   RS `None`, earnings acceleration `0.16051376146788993`, sales acceleration
   `0.10448020120716085`, fundamental age 62 days, ATR fraction
   `0.027658207122390508`, breakout gap `0.014595496246872394`, average dollar volume
   `15136819183.4066`, and distance from 52-week high `-0.15810876007995034`.
   Holding features returned actual RS 53.5, industry RS `None`, and volume ratio
   `1.1235298963617752`.
7. The real authenticated transition contract contained 15 transitions. Direct
   calls of its existing resolver returned COG -> CTRA on 2021-10-04, WLTW -> WTW
   on 2022-01-10, and VIAC -> PARA on 2022-02-17.
8. Read the existing recorded quick request without modifying it, regenerated its
   in-memory canonical input with the production field and then development
   field, and decoded the development serialization. Production input SHA was
   `628d1e095af28250a99b8efcb20e7170019d0ab63a2da668db064f935f3ac941`;
   development input SHA was
   `fc178d6e370323d686eaca260744f3713b567c8718109e2220bfe59dcadff66b`.
   Decoded scope was `development_sp500_v2`. These are helper-verification hashes
   of the old recorded request, not identities for the controller's rebuilt run.

## Remaining integration work and limitations

The controller must rebuild and execute the actual evaluator image with new
source-bound identities and `pit_data_scope="development_sp500_v2"`. No complete
simulation is claimed by this report. The request decoder requires the scope
field in newly serialized canonical JSON, including production requests.

Legacy S&P membership/price coverage limitations remain, including BBWI. Industry
and sector facts remain absent, and held exposure stays unclassified. Development
results cannot authorize production baseline capture or held-out evaluation and
do not establish three-universe, classification-feature, or final V5 readiness.
