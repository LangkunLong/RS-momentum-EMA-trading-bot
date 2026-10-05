# Historical scope and feature contract

**Issue:** #66 — Define historical research scope, feature meanings, and coverage requirements

**Status:** Accepted definition on 2026-09-28; #97 timing/status review complete; #80 confirmed the shared status projection

**Source revision inspected:** `c628a3af3c2c14dd684340d1b695ce91f8438842`

**Owner decisions incorporated:** foreign-issuer financials may be deferred; public facts become available on the next eligible session; compact quarterly and annual EPS/revenue headline histories and annual ROE remain in scope; full SEC filing text is not required.
**Boundary:** This document defines the facts a historical policy may receive. It does not acquire data, calculate features, change policy scores, or accept a production dataset.

## 1. Purpose

For every historical strategy decision, the research bundle must answer: **what was publicly knowable then, what does each value mean, and how much of the declared universe has usable values?** A field in a schema or a current vendor response is not by itself evidence that the historical fact was available at the decision time or used by the policy.

Use compact, versioned headline histories where they meet the definitions below. Do not require storing or parsing complete SEC filings. Keep a source reference or digest and enough period, publication, method and revision metadata to reproduce and audit each value.

## 2. Universe and claims

### Production claim

The production research universe is the historical union of the S&P 500, Nasdaq-100 and Russell 2000. Use dated membership and stable security identity. Preserve the constituent index tags and overlapping membership; count a security once in the pooled union denominator for a given session, and publish separate index-slice coverage as well.

### Development claim

An S&P-only or otherwise reduced bundle may support explicitly labeled development checks. It does not establish production-universe coverage or qualify a full-universe strategy result. A symbol/row count does not establish point-in-time feature coverage. Do not lower an existing coverage gate to make a smaller bundle pass.

Foreign issuers remain in the production membership/price scope when their dated index membership requires it. Their financial features are outside the initial supported financial-data claim; report them as unsupported/unavailable rather than dropping those securities from the universe.

## 3. Point-in-time visibility and revisions

1. Every observation must identify the fiscal period it describes and the earliest supported source-public date/time. Period end and provider ingestion time are not publication dates.
2. A fact becomes eligible on the **first eligible exchange session strictly after its source-public date**. For example, a headline released Tuesday is not used for Tuesday's completed-session decision; it is first eligible Wednesday (or the next session if Wednesday is closed). This rule applies even when the source has an intraday timestamp. Keep `source_public_at/date` distinct from derived `available_from_session`; if an existing bundle's `public_date` already stores the first usable session, do not apply the next-session shift a second time.
3. Prefer a supported public earnings-release date when the headline value is demonstrably present in that release. Otherwise use the supported filing publication/acceptance date. A provider's ingestion/update timestamp is not the public date. Do not infer an earlier announcement date from a later filing or from today's company profile.
4. Each restatement or source correction has its own public date and revision identity. Earlier decisions keep the version that was public then; never rewrite historical snapshots using a later restatement.
5. If only an undated/latest value is available, it is not eligible for a point-in-time historical claim. Keep it out of historical features and report the observation unavailable.

## 4. Financial headline contract

### 4.1 Permitted compact data

Full filing documents and narrative parsing are not required. Use either (a) historically dated, source-reported growth/ratio headlines, or (b) a compact history of the reported scalar headline values from which the same metrics can be calculated. A precomputed value is acceptable only when its formula, fiscal-period basis, publication/vintage date and revision behavior are known and compatible with the strategy metric.

At each decision session, expose the latest compatible observation that is already public, together with its fiscal period and age. Between report dates it remains the last known fact until superseded by a new public observation; this is an as-of selection, not interpolation or a vendor's current backfill. If the newest report lacks a required comparable value, report that feature unavailable under its period rule rather than substituting a stale older growth result.

The initial filing-backed scope is U.S. domestic **Forms 10-Q and 10-K, including 10-Q/A and 10-K/A amendments**. Other forms (including 8-K as a filing feed, 10-QT/10-KT, and foreign 20-F/6-K) are unsupported for this initial filing-backed claim. A separately archived public earnings-release record may supply a headline before the 10-Q/10-K only when that record contains the value, reporting period and public date. Foreign financial periods/forms/currencies are deferred. This is a financial-feature decision, not permission to remove foreign issuers from the declared universe.

### 4.2 Policy-visible metric inventory

