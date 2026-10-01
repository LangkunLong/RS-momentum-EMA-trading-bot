# Issue #99: Synthetic Account Reconciliation

This module provides a pure adapter from the pinned #100 policy-state value types and
synthetic broker facts into the existing strict `PortfolioFeaturesV3` boundary. It does
not open a store or call a broker or provider.

## Inputs

`policy_execution_state_to_projection` consumes a `PortfolioStateSnapshot`, zero or more
`ActionStateProjection` records, zero or more `HoldingEpisode` records, and a synthetic
`BrokerAccountSnapshot`. The portfolio snapshot supplies deployment, store, source-account,
and clock identities. Action projections retain their own decision identity, Decimal
quantities, risk basis, and attempt-scoped client and broker references. Holding episodes
supply confirmed quantity, current broker symbol, and only broker-confirmed stop facts.

The account cutoff and valuation time stay separate. The action/portfolio decision clock
must match the declared valuation session, while the account observation may be later than
the feature cutoff when it belongs to the next execution opportunity.

The adapter builds an explicit `security_id` to `broker_symbol` mapping from the canonical
action and holding facts for that valuation session. Conflicting symbols for one stable
security identity block reconciliation. The adapter never derives a broker symbol from a
security ID. This mapping is only as authoritative as the persisted #100 projection; the
adapter does not consult an external security master.

## Reconciliation rules

- Confirmed fills are already reflected in account cash and positions. Only residual buy
  quantity contributes pending cash and committed risk.
- Pending buy risk uses #100's residual committed risk and risk basis. If either is unknown,
  known cash reservation remains visible while risk readiness stays unavailable.
- Filled position risk, pending buy risk, pending buy cash, available cash, and pending
  strategy sells remain separate output values. Protective sells map to holding episodes
  and do not count as strategy sells.
- Attempt references and per-attempt fill watermarks remain associated with their attempt.
  Conflicting or ambiguous broker aliases block reservations.
- `remainder_ready` continues to reserve its residual. `reconciliation_required` and
  `partial_incomplete` block readiness. Unknown action/order states do not release
  reservations. Broker `expired` state also blocks because the current producer has no
  corresponding local attempt status.
- `PortfolioFeaturesV3` is produced only when all required account facts reconcile and no
  findings remain. Missing cash, equity, position/order snapshots, valuation marks,
  classifications, mappings, or stop-risk facts remain `None` and block readiness.

The current `ActionStateProjection` does not expose `resolution_reason`, even though its
source `ActionIntent` does. The adapter therefore retains `resolved` as unresolved and
blocks readiness. The producer-side field and preservation test are a required integration
fix; this fail-closed behavior is not final acceptance for resolved actions.

## Scope boundary

The conversion is tested against the real immutable DTOs in the #100 pure producer
checkpoint `6bbf20ab2de0f177628bf623c8fa3c138679d868`. That checkpoint does not include the
planned persistence store. Store loading, durable restart reconciliation, combined
#99/#100 acceptance, and independent review remain pending. All account and broker facts
used by #99 tests are deterministic synthetic fixtures.
