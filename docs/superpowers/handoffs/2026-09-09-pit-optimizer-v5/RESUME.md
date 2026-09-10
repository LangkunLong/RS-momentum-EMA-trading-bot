# Resume the paused PIT optimizer V5 work

**Do not run anything automatically. The user paused this work on 2026-09-09.** This is a future continuation guide, not a fresh execution authorization.

## Exact local locations

Original worktree:

`C:/Projects/trading_bot/RS-momentum-EMA-trading-bot/.worktrees/pit-optimizer-v5-architecture`

Main checkout:

`C:/Projects/trading_bot/RS-momentum-EMA-trading-bot`

All paths below are relative to the original worktree unless stated otherwise.

| Name | Path |
| --- | --- |
| Proposal package | `.artifacts/pit-optimizer-v5/development/increment-c-campaign-proposal` |
| Concrete launch package | Proposal package + `/concrete-c-c` |
| Prepared campaign root | `.artifacts/pit-optimizer-v5/development/provider-admission-20260908-01` |
| Actual host resource parent, outside worktree | `C:/Projects/trading_bot/RS-momentum-EMA-trading-bot/.artifacts/pit-v5-admission-20260908-01` |
| Full transition progress ledger | `.superpowers/sdd/2026-09-08-pit-optimizer-v5-transition/progress.md` |
| Awake/credential wrappers | `.superpowers/sdd/2026-09-04-pit-optimizer-v5-campaign/run-with-awake-host.py` and `run-with-controller-credential.py` |

Keep the ignored artifacts and this worktree. A clone of `main` contains the implementation and selected evidence, but is not a replacement for the original prepared campaign environment. Git's configured line-ending conversion can change checkout byte hashes; do not replace saved authority hashes with those of a fresh checkout. The evidence directory preserves exact reference bytes using its own `.gitattributes`.

## First bounded task when the user returns

1. Read this handoff and the original transition plan. Check actual current state before action, since time has passed. Do not rerun preparation, old campaigns, or the old helper simply to obtain coverage.
2. Independently review only the frozen capture correction: live `concrete-c-c/run-approved-context-handoff.py`, SHA `d0c53e05c4f9cabf636a0df634b34d42473c460dccd3dd478d6fa29c59634e7f`; exact freeze is `context-handoff-review-freeze-02-exact/`. Compare with the old freeze and actual failure record. The current old review approves only the old SHA; it cannot be reused for the corrected helper.
3. Confirm that no source/receiver process or later invocation was introduced after the recorded stop. At this checkpoint, source invocation returned exit 1 with no live session. Descriptor, receiver claim, and result were all absent. Corrected source mode and receiver mode have never run. There is no session ID to resume from this handoff.
4. Reconcile the saved campaign against the current machine/session before considering execution. Execution-environment fingerprints can change between tool sessions. This wrap-up commits code and advances `main`; it does not establish that the original preparation context remains reproducible later. Preserve the original manifest/config/owner and report any mismatch rather than rebinding them.
5. Once the user resumes and the concrete correction is approved, use the existing grant scope if it still covers the same unstarted/unspent campaign. Do not silently add budget, replace a campaign, or launch during the pause. A different campaign or expanded spend requires its own concrete authorization.

## If the approved handoff is eventually executed

Follow the reviewed helper's actual contract, not an invented shell command. The current source is a reference until its exact corrected hash is independently approved.

- Use the known `C:/WINDOWS/py.EXE -3.13 -B` launcher and original worktree. Do not directly invoke the physical WindowsApps binary.
- Source mode belongs in the matching preparation context. Descriptor existence while its process is still live is readiness; source has no ready print before accept. The one-use local handoff has a 60-second window.
- Receiver belongs in the network-enabled execution context. It must validate the full native graph, all four exact component identities, and 32 unchanged campaign files before running the exact saved argument vector.
- Track the actual receiver session through the child exit. A tool observation timeout does not mean the process stopped and never authorizes another launch. Keep the machine on AC with the lid open; the existing awake guard prevents idle sleep only.
- Preserve descriptor, claim, and result after any failure. No blind retry, deletion, state repair, reservation migration, or owner/config substitution. Diagnose the concrete stage first.

No Windows driver audit, general environment inventory, connection trial, image rebuild, or broad framework rewrite is needed for this handoff.

## Immutable campaign references

Campaign ID: `development-provider-two-evaluated-admission-20260908-01`. Its manifest, adapter config, and policy are under `campaigns/<campaign-id>/` in the campaign root.

| Reference | SHA-256 |
| --- | --- |
| `manifest.json` | `d6ecfcf650ba705015f11220a5152ff24bbec56faf33bab8399d4a5fad0ead72` |
| `round-1-adapter-config.json` | `934b02388104c31e9d8b570e6319eb158bc401312a634fd63438d78559a389ea` |
| `campaign-admission-policy.json` | `8b40cb1992ec1e57d73b1951691bea3cee45ffc04f3db82d84e5ff38e6abdd7e` |
| Saved round owner digest | `05b9b28975be1ae9c4a61431a9d1d287a29ec9abc4e39b552dc5188d87065e7e` |
| `concrete-c-c/launch-envelope.json` | `e3c6c06daf69111fc566605eb3e1b55547c0257bc81c71b6e9c6fa972c5b9237` |
| Actual preparation report | `7b3695cb8206135c02ffbeacffac64f94b8db26e0cd952e825e00bb31c585fce` |

## Completion criteria still outstanding

The campaign must produce two distinct real evaluated feedback rounds, each with authenticated role evidence, experiment records, critic output, checkpoint, and cleanup. Rejected or failed lifecycle attempts do not count. Report actual attempts, rejections, evaluations, spend, and remaining budget separately; preserve the original deadline.

Then use native historical summary and `authenticate_campaign_policy_compliance_v5(repository=..., manifest=..., expected_policy_ref=...)` against the exact enrolled policy. Do not use live adapters to reinterpret old ownership.

Only after two evaluated rounds: analyze the actual candidate edits, windows, returns, trades, exposure, and critic findings. Produce the requested detailed small-step GPT-5.6 Luna implementation handoff from those results. The user creates that future task manually. This pause handoff is not the results-based Luna handoff.

Continue to distinguish provisional S&P 500 development evidence from final three-universe production claims. Vendor inquiries were submitted previously, but mailbox access/reply monitoring was not authorized and is not part of this continuation.

## Standing working constraints

No reading, writing, or running tests; no synthetic requests/responses/records, mock capabilities, or trial executions. Use source inspection, exact-byte comparison, scoped Ruff, and in-memory compilation. Credentials stay in the existing local credential helper; never publish environment values or `.env` contents. Preserve old archives and exact preimages before corrections. Use native extended-length paths for deep Windows file I/O while preserving logical identity paths.

The wrap-up explicitly authorizes commit and push to `origin/main`; it does not authorize campaign execution, trading, deployment, archive deletion, or a new paid run during the pause.
