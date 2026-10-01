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

The account cutoff and valuation time stay separate. The current portfolio and broker
snapshot must share the declared valuation clock. Each action retains its original decision
and execution clocks; a coherent older clock can remain valid for an already-issued action
when the current order, attempt, and account facts match. Future or inconsistent action
clocks fail closed, and expired unsent actions are never retargeted to a newer session.

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
  Equivalent rows using registered IDs for one canonical action attempt coalesce only after
  every supplied ID resolves to that same attempt and material broker facts agree. Unknown,
  cross-attempt, or contradictory aliases and facts block reservations.
- `remainder_ready` continues to reserve its residual. `reconciliation_required` and
  `partial_incomplete` block readiness. Unknown action/order states do not release
  reservations. Broker `expired` state also blocks because the current producer has no
  corresponding local attempt status.
- A holding's `position_reconciliation_required` policy flag blocks readiness and is
  reported with its evidence reason. Other persisted policy flags remain visible to the
  projection but do not block this consumer.
- A flat historical holding retains its confirmed stop price and order references without
  requiring a live protective order. Any active protective sell still mapped to that flat
  episode blocks readiness. A later episode for the same stable security reconciles against
  its own active stop and quantity.
- `PortfolioFeaturesV3` is produced only when all required account facts reconcile and no
  findings remain. Missing cash, equity, position/order snapshots, valuation marks,
  classifications, mappings, or stop-risk facts remain `None` and block readiness.

An explicitly resolved action can release its reservation only when its resolution reason
is present, each attempt is either in a supported terminal state (`filled`, `cancelled`, or
`rejected`) or is provably unissued (`intended`, zero confirmed fill, no primary or alias
order references, and no terminal status), and current broker facts show no active matching
order. Missing reasons, references or fills on an intended attempt, or an active order keep
readiness blocked.

## Scope boundary

The conversion is tested against real immutable DTOs from approved pure producer checkpoint
`aef51d0d4893db1049401bc37ea40f434ea60b56`. A focused consumer test also uses a temporary
SQLite store from persistence checkpoint `d52fb22deedc74361f6e4ae4a0113dc4f215c3c2` and
construction corrections `963a61f1dbc0641d50a4272e845447bf4e40abfa` and
`43a0820dea9688fd8583f45ccb9704f172553954`. The latest focused regressions reopen the
temporary SQLite store, then call `load_policy_execution_snapshot` to reconcile pinned
older-generation records with current portfolio and broker facts. Source `43a0820` and its
report-only companion `bfcaa5a8cfdcfc5e32edfd78e4dfd5bb65e5b2d5` are reviewed producer
construction dependencies; this does not establish full integrated acceptance. The tests
are synthetic consumer evidence, not live durable-store or runtime acceptance. The module
itself remains pure and does not open the store. All account and broker facts in #99 tests
are deterministic synthetic fixtures.