| Metric/history | Minimum meaning and units | Lookback requirement | Policy relationship |
|---|---|---|---|
| Quarterly EPS growth | Same-fiscal-quarter year-over-year growth; normalized decimal (`0.25` = 25%). Preserve the EPS basis (diluted, basic, or the existing explicitly labeled net-income fallback); do not relabel another family as EPS. Growth is `(current / prior) - 1` only for a valid positive prior value. | Retain the latest four quarter growth slots for the current CANSLIM C calculation and the two newest for V3 acceleration. Missing quarters remain missing; full-window coverage is reported separately from the existing score's handling of partial history. | Latest quarterly earnings growth contributes to the existing C score; the V3 feature builder exposes quarterly earnings-growth acceleration. |
| Quarterly revenue growth | Same-fiscal-quarter year-over-year growth; normalized decimal; preserve currency/concept basis in provenance. Growth is `(current / prior) - 1` only for a valid positive prior value. | Retain the latest two quarter growth slots for V3 acceleration. The current N score uses the newest quarter match only; if that comparison is unmatched, the input is unavailable rather than backfilled from an older quarter. | Latest quarterly sales growth contributes to the existing N score; V3 exposes sales-growth acceleration. |
| Annual EPS growth | Current annual value compared with its immediately preceding reported annual observation for the same EPS family; normalized decimal and labeled EPS basis. Growth is `(current / prior) - 1` only for a valid positive prior value. | Retain up to the latest three annual growth slots matching the existing three-year consistency setting (four annual EPS periods if calculated from levels). Report incomplete history rather than fill it. | Existing annual A score and entry composite consume annual earnings growth. Do not replace it with an unrelated vendor grade. |
| Annual revenue growth | Current annual revenue compared with its immediately preceding reported annual observation for the same revenue concept; normalized decimal, with comparable currency/accounting basis and a valid positive prior value. | Retain three annual growth observations, matching the annual EPS history window; report shorter history as incomplete. | In scope as a headline history by owner decision. It is not established as a consumer of the current baseline score; availability does not silently add a new score weight. |
| Annual ROE | Current baseline-compatible return on equity, normalized decimal. The inspected implementation divides the latest annual net-income value by the latest nonmissing shareholders' equity value available in its history; it returns unavailable when that equity value is nonpositive or missing. It does not search backward for an older positive value. | Latest visible annual value, with source period and public date. | Preserve this existing annual A score input. A provider ROE may be used only if its calculation and source-period selection match; otherwise compact dated net-income and equity headline values suffice to reproduce it. |

**Growth semantics:** quarterly EPS/revenue comparisons follow the current fiscal matcher: closest prior-calendar-year period within 28 calendar days of the same month/day; unmatched periods, conflicting duplicates or equal-distance ties remain unavailable, and the newest unmatched quarter is not skipped to reuse an older one. Annual EPS growth in the current evaluator uses adjacent available annual observations in date order; it does not verify that a fiscal year is present between them. For a source metric labeled one-year annual growth, the compared fiscal periods must be identified. If the current evaluator's annual behavior and the source's one-year comparison differ because of a missing year, record the mismatch for #71 rather than silently claiming parity. Preserve the reported fiscal period and metric-family/basis. A direct vendor growth headline must disclose the comparison period and calculation convention. Where the prior value is zero/negative, the current baseline's existing unavailable/negative-denominator behavior remains; do not silently invent a growth percentage or change the score. Any different loss-to-profit convention requires a separate policy change.

**Acceleration units:** the current V3 earnings/sales acceleration is newest YoY growth minus the immediately previous quarter's YoY growth. Since growth rates are decimal fractions, the difference is also a decimal fraction (for example, `0.35 - 0.20 = 0.15`, or 15 percentage points). It is unavailable unless both matched growth values are available.

**Q4:** Use an explicitly reported fiscal-Q4 headline when the source identifies its fiscal period and public date. If the source only has annual and Q1–Q3 scalar values, Q4 may be derived as annual less Q1–Q3 only when concept, currency, scale and accounting basis reconcile; the derived Q4 is not public before the annual value becomes public. If neither a valid direct nor derivable value exists, Q4 is unavailable. Never treat an annual growth percentage as a quarterly Q4 growth percentage.

