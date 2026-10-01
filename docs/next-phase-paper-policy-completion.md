# Lead C completion record

Status: LOCAL CORRECTION IMPLEMENTED / INCOMPLETE.

Corrected application/test candidate: `c48d95ca12ea792fcaf7b18004ad90f62d9aabaa`, tree `0e32d17c66156b7d700c69df9d7c43da81b513bc`. Producer source `2881118`, producer report/erratum `f14562a`, consumer test/report `b207822`, unchanged consumer implementation `d2a0677`, and lead chain `0ad64e9` are integrated. Producer and final integrated independent C-R1 reviews approve specification and quality for all nine bounded offline criteria, with no new Critical or Important findings. The integrated review is retained unchanged in `docs/next-phase-paper-policy-c-r1-independent-review.md`. Principal delta acceptance, required remote CI and normal merge remain pending. The earlier ea2e145 publication payload stays held and immutable.

Actual source-bound evidence is 48 producer cases, 7 selected consumer cases and 11 combined/identity cases. The historical 107-pass checkpoint belongs to d2c4082. Raw Git equality binds executed source/test files to the corrected candidate; six changed Python blobs also compile under Python 3.13. No provider, broker, operational store, runtime or legacy execution entry point was used or modified by this correction.

Completion requires all nine original #98/#99/#100 criteria, independent specification and quality review, combined offline boundary/restart evidence at exact integrated source, schema compatibility constraints for D, and actual accepted normal merges. This file is not a completion receipt.

## Original acceptance criteria and remaining evidence

| Issue and criterion | Retained evidence | Current disposition |
| --- | --- | --- |
| #98 equivalent facts give equivalent features, breadth and benchmark context | 88 focused owner cases; controlled V2/V3 comparisons; unchanged accepted #71 calculator | Scoped and integrated spec/quality approved; principal acceptance pending |
| #98 legacy scanner filters do not silently remove candidates | Complete eligible-universe fixture includes legacy-filtered candidates; rejects unexplained omissions | Scoped and integrated review approved |
| #98 unavailable fields remain explicit and historical evidence stays historical | Missingness reason checks, availability/date boundaries, recorded completion and full supplied-input manifest | Scoped and integrated review approved; no production/provider admission claimed |
| #99 residual cash, pending orders and partial fills | Historical107case evidence covers entry/addition/sell paths and zero-fill replacement reservations | Canonical replacement lifecycle passes; integrated review approved; principal delta acceptance pending |
| #99 exposures, risk and classifications reconcile at declared valuation | Coherent canonical facts reconcile; missing replacement holdings correctly remain unready | Acquired risk/notional and protection regressions pass; integrated review approved; principal acceptance pending |
| #99 missing facts never become zero risk | Named durable reconciliation flag blocks V3 readiness until evidenced clear; unrelated flags stay visible without invented blocking semantics | Lead set/restart/unready/clear/ready case passes; independent review approved |
| #100 decision/action identity survives restart | Historical entry/addition/exit identity evidence retained | Replacement identity/attachment/replay pass; scoped review approved; integrated delta review approved; principal acceptance pending |
| #100 additions, exit tiers, stops, peaks and pending intentions persist |43focused producer cases and prior recovery evidence retained | Replacement partial/full and later fills/protection pass; scoped review approved; integrated delta review approved; principal acceptance pending |
| #100 timing, missed sessions, conflicts, migrations and rollback recover safely | Temporary-store migration, rollback, caller-version CAS, mixed-generation and late-reference recovery evidence | Producer and integrated spec/quality approved; deployment and actual-store migration excluded |

## Corrected C-R1 combined checkpoint

The replacement lifecycle passes first partial fill, later cumulative fill and full fill, stable IDs, canonical restart/replay, risk/residual separation and partial/resized protection. Actual 48/7/11 receipts and exact source bindings are retained under `.artifacts/lead-c/`. Producer scoped approval is complete; integrated review approved; principal acceptance remains pending.

## Historical pre-C-R1 combined checkpoint

Integrated application candidate `d2c4082746017dd4ecb14d709d71a7c12b1fc2d5`, tree `1e511574341e425f6ec2c326991a8dfaa6f49f38`, includes producer43a0820/reportbfcaa5a and consumerd2a0677. The three new lead regressions pass, including previously unreached flag-clear and active-protection negative assertions. Six specifically vetted offline modules passed107cases (final chunkdc89cb,2warnings,13.95s), including the complete8case chain and2feature-identity cases. Ruff passed. The compileall traversal reported an inaccessible ignored cache directory; a separate read-only compile of all312committed Python files passed. Python3.11 and required remote CI remain pending. Receipt `.artifacts/lead-c/integration-validation-d2c4082.md` retains commands, warnings, exact inventories and historical failure boundaries.

