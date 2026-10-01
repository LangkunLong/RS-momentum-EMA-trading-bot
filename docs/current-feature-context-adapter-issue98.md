# Issue #98 current feature and market-context adapter

## Purpose

`core.current_policy_inputs` is an offline boundary for building current policy inputs from recorded data. It calls `build_entry_features_v3` and `build_market_context` with their existing inputs and meanings. It does not fetch data, run the scanner, create decisions, or connect to runtime/account actions.

The caller supplies a hash-verified `PITDataBundle` containing the recorded current facts, completed-session OHLCV histories, a dated market-close panel, the RS snapshot, explicit decision times, and source missingness. The bundle's `public_date` is already the normalized first-eligible session. For each visible quarterly observation, the adapter verifies that the separately retained publication record agrees with that normalized date.

## Pre-policy universe boundary

Call `build_current_feature_context_snapshot` before legacy scanner ranking or the fixed RS floor. `candidate_symbols` lists every active member with sufficient source records to attempt feature calculation. The adapter does not rank or filter those candidates, so an RS score below the legacy scanner floor remains available to the policy input path.

Every active member omitted from `candidate_symbols` requires an `unavailable_members` record containing a #66 detailed state, reason, and source identity. The adapter rejects an unexplained omission. It also rejects an omission when both a completed-session close and an RS observation are present; that member must enter feature evaluation and express individual unavailable features as `None` with field-level reasons.

Market context always uses the bundle's complete dated active membership. Members with absent price or RS observations remain in `active_constituent_count`; the existing builder reports reduced breadth and RS coverage against that denominator. The benchmark references remain SPY, QQQ, and IWM. Future rows, malformed closes, and incomplete benchmark windows fail closed.

## Publication dates and decision clock

`derive_available_from_source_date` takes the raw source-public date, an optional exchange-local timezone-aware timestamp, and an explicit ordered exchange-session list. It returns the first session strictly later than the public date, even for an intraday release or when the public date is a holiday. It retains the raw date and timestamp separately from `available_from_session`.

`validate_normalized_availability` verifies an existing normalized session against the same rule and returns that session unchanged. This avoids a second next-session shift for bundles that already store first-eligible dates.

`RecordedExchangeSessionCompletionV1` carries the session date, IANA exchange timezone, recorded official close instant, source identity, and a SHA-256 digest over those record fields. The caller must provide the authenticated recorded calendar evidence; the adapter verifies the record digest and requires the close timestamp to use the declared exchange timezone and local session date. The supplied close instant supports shortened sessions without assuming a standard close time or consulting a live calendar.

`CurrentDecisionClockV1` binds that completion record to the completed feature session. Its declared `as_of_cutoff` must use the same exchange-local IANA timezone and fall at or after the recorded close. Thus a local morning timestamp or a UTC timestamp whose UTC date differs from its exchange-local date cannot accompany completed daily bars. The timezone-aware valuation time remains a later, separate observation; it does not move the feature cutoff.

## Missingness and identities

Every `None` feature needs a #66 state and reason. The adapter keeps the numeric value as `None` and derives the linked #80 boundary state (`unavailable`, `stale`, or `not_yet_public`). Whole-member input gaps remain in `unavailable_members` and are not represented as policy rejections.

The result carries the exact source revision, authenticated bundle SHA-256, `recorded_input_manifest_sha256`, accepted feature-contract identifier, feature calculator identity, and universe scope. The input manifest digest covers the exact declared cutoff and completion record, candidate set, all supplied OHLCV histories, dated market closes, RS values, regime/breadth inputs, missingness, availability records, member dispositions, source revision, and development flag. Snapshot input identity is the bundle SHA plus this manifest digest, so changed same-session values do not reuse an earlier complete input identity. The content digest does not replace the caller's responsibility to authenticate each recorded source.

Schema V2 is accepted only with the explicit development flag and is labeled `development_only`; the synthetic test fixture is S&P-only. Schema V3 retains the S&P 500, Nasdaq-100, and Russell 2000 universe IDs.

Schema V3 does not by itself establish production readiness or provider acceptance. The unchanged V3 feature builder requires an RS observation for every active member; if that cross-section is incomplete, feature construction fails closed. This adapter preserves that requirement instead of changing the shared calculator.

## Local evidence

`tests/test_current_feature_context_adapter.py` builds synthetic SQLite bundles at explicit pytest temporary paths. It compares adapter outputs with direct calls to the unchanged historical feature/context builders for both the S&P-only V2 development fixture and a controlled V3 fixture whose members span and overlap all three source universes. It also exercises a below-floor candidate at the pre-policy boundary, retains absent active members in coverage, checks strict publication timing, exchange-close evidence, input-manifest identity, and missingness, and rejects incomplete universe handoffs. This evidence is offline and does not establish real-provider acceptance or production readiness.