**No full-document requirement:** retain a source accession/identifier, provider/source identity, source public date and a stable link or digest when available. A full filing copy may be kept by a later source-evidence owner if independently needed, but it is not an input requirement for this feature contract.

### 4.3 Minimum provenance per headline observation

- Stable issuer/security identity and source ticker mapping.
- Metric name and exact basis (for example, quarterly revenue YoY growth; diluted EPS YoY growth; ROE formula/version).
- Fiscal period end and period type (quarter, fiscal year, or instant balance-sheet value); for growth ratios, both compared fiscal periods.
- Normalized value/unit, with currency and scale recorded for monetary source facts.
- Earliest supported public date and source type (earnings release or filing); keep provider-observed time separate.
- Derived first-eligible `available_from_session`, stored separately from `source_public_at/date`; downstream consumers must not shift an already-normalized session a second time.
- Source/provider ID, source record/accession/reference or digest, and revision/vintage identity.

For the existing SEC Companyfacts bridge, the upstream fact identity is the accession, form, filed date, fiscal period end and reported value (`accn`, `form`, `filed`, `end`, `val`), joined to the submission archive's acceptance timestamp when available. The current normalized bundle fields include `basic_eps`, `diluted_eps`, `total_revenue`, `net_income`, `total_stockholders_equity`, and `shares_outstanding`. These compact values and their provenance are sufficient; the feature contract does not require full filing text. A headline-growth provider can replace these scalar facts only when it meets the direct-metric requirements above.

For direct growth headlines, preserving the numerator/denominator values is not mandatory if the source method and vintage are auditable. For ROE, the source formula must match the baseline; otherwise ingest only the small net-income and equity facts needed for that formula, not the complete statements.

## 5. Other feature definitions and scope

| Feature family | Definition and policy-visible input | Scope and missingness |
|---|---|---|
| Relative strength and prices | V3 RS scores use the current 12-month weighted quarterly price return: 65-session periods weighted 0.40/0.20/0.20/0.20, then cross-sectionally ranked on a 1–99 scale. The PIT snapshot permits annualized short-history fallback with at least 60 valid closes; preserve this as a labeled fallback. Industry-group RS is the arithmetic mean of classified active members' RS scores (same 1–99 scale); require complete active-universe RS before computing it. Price features: ATR20 = mean of 20 true ranges / session close (fraction; 21 bars); breakout gap = `(session open - prior close) / prior close` (fraction; 2 bars); ADV50 = mean close × volume for the preceding 50 sessions, excluding event session (currency/session; 51 bars including event); distance from 52-week high = `session close / max(high[-252:]) - 1` (fraction; 252 bars including event); volume ratio = event volume / preceding-50-session mean (unitless ratio; 51 bars including event). | Use only an exact completed-session prefix and documented split-adjusted price basis. Missing/short/nonfinite history yields unavailable feature; no current-provider fallback. Production price/corporate-action coverage is outside #66. Record the RS settings/source revision with each bundle so a changed ranking recipe does not silently redefine the score. |
| Index affiliations | Dated active membership identifiers for each index as of the decision session. | Membership source and security lineage must cover the session; pooled-union and per-index membership are retained separately. | Visible context, not itself a score. It supports correct universe and feature denominators. |
| Industry group | Dated industry assignment and group RS calculated from eligible active-universe member RS values, using the same RS scale. Assignment needs effective date and public/source date. | Distinct from sector. A current classification profile cannot be backfilled into historical dates. Missing assignment or inadequate group history yields unavailable group feature. |
| Sector | Dated explicit sector assignment, sector RS, and portfolio sector exposure are distinct fields. Conditional on a nonmissing dated `sector_id` and a complete active-universe RS snapshot, sector RS is the arithmetic mean RS score of active members assigned to that sector, on the same 1–99 scale. | The original #66 acceptance recorded V3 sector RS as intentionally `None`; issue #77 extends V3 with this conditional calculation. Missing or blank `sector_id` remains unknown/unavailable and is never inferred from industry. Portfolio exposure sums visible held notionals by sector; holdings without a visible assignment or sector ID remain under `unclassified`. |
| Institutional ownership quantity | Initial aggregate inputs are reported institutional ownership percentage (`held_percent_institutions`, normalized to a documented fraction) and current/prior reporting-institution counts (`institution_count`, `prev_institution_count`), each with its reported period and public date. Reported shares held may be retained if the source supplies them; do not invent share quantity from holder counts. | A percentage is policy-visible only when its denominator is documented and dated. Shares outstanding may be reported as a separate proxy denominator; missing denominator makes a derived fraction unavailable, not zero. This describes quantity/trend only, not holder quality. The saved development audit found ownership fields empty; that is a saved gap, not a new measurement. |
| Shares outstanding / float | If needed for ownership or supply, shares outstanding is an explicitly named proxy denominator. | Do not call it public float or tradable float. True float requires its own dated facts and treatment of insider/restricted classes. |
| Sponsor quality | Quality/identity of particular institutional sponsors, persistence rules and weighting. | Optional and deferred. Aggregate ownership or holder count is not sponsor quality. |
| New catalyst / supply | Current measurable representation is quarterly revenue growth and price proximity to a 52-week high. These are financial/price proxies, not event facts. | Product launches, management changes and other dated events are optional and deferred pending an event source and timing/backfill contract. Do not claim those events are modeled. |
| Fundamental freshness | Age in calendar days from the newest `available_from_session` for a visible quarterly observation to the decision session. Keep its original source-public date separately; do not measure age from fiscal period end or provider ingestion time. | Expose age and provenance. This document adds no stale-data threshold and does not relax or invent a policy gate. If the value/history is absent, unsupported or invalid, report the reason and use `None` at an optional numeric policy field. |

