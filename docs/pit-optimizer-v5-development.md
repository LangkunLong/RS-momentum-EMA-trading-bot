# PIT optimizer V5 development evaluation

The existing V5 candidate evaluator can run authenticated schema-V2 S&P data with
the V3 policy interface when its canonical `PanelExecutionRequestV5` explicitly
sets `pit_data_scope="development_sp500_v2"`. This is a provisional integration
path through the same candidate worker, portfolio simulator, fills, and report
builder used by V5.

## Selecting the scope

Construct a request with `pit_data_scope="development_sp500_v2"`, a `quick` or
`discovery` panel, and `baseline_capture_inputs_ref=None`. To update an existing
request object before serialization:

```python
from dataclasses import replace
from core.pit_optimizer_v5.container_protocol import panel_execution_request_bytes_v5

development_request = replace(
    request,
    pit_data_scope="development_sp500_v2",
    baseline_capture_inputs_ref=None,
)
request_bytes = panel_execution_request_bytes_v5(development_request)
input_sha256 = development_request.sha256
```

Use the newly serialized bytes and input SHA in the image-bound invocation. The
scope is part of the canonical input hash, and output binds that complete input
hash. The simulator also records scope in result configuration and effective
engine policy identity. Rebuild the evaluator image and regenerate its source,
sandbox, contract, and request identities after this source change. Old serialized
requests must be regenerated to include the new field, even for production.

The constructor default is `production`, which requires schema-V3 data in the V5
evaluator. Development scope requires actual schema 2; it neither relabels a
bundle nor accepts schema 1. Development requests cannot carry baseline-capture
authority or evaluate a qualification/held-out panel. A baseline source revision
may still be measured as a development candidate, without creating a production
baseline artifact. Production preparation and authorization gates are unchanged.

## Real development data and limitations

The local development bundle is
`.artifacts/pit-optimizer-v5/development/data/sp500-v2/pit_bundle.sqlite3`, SHA-256
`cf729b47d762ae86287bb8a87c28a1664e7475c74ef1aca736fbc240111349de`, with its adjacent
`prices_provenance.json`. Authentication still checks the bundle, provenance,
identity contract, policy source, and evaluator authority.

This bundle contains historical S&P membership and SPY/QQQ/IWM reference prices.
It is not a three-universe V5 production dataset. Existing S&P affiliations are
reported as `("sp500",)`. No dated industry classification or sector taxonomy is
invented: entry industry/sector RS remains `None`, holding industry RS remains
`None`, and held industry/sector notionals remain `unclassified`.

Schema-V2 development allows the causal engine's partial RS snapshots. A missing
holding RS stays `None`; supplied malformed, nonfinite, or out-of-range RS values
still fail. Membership and tradable-universe data are not rewritten. One observed
limitation is BBWI: legacy membership starts on 2021-01-01, while actual price rows
start on 2021-08-03, so its RS is absent on 2021-03-31. This is not a zero score or
synthetic history. Normal strategy qualification continues to use available facts.

Price and public-date cutoffs, completed-session history validation, and causal
feature calculations remain active. Legacy holding validation uses the existing
hash-bound `PriceIdentityTransitionContract.resolve_open_holding`, so unsupported
identity boundaries still fail instead of extending a holding with invented data.
Production schema-V3 feature validation still requires complete active-union RS.

The container exits nonzero on failure and emits a bounded stderr diagnostic with
the integration stage, final source filename/line, exception class, and sanitized
message. It does not print source lines, locals, environment values, or a worker
transcript. No success artifact is emitted for a failed evaluation.

A completed development run demonstrates integration on this provisional S&P
data. It does not establish production baseline, held-out, industry-feature, or
three-universe readiness.
