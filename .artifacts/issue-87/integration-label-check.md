# Issue #87 integration label check: #86 draft

**Checked:** 2026-10-03 (America/Toronto)
**Scope:** Read-only comparison of #86 report contracts, diagnostic definitions/formulas, and constructed role descriptions against the committed #87 source-correction request. No source was changed and no unchanged tests were rerun.

## Inspected draft identity

The #86 worktree is `C:\Users\llong\.codex\worktrees\210d\RS-momentum-EMA-trading-bot`, branch `codex/issue-86-report-evidence`. Its HEAD is `c6c5b715f77a1225ff9f6e456c8cf3d5145654ab`, tree `62685d7d5ce9d4851051ed5ec98be4b81a5cc757`. The checkout has uncommitted #86 edits; these file hashes identify the exact draft bytes inspected, while the Git commit/tree identify only its base:

| Draft source file | SHA-256 | Relevant definitions/flow |
| --- | --- | --- |
| `core/pit_optimizer_v5/contracts.py` | `17B81534127467BAD041B50BDD70B4400180D9FCEA1C3F935728A3B004073796` | `EvaluationReportV5` v2 fields and validation; `RoleEvidenceItemV5.description`; semantic-v1 serialization omits v2 fields. |
| `core/pit_optimizer_v5/diagnostics.py` | `732DC1F2FD1B8686BCFA514184DDF432BA532C4F3E864773813946743C1F5922` | Lines 87-111 calibration limitation and v2 metric definitions; lines 897-901 gross-path reconstruction; lines 914-915 observed-session exposure mean; lines 982-990 idle proxy; lines 1027-1051 scale-out gap; lines 1098-1106 v2 report construction; lines 1120-1264 role projection. |
| `core/pit_optimizer_v5/production_runtime.py` | `9D15E8FE07A38D06520998024D5030962CAC3F34818A309047798101C9225425` | Lines 1066-1106 investigator role construction; lines 1519-1623 detailed report role construction and friction calibration descriptions. |

Compared source-correction request: `.artifacts/issue-87/source-corrections.md`, SHA-256 `55CE8E3F065FA812F621E7E026A50D862EDFEBE6D3675322D96BF4AF20ED0811`, from #87 HEAD `666dd5b925095f25053ae3ab4af613f2ee0a2d8c` / tree `7edbbe664e6c4f3acfbb7dcf8295f05991e7f4bc`.

## Findings

| Meaning checked | Draft result |
| --- | --- |
| Same-path gross estimate | **Matches.** The formula adds cumulative configured fill friction back to each observed equity point and calculates return on that path. The v2 definition calls it a “same-path gross-of-configured-friction estimate” and states that it is not a zero-cost re-simulation or counterfactual. |
| Observed-session exposure proxy | **Matches in the final draft bytes re-read.** The calculation averages each observation's `gross_long_notional / total_equity`, giving every observed session equal weight, then scales net annualized return by that mean. The current v2 definition explicitly says “arithmetic mean of observed-session gross-long-notional / total-equity exposure fractions,” says each session is weighted equally rather than by elapsed time, includes terminally liquidated sessions, and excludes interest/redeployment. |
| Whole-episode maximum timing unknown | **Matches.** The formula uses `episode.maximum_completed_bar_price` for every scale-out in that episode. The metric definition says the maximum has no timestamp and its chronology relative to a sale is unknown; it describes a hindsight price gap and disclaims realized loss and missed future upside. The $130-before-sale example remains a scenario assumption from #87, not evidence supplied by this field. |
| Calibration unavailable | **Matches.** Reports carry `friction_calibration_status="not_supplied"` and a limitation saying no empirical inputs/provenance or quote, volume, ADV, order-size, or participation data calibrates the scenario. Role projections include configured scenario rates and that status/limitation. |

## Public and role propagation

`EvaluationReportV5` requires all three v2 metric definition IDs and non-supplied calibration status. Its semantic-v1 primitive serialization omits the added v2 fields. `diagnostics.to_role_evidence` attaches each metric definition as the corresponding role item description and adds the calibration limitation/status. `production_runtime._investigator_parts` and `_report_evidence` also attach those descriptions to constructed role evidence; the detailed path includes the scenario parameters and calibration status. The inspected paths therefore carry the definitions to both report and role consumers.

## Draft status

The diagnostics file changed during this read-only check: its first observed draft hash was `D8C6E48B48331AFD586CC6F055ECF090E770E261C8EE9590D35EEFA8F6A0897A`; a subsequent re-read produced the hash recorded above and included the explicit idle denominator and equal-weighting. Findings reflect that latter byte identity. No additional #87 wording correction is pending against the current inspected bytes.

The #86 worktree is still in progress and has uncommitted edits. Treat this as a draft integration check, not final acceptance; final review and source binding remain pending.