## 6. Missingness and value handling

Keep these states distinct in source/coverage evidence: `not_yet_public`, `absent`, `unsupported_scope`, `insufficient_history`, `stale_by_declared_rule`, `invalid`, and `observed`. Numeric zero is an observed value when the source reports zero; it is never a substitute for missing. Do not silently forward-fill across a revision, fill from today's provider response, or assign neutral values to unavailable fields.

The policy-facing numeric field may be `None` where its contract allows absence. Carry a reason/provenance alongside the coverage record, even if the decision interface itself only carries `None`. Fail closed on invalid units, dates, nonfinite values or broken source lineage.

### 6.1 Projection to the shared policy boundary

#66 records detailed source/coverage state. #80 may use a smaller policy-boundary status vocabulary, but the adapter must preserve the #66 state as a detailed reason/provenance field:

| #66 source/coverage state | Confirmed #80 boundary state | Required detail / rule |
|---|---|---|
| `not_yet_public` | `not_yet_public` | Applies to a specific expected observation before its `available_from_session`. It is not the status of an as-of feature when an earlier published observation remains the latest known value. |
| `absent` | `unavailable` | Retain reason `absent`; do not impute a value. |
| `unsupported_scope` | `unavailable` | Retain reason `unsupported_scope`. Do not map this to `not_applicable` by default: e.g. financial data being unsupported for foreign issuers is a data-support limitation, not proof the metric cannot apply. If the feature is required, this remains a readiness gap. |
| `insufficient_history` | `unavailable` | Retain reason `insufficient_history` and the lookback deficit. |
| `stale_by_declared_rule` | `stale` | Retain the threshold/rule and measured age. A stale state is emitted only when the versioned feature contract declares the freshness rule. |
| `invalid` source fact/provenance | `unavailable` | Retain reason `invalid` and the validation failure. Reserve `calculation_failure` for a failed transform after valid source inputs; do not disguise corrupt source data as a calculator defect. |
| `observed` and validly normalized | `present` | Retain the selected source observation, fiscal period, source-public date, `available_from_session`, metric/calculator identity and value. |

Use `not_applicable` only when a versioned feature contract has an explicit applicability rule for that entity/decision and says the field is not required in that case. It must not be a shortcut for missing, unsupported, stale, invalid or optional data. A required feature with any non-`present` boundary state blocks activation unless an explicit applicability rule excludes that case. An optional feature may remain `None`; its boundary state and detailed provenance remain available for audit and must not be converted to zero/neutral.

#80 confirmed that the detailed #66 state and provenance remain authoritative at the trusted feature/coverage boundary; concise labels are derived and linked, not added as new V3 feature fields. The projection is intentionally lossy: in particular, both `absent` and `insufficient_history` map to `unavailable`, so consumers must retain the linked detailed reason rather than infer it from the concise label.

The status applies to an **observation or as-of feature value**, not indiscriminately to a whole symbol/session. For example, before a new quarter is public, that quarter's source observation is `not_yet_public`; the policy's as-of quarterly-growth input may still be `present` from the previous disclosed quarter, with its original period and increasing age. If a required freshness rule is exceeded, the as-of feature becomes `stale`.