Independent integrated review approves the final candidate and all nine criteria; the three consumer findings and two inherited blockers are closed with no new Critical/Important application finding. The unchanged full review is published locally in `docs/next-phase-paper-policy-independent-review.md`. The nonblocking source-hash correction is appended by owner49ecbf4 and verified by the lead; application/test bytes remain frozen. Principal acceptance, publication, required remote CI and normal merges remain outstanding, so full completion is not claimed.

## Historical combined checkpoints

Producer43a0820/reportbfcaa5a and consumercf3310f had independent scoped approvals. Six specifically vetted offline modules passed101cases at lead6581650 (receipt872322,2warnings,41.51s). Independent integration review then found3consumer gaps: loss of a durable reconciliation flag, rejection of valid never-issued resolutions, and live-protection requirements for flat history. All3were reproduced by lead tests before correction; the later passing candidate above supersedes this historical failure status.

Expanded five-case chain passes at `549f8d3dc3e12534cb4141ca6aafc52b6ccd268c` with producer963a61f and consumer2cf4bdf: chunk7109f0, 5passed, 2warnings in2.60s. This resolves the two registered-alias regression failures described below. Consumer correction re-review is active. Store re-review clears findings1/3/4/5/6/7 but retains one finding2 variant: late references reopen a resolved holding-backed action without restoring the holding's pending conflict. That remaining correction, final independent integration review and final-source validation/publication/principal gates still prevent completion.

Consumer compatibility6f225eec1c9434f0ae035383cabba43797f33ba2 is locally integrated at lead `c6ee7ceae26e2f6825a1635c874007ee59f156b8`. The original three tests in `tests/test_paper_policy_chain.py` pass at this checkpoint (chunkeb2707: 3passed, 2warnings in4.26s), including the unchanged later-session/mixed-generation case. Receipt `.artifacts/lead-c/combined-chain-c6ee7ce.md`.

Consumer re-review found one Important registered-alias deduplication gap. Two new parametrized combined cases reproduce it through actual store alias registration and restart: chunk583ad9 reports2failed, 2warnings in1.59s at the same application source. The expanded five-case chain is therefore not all passing. Consumer correction/re-review, remaining #100 findings2â€“5/7, final combined review and updated-source verification still prevent full acceptance. Earlier protection/clock failures below are retained history; alias failures are current.

## Completion gates and retained earlier evidence

The first combined execution at application headdcbf6d0 returned two failures at partial-entry protective-stop setup. Stable regression committeda3ef2b7; receipt `.artifacts/lead-c/combined-first-run-dcbf6d0.md`. After integrating owner fixes67ad856 andc2b6111, exact application head `46b0d8ef28a6ce3f4220da77fb6f7ae8f8091a04` passed both unchanged tests (chunk68fe13: 2passed, 2warnings in2.06s). This verifies their same-session restart/reservation/resolution and negative-account assertions. Independent scoped re-review approved store findings1 and6 atc2b6111.

The third mixed-generation case then ran at the same application source and failed at the known consumer clock restriction (chunk8d515a: 1failed, 2warnings in4.25s). The store's old holding/action identity assertions passed; later account-readiness assertions were not reached. Receipt `.artifacts/lead-c/combined-later-session-baseline-efe6cc6.md`. Remaining steps are store findings2â€“5/7 and re-review, consumer later-session corrections and passing mixed-generation case, final independent integration review, regenerated final inventory, required CI and principal acceptance/normal merges. Do not add overlapping historical test counts together.

The current implementation inventory is checkpoint evidence only: `.artifacts/lead-c/source-inventory-a3ef2b7.json` and `.artifacts/lead-c/store-api-schema-1847869.json`, with hashes and limitations in the companion inventory/interface documents. Existing execution entry points and #71 calculator are unchanged. No providers, broker, operational store or runtime were used by the lead integration tests.

GitHub export remains separately gated after automatic approval review rejected the #98 push for missing explicit authorization to export the five-file payload. Peer messaging also remains gated. Neither condition prevents the authorized local corrections and reviews; no alternative transport or relay is used.
