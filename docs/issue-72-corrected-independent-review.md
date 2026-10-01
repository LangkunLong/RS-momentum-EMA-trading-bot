# Issue 72 corrected independent final review

## Verdict

Specification: **pass for the bounded synthetic bridge scope**. Code quality/integrity: **pass; no remaining actionable findings identified**. Ready for Lead A integration and final integrated focused/quality gates. This is not a production-data admission or merge claim.

Reviewed final source: 32a52915853cfe1a240e7898796213b405c05c66.
Base: ab385d792e19ff6db39d87f1123f47f660fc1e1d.
Earlier reviewed revision: df3892234ad4b95408b9354c445e9b3a61985328.
Worker: C:/Users/llong/.codex/worktrees/2c6e/RS-momentum-EMA-trading-bot.
HEAD and clean status independently verified. Whole base-to-final scope reviewed through complete original source review plus all correction changes. git diff --check across base-to-final passed.

## Findings resolved

- R1, same-issuer lookback loss: single-CIK histories again include every available issuer snapshot under each authenticated alias. Exact original counterexample now yields NEW and OLD each with EPS0.75/public_date2020-01-02 and EPS1.25/public_date2020-01-06. The repeated FISV/FI extraction regression preserves intervening FI-period filings. Multiple-CIK reuse remains separately constrained and has an explicit regression; this does not authorize joining different issuers.
- R2, mixed identity omission: segmented lineages and remaining legacy/ordinary lineages are now processed together. Original mixed Fiserv+ORD counterexample emits FI,FISV,ORD. The focused regression additionally drives the mixed result through actual security-master and extraction functions and verifies ORD financial output.
- R3, fixture membership binding: non-SEC synthetic fixture branch again requires the exact destination membership hash. Focused negative zero-hash case rejects; replacing it with the exact hash accepts under explicit fixture opt-in. SEC-source bridge validation and production admission guard remain intact.

## Independent verification and retained evidence

Ran only concrete-risk checks, not the full suite:

1. Re-executed the two original algorithmic counterexamples on the exact final source, retaining the original failing evidence separately.
2. Directly invoked five exact committed regression functions with assertions enabled and isolated synthetic directories: mixed segmented/ordinary extraction, repeated same-issuer aliases, different-CIK ticker reuse, synthetic fixture hash rejection/valid acceptance, and actual SEC parser/master/extractor/publisher/builder SQLite case. All five passed. This is direct regression-function execution, not a claimed pytest session.
3. Independent actual builder produced bundle SHA256 **85d1a599794d4a4d2e0f5808cc605336c42c80373937af60e0952acba01be487**, exactly matching the corrected worker receipt. SQLite assertions cover all four original financial rows with unchanged values, period ends and public dates.
4. All five source-file SHA256s in the corrected receipt match actual frozen worker bytes. Original tracked receipt SHA256 matches its retained identity, confirming preservation rather than rewriting.
5. Inspected actual retained command outputs supplied through authorized worker read: original68pass/1warning6.66s and Ruff exit0; corrected71pass/1warning7.05s, Ruff and compile exit0. The earlier71pass6.37s output is also retained. No rerun was used to reconstruct these historical logs.

Evidence in lead .artifacts/coordination:

| Artifact | SHA256 |
| --- | --- |
| issue72-corrected-counterexamples.py | 017e209e7f34364c19924bf5de90c4d9b4dbfbad58e20b1688fd2402bf5b805d |
| issue72-corrected-focused-checks.py | 599e4298b1ae68f24a0234cca15b67c0c37ae879a63415ff68ab7c4dd1b4e0a7 |
| issue72-corrected-counterexamples/result.json | ebb64e33b2fe4ad248a368256e735c850a4b5b993fecd90f23738ce7e2ea0b1d |
| issue72-corrected-counterexamples/focused-regressions.json | c48638228e05b068711287a8b2bd448b0d102789f375b1160f6e3663da23a84b |

The corrected counterexample directory retains generated synthetic inputs and actual SQLite output. Raw historical command records: issue72-retained-original-command-outputs.json and issue72-retained-corrected-command-outputs.json. Worker receipt docs/issue-72-financial-lineage-bridge-followup-receipt.json binds source/input/output digests and distinguishes the original receipt.

## Exact issue acceptance mapping

| Criterion | Review evidence | Disposition |
| --- | --- | --- |
| Bind exact original inputs and record lineage-to-ticker transformation | Source V3 membership/provenance/prices hashes; deterministic ticker-membership ledger; separately retained identity-history windows; builder independently reconstructs both; master/history/audit bytes rehashed and rows checked; original exporter archive identities preserved | Satisfied for synthetic/retained bridge scope |
| Actual builder accepts coherent outputs and rejects foreign/relabelled membership | Deterministic synthetic ZIPs through real SEC parsing, master construction, extraction, publisher and SQLite builder; foreign V3 membership/provenance, relabel-only digest, tampered ledger/audit/master/facts controls; corrected fixture zero-hash rejection; source production admission remains closed | Satisfied for bounded builder path |
| Publication timing and financial values unchanged | Actual four-row SQLite equality; original strict next-SPY-session extraction retained; alias-history and repeated-symbol regressions; no secondary publication shift; no fixture-derived coverage/consumption claim | Satisfied |

## Four statuses

- Implementation: complete and independently reviewed at32a5291; integration/merge pending.
- Required inputs: actual deterministic synthetic membership, identity and filing artifacts supplied for this implementation issue; production source acquisition remains separate.
- Acceptance evidence: all three criteria independently reviewed with reproduced corrected behavior and exact evidence identities; final integrated checks pending with Lead A.
- Dependencies: accepted #66 and settled V3 identity interface consumed. Full #68/#69/#70 data acceptance remains open and is not implied.

## Integration and limits

Latest-main comparison available during the earlier frozen review, 68b5358e55df1d8a93851550f421e594541dc74e versus dispatch base, had no changed-path intersection with this bridge change. This does not replace integration on latest main or the planned combined focused/quality gates. Lead owns publication and principal independent final merge acceptance.

No provider/network acquisition, operational mutation, Docker run, or old expensive report rerun was performed. Production source admission remains fail-closed. SEC archive identities at bundle build are exporter provenance declarations; the builder does not claim fresh ZIP consumption. Broad pytest run was stopped without final summary and remains informational, not acceptance evidence. The cache_dir warning is correctly attributed to pyproject.toml and is unrelated to these corrections.