The shared compatibility identity should name the feature-contract version, exact required and optional field sets, source-metric definition/version for provider-supplied headlines, and calculator identity/version for locally calculated fields. Historical and current adapters must produce the same declared meanings and units. A policy may consume optional `None` only as unknown. #80 owns the canonical boundary representation; #97 checks that the selected deployment declares and satisfies it. The current V3 numeric dataclasses allow nullable values but do not themselves distinguish source status, requiredness or provenance, so `None` alone is insufficient for readiness gating.

## 7. Coverage contract

For each claimed date range and each required field/feature:

- **Denominator:** all eligible scheduled decision sessions × unique stable securities expected to be members of at least one declared index on that session, from the authoritative dated membership/lineage register. Exclude benchmark ETFs from the strategy-universe denominator. For per-index breakdowns, report the member-security sessions for that index separately; the pooled union still counts an overlapping security once per session. Missing membership/identity is reported as a denominator/lineage gap; it must not make the expected universe silently shrink.
- **Numerator:** denominator rows with a public, valid, correctly mapped observation and the complete required lookback for that field/feature. For a headline growth series, include only dates on/after that observation's eligible session. An observation that is available but lacks sufficient history is not lookback-ready.
- **Report stages separately:** membership/identity-ready, raw-source-observed, public-date-visible, lookback-ready, calculation/normalization-ready and policy-input-ready. Report the specific missingness reason at each failed stage.
- **Breakdowns:** year, index membership, domestic/foreign financial-support scope, feature, source and missingness reason. Publish both security-session denominators and unique-security counts; neither substitutes for the other.
- **Claims:** label an S&P-only or other reduced result development-only. Production coverage requires the full declared union and all existing production gates. No coverage percentage or threshold is changed by this specification.

There is no fresh coverage measurement in this document. The retained development manifest counts and earlier audit observations remain saved context, not current acceptance evidence.

## 8. Source-to-policy map and consumer distinction

| Source/history | Normalized policy-visible value | Current baseline consumer / status |
|---|---|---|
| SEC reported `diluted_eps`/`basic_eps`, or a dated source-reported EPS YoY series with EPS-family label | Current quarterly earnings growth; successive growth history for acceleration | CANSLIM C uses current earnings growth; V3 feature contract exposes earnings acceleration. Preserve the source metric family and existing fallback behavior. |
| SEC `total_revenue`, or a dated source-reported revenue YoY series | Current quarterly sales growth; successive growth history for acceleration | CANSLIM N uses quarterly revenue growth; V3 exposes sales acceleration. |
| SEC annual `diluted_eps`/`basic_eps`, or dated annual EPS YoY series | Annual earnings growth series and annual consistency input | CANSLIM A consumes annual earnings growth. |
| SEC annual `total_revenue`, or a dated annual revenue YoY series | Annual revenue growth series | Owner-approved source/input scope; not a current baseline score contribution unless a policy explicitly consumes it. |
| Compatible dated ROE headline, or `net_income` plus `total_stockholders_equity` headlines | Annual ROE | CANSLIM A consumes ROE under its current definition. |
| Completed-session OHLCV and benchmark history | RS, ATR20, breakout gap, ADV50, 52-week-high distance, volume ratio | Existing and V3 feature paths; exact consumption is policy-specific. |
| Dated industry / sector assignment and universe RS | Industry-group RS; sector RS; sector exposure kept distinct | Industry path exists; sector RS currently unavailable in V3. |
| `held_percent_institutions`, `institution_count`, `prev_institution_count`, and dated shares outstanding proxy | Aggregate ownership level/trend quantity and denominator-labeled fraction | Optional consumer scope; no sponsor-quality claim. |
| Revenue history plus price-high history | Revenue growth and proximity to high | Existing proxy representation; no product/management event inference. |

**Availability is not consumption.** The data contract states which facts a policy is permitted to receive; the policy contract states which facts it actually reads and how they affect decisions. The inspected V3 entry baseline delegates the V2/base decision. Adding annual revenue or a V3 feature to an available snapshot does not silently add it to the baseline score.

## 9. Worked contract examples

