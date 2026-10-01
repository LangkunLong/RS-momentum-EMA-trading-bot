# Lead C completion record

Status: ACTIVE / INCOMPLETE. Implementations and scoped reviews exist, and source is integrated locally for correction and validation. No C remote publication, principal acceptance or normal remote merge has occurred. Canonical ongoing state: `docs/next-phase-paper-policy-ledger.md`.

Completion requires all nine original #98/#99/#100 criteria, independent specification and quality review, combined offline boundary/restart evidence at exact integrated source, schema compatibility constraints for D, and actual accepted normal merges. This file is not a completion receipt.

## Original acceptance criteria and remaining evidence

| Issue and criterion | Retained evidence | Current disposition |
| --- | --- | --- |
| #98 equivalent facts give equivalent features, breadth and benchmark context | 88 focused cases, reviewed source51053ad/report8190c15; controlled V2/V3 comparisons; unchanged accepted #71 calculator | Scoped spec/quality approved; final integrated and principal acceptance pending |
| #98 legacy scanner filters do not silently remove candidates | Complete eligible-universe fixture includes legacy-filtered candidates; rejects unexplained omissions | Scoped review approved |
| #98 unavailable fields remain explicit and historical evidence stays historical | Missingness reason checks, availability/date boundaries, recorded completion and full supplied-input manifest | Scoped review approved; no production/provider admission claimed |
| #99 residual cash, pending orders and partial fills | Reviewed consumer fixf9575a5; focused tests plus actual combined restart, replay and residual-reservation assertions | Same-session combined case passes at46b0d8e; later-session compatibility and final review pending |
| #99 exposures, risk and classifications reconcile at declared valuation | Canonical monetary facts and holding-specific joins; combined fixture includes actual acquired shares and pending commitments | Same-session combined assertions pass; later-session origin/current valuation handling under correction |
| #99 missing facts never become zero risk | Unready findings and strict V3 feature suppression; combined missing stop/classification/pending risk and conflicting/stale fact variants | Combined negative case passes at46b0d8e; final corrected-source review pending |
| #100 decision/action identity survives restart | Pure approvalaef51d0; durable slots, fill receipts and atomic consistent read; two lead feature-to-decision tests pass | Store aliases, recovery and public writers require fixes; no whole-chain acceptance |
| #100 additions, exit tiers, stops, peaks and pending intentions persist | 18 pure + 13 store focused tests at frozen1847869; lead chain retains completed addition and partial entry | Full review requires unknown-risk, unsafe-write, late-tier-fill and partial-entry-protection corrections |
| #100 timing, missed sessions, conflicts, migrations and rollback recover safely | Pure missed-session/remainder cases; temporary-store transactional migration, failure injection, rollback and mixed-generation tests | Caller-version pointer ABA and pending-conflict recovery need fixes; deployment and actual-store migration excluded |

## Completion gates

The first combined execution at application headdcbf6d0 returned two failures at partial-entry protective-stop setup. Stable regression committeda3ef2b7; receipt `.artifacts/lead-c/combined-first-run-dcbf6d0.md`. After integrating owner fixes67ad856 andc2b6111, exact application head `46b0d8ef28a6ce3f4220da77fb6f7ae8f8091a04` passed both unchanged tests (chunk68fe13: 2passed, 2warnings in2.06s). This verifies their same-session restart/reservation/resolution and negative-account assertions. Independent scoped re-review approved store findings1 and6 atc2b6111.

The third mixed-generation case then ran at the same application source and failed at the known consumer clock restriction (chunk8d515a: 1failed, 2warnings in4.25s). The store's old holding/action identity assertions passed; later account-readiness assertions were not reached. Receipt `.artifacts/lead-c/combined-later-session-baseline-efe6cc6.md`. Remaining steps are store findings2–5/7 and re-review, consumer later-session corrections and passing mixed-generation case, final independent integration review, regenerated final inventory, required CI and principal acceptance/normal merges. Do not add overlapping historical test counts together.

The current implementation inventory is checkpoint evidence only: `.artifacts/lead-c/source-inventory-a3ef2b7.json` and `.artifacts/lead-c/store-api-schema-1847869.json`, with hashes and limitations in the companion inventory/interface documents. Existing execution entry points and #71 calculator are unchanged. No providers, broker, operational store or runtime were used by the lead integration tests.

GitHub export remains separately gated after automatic approval review rejected the #98 push for missing explicit authorization to export the five-file payload. Peer messaging also remains gated. Neither condition prevents the authorized local corrections and reviews; no alternative transport or relay is used.