1. **Next-session visibility:** a company publishes quarterly revenue growth on Tuesday after close. `source_public_date=Tuesday`; `available_from_session=Wednesday`. The Tuesday completed-session snapshot cannot use it. If Wednesday is a market holiday, use the next open session.
2. **Earlier release than filing:** an earnings-release record shows quarterly EPS growth on October 31 and the 10-Q is filed November 2. If the release record identifies that exact metric, period and value, it is usable from the first eligible session after October 31. If not, use the filing date; never infer October 31 from the later filing.
3. **Revision:** a value first public on August 10 is visible from the next eligible session. An amended report public on September 5 becomes visible from the next eligible session after September 5. Snapshots between those sessions retain the original value.
4. **Fiscal-quarter match:** compare fiscal Q2 2025 EPS with the uniquely matched prior-year period near the same month/day (the current matcher permits a 28-day tolerance for 52/53-week periods). Do not compare with Q1 2025. If the newest quarter has no unique comparator, its growth is unavailable; do not substitute an older quarter.
5. **Fourth quarter:** FY revenue is 1,000 and Q1–Q3 are 740 on the same concept, currency, scale and accounting basis. Q4 revenue may be derived as 260, first visible when the annual value becomes public. If the annual report reclassifies the line item so values do not reconcile, Q4 is unavailable. An annual growth percentage alone is not a Q4 growth value.
6. **Ownership denominator:** reported institutional ownership is 35 million shares and dated shares outstanding is 100 million; a derived percentage is 0.35 only if the source definition supports that denominator. If shares outstanding is missing, the percentage is `None` with a missing-denominator reason, not 0%.
7. **Universe claim:** a study over S&P 500 members only is labeled S&P development scope. A production claim requires the dated union of S&P 500, Nasdaq-100 and Russell 2000 membership, including non-S&P securities and overlapping-membership handling.
8. **Proxy versus event:** 30% quarterly sales growth and a close 1% below the 52-week high can populate those two proxy inputs. They do not establish that the company launched a product or changed management.

## 10. Coordination and ownership boundaries

- **#66 owns:** historical truth, supported financial scope, units/periods/visibility/missingness semantics, production versus development claim labels, coverage denominator definitions and source-to-policy feature map.
- **#80 owns:** canonical shared input types, feature names, score/decision meaning, version identity, coarse policy-boundary status vocabulary and explicit availability-versus-consumption rules. Reuse this document's source semantics; resolve schema/status naming conflicts in the shared contract instead of forking meanings.
- **#97 owns:** present-day data readiness/freshness and policy deployment compatibility, including the selected feature/calculator identity and exact required/optional sets. Current-provider availability must meet the same metric meaning and units but is a separate readiness measurement from historical coverage.
- **#67–79 own later data, calculation and coverage work:** this spec supplies their contracts; it does not claim their implementation or evidence is complete.

## 11. Acceptance evidence for #66

Accept #66 when a reviewer can verify this specification against the published criteria and finds:

1. Every existing/target feature mapped to source, public-date rule, unit, formula or accepted source metric, lookback, missingness and actual baseline-consumption status.
2. Production-union and limited-development claim rules stated without relaxing existing gates.
3. Domestic quarterly/annual headline support, Q4 treatment, foreign financial deferral and announcement-versus-filing timing explicitly decided.
4. Industry, sector, ownership quantity, sponsor quality, float proxy, annual revenue visibility and event/catalyst scope clearly distinguished.
5. Coverage numerator/denominator and stage/reason reporting are precise enough for #67 and #79 to implement without inventing a definition.
6. Worked examples in section 9 cover release/filing timing, revisions, fiscal-quarter matching, Q4, ownership denominator, production/development scope, and price/revenue proxies versus events.
7. Shared field and boundary-status definitions are reconciled with #80; the present-input/freshness boundary and compatibility identity are reconciled with #97; remaining disagreements and disposition are recorded. #97 confirmed next-session timing and reviewed the detailed-to-coarse mapping; #80 confirmed that detailed provenance remains authoritative behind linked concise labels. The shared feature/status mapping review is complete. Any remaining #80 discussion about exit tiers or fill behavior is outside this feature-definition agreement.

### Published issue acceptance criteria map

The five criteria below are copied in substance from the published Historical-01 issue and checked against this specification. “Satisfied” here means the definition/documentation criterion is covered; it does not claim that data acquisition, feature implementation, production coverage measurement or strategy qualification has occurred.

| # | Published criterion | Status | Exact specification evidence | Remaining gap |
|---|---|---|---|---|
| 1 | Every feature has a source/publication rule, units, lookback and missingness definition. | **Satisfied** | Sections 3–6 define point-in-time visibility, financial source and fiscal periods, metric units/lookbacks, nonfinancial feature definitions, freshness and missingness. Section 8 maps sources to policy inputs; section 9 gives timing and matching examples. Deferred sponsor-quality and event features are explicitly out of scope in section 5. | None for the definition deliverable. Later source adapters and usable coverage remain future work. |
| 2 | Industry, sector and institutional quantity are distinguished; optional sponsor quality and event features have explicit scope decisions. | **Satisfied** | Section 5 separately defines industry group, sector, institutional ownership quantity, sponsor quality and new-catalyst/event scope; section 8 distinguishes their policy mapping. | None for scope decisions. Ownership and sector inputs are not supplied or implemented by this document. |
| 3 | Required production claims and limited development claims are documented without silently lowering existing gates. | **Satisfied** | Section 2 defines the historical S&P 500 + Nasdaq-100 + Russell 2000 production union and labels reduced-universe results development-only. Section 7 defines denominators and preserves existing gates; section 9.7 illustrates the distinction. | None for the claims contract. Production membership/data coverage has not been measured here. |
| 4 | Annual revenue and raw-financial visibility, aggregate versus raw ownership, shares outstanding as a float proxy, and revenue/price-high catalyst proxies are recorded without implying sponsor quality or product/management events are modeled. | **Satisfied** | Sections 4.1–4.3 define compact dated headline values, raw financial provenance and annual revenue. Section 5 separates ownership quantity from sponsor quality, names shares outstanding only as a proxy denominator, and identifies revenue growth/price-high proximity as proxies; section 8 maps those inputs; section 9.6 and 9.8 illustrate the rules. | None for the definition criterion. Annual revenue is available input scope and does not silently receive a baseline score weight. |
| 5 | Quarterly/annual filing support, Q4 treatment, foreign-issuer forms and earliest earnings-announcement support each receive an explicit scope decision. | **Satisfied** | Sections 3 and 4 define public timing, domestic 10-Q/10-K support, unsupported/deferred forms, dated earlier earnings-release conditions, quarterly/annual metrics and Q4 derivation/rejection. Examples 9.1–9.5 exercise the timing and Q4 rules. | None for the scope decision. Full production history and source evidence are outside this definition phase. |

**Accepted:** on 2026-09-28 the owner accepted #66's five published criteria for this definition deliverable. That acceptance does not assert downstream implementation, production inputs, coverage measurements or strategy qualification.

Passing this acceptance is a definition result. It does not assert production data completeness, feature calculation implementation, measured coverage, or strategy qualification.

## 12. Review record

An independent bounded review against the published #66 criteria and targeted source behavior was completed on 2026-09-27. Findings were resolved in this draft: worked examples were added; financial field and form mappings were made explicit; quarterly N and fiscal-matching behavior was aligned to current code; ROE and freshness semantics were corrected; and source-public dates were separated from available sessions. The reviewer found no remaining material internal-review issue. The #97 reviewer confirmed next-session timing and reviewed the compatibility/status mapping. #80 confirmed the mapping, including that `absent` and `insufficient_history` both project to `unavailable` while detailed state/provenance remains authoritative and linked at the trusted feature/coverage boundary.

The shared feature/status mapping is confirmed by #80 and #97. The published criteria-to-evidence map above supports the owner's acceptance of this definition deliverable. Publication and issue closure are recorded separately from the original bounded review.

## 13. Status and implementation boundary

This artifact is a definition deliverable only. At the earlier issue-register snapshot, Project values were **Implementation — Not assessed; Required inputs — Not assessed; Acceptance evidence — Not assessed; Dependencies — Ready to start.** The owner's later acceptance applies to the definition, while downstream implementation and production evidence retain their own statuses. Existing source code and saved development-data observations are not substitutes for those later checks.

At the original #66 definition delivery, no application, test, or configuration code was changed, no data was acquired or modified, and no calculation, external provider, model, or broker was invoked.

Implementation follow-up (2026-10-04, issue #77): V3 now exposes conditional dated sector RS, portfolio sector exposure, and fixture-backed sector coverage metering. The fixtures verify causal visibility and missingness only; they do not measure real-source or production-universe coverage.
