# Development campaign composition increment

Base: a068e0aed40ecbe5e0078924071f374e7809b8c2. Scoped source is development_preparation.py, cli.py, operations.py, and production_workspace.py under core/pit_optimizer_v5. This report is the fifth scoped file. Implementation commit is pending: HEAD remains a068e0aed40ecbe5e0078924071f374e7809b8c2. The five scoped files are staged for controller review. Automatic approval review rejected the scoped hooks-disabled commit, stating that disabling hooks could bypass security checks and explicit user authorization for that bypass was not established. No workaround was attempted; controller must resolve the authorization before committing.

## Implemented behavior

Development uses the existing FeedbackRoundInputV5/dependencies, shared private `_run_campaign_v5`, and existing run_feedback_round_v5. The production provider wrapper still requires production scope and its real ledger/gateway graph. DevelopmentAdapterConfigV5 contains actual constructed host identities, seven explicit host paths, an absolute response directory, and policy_only export mode; there are no paid provider fields. Factories and standalone verify-run/summarize select the explicit disabled development scope. Standalone run/resume remain production operations; development uses run-campaign/resume-campaign.

Policy export queries only the four exact EDITABLE_POLICY_PATHS_V5 from the declared immutable commit. It checks complete/unique membership and regular blob modes before reading bytes, then uses existing owned-directory writes. The mode and allowlist bind driver identity; recovery reconstructs that mode. Full production export and its existing identity encoding are unchanged. Development source bootstrap verifies the supplied immutable commit and four blobs, without requiring host HEAD equal the older policy commit or inspecting unrelated worktree changes.

Preparation authenticates four actual canonical request/output pairs, exact round-trip output bytes, development scope, episode IDs development-discovery-1..4, ordinals, one policy/evaluator/sandbox/execution/method/startup authority, gross/base/stress, and base selection. It authenticates the repository data bytes against the contract and compares actual policy source bytes to the immutable four Git blobs. Canonical campaign aggregation checks four unique panel identities and disjoint planned dates. Explicit evaluator_contract_sha256 and sandbox_profile_sha256 pin the intended rebuilt authorities. Nothing is published until all four real outputs and source/data checks pass. Original request/output bytes are preserved in create-only content-keyed binary artifacts and a parent-provenance sidecar; imported panel reference text can differ because identity is checked by content/dates. The baseline uses genuine absent semantic fingerprints and disabled_development.

Without a supplied panel_plan_ref, preparation creates a local plan from the actual four requests and authenticated quick panel. The unused mechanics slot references actual quick content under a distinct development-mechanics-unexecuted episode ID and purpose quick: the existing panel schema only permits quick/discovery/qualification. Two content-hashed local descriptors explicitly state confirmation/qualification executed=false and capability_enabled=false. No final held-out plan or evidence is read or claimed. With a supplied plan ref, existing plan panels are authenticated by normal manifest validation and canonical campaign checks; no replacement plan/descriptors are created.

Pending controller results return status=pending_controller_response, role/call/request refs, original campaign and round start times, deadline, and exact original resume command before cleanup gating. They do not close a round, advance it, or retire a live workspace. Resume also permits an investigator request preceding the first journal event; it retains the already persisted round start. A previous development round's cleanup no longer falsely requires a later pending round's live resources to be retired. Each completed round still authenticates its own durable retirement evidence. Production aggregate cleanup checks remain unchanged. Response submission never changes a deadline.

Controller imports authenticate the original stored call/request and campaign/round launch, check discovery binding and the envelope's exact keys/call/request identities, validate through parse_and_bind_role_artifact, then seal original response bytes using append_controller_role_response. A different existing response cannot be overwritten. Verification accepts only ControllerRoleTerminalAuthorityV5 under explicit disabled development and reconstructs the terminal package from sealed bytes; production paid ledger authentication remains separate.

## Concrete operator interface

Run from this worktree with the actual Python executable. PowerShell variables below are operator inputs, not fabricated digests. `$runtime` is the absolute `.artifacts/pit-optimizer-v5/development/runtime` directory in this worktree. `$inputFile` is an absolute JSON file with exactly the preparation function fields:

```json
{
  "source_root": "ABSOLUTE_GIT_WORKTREE",
  "source_commit": "0985d9bae74162ec1da8a7268bd54e47f21b0b85",
  "git_executable": "ABSOLUTE_GIT_EXECUTABLE",
  "campaign_id": "OPERATOR_CAMPAIGN_ID",
  "target_pct": "OPERATOR_ACTUAL_TARGET",
  "quick_panel_ref": {"relative_path": "panels/specs/quick.json", "sha256": "ACTUAL_QUICK_DIGEST"},
  "evaluator_contract_sha256": "ACTUAL_REBUILT_CONTRACT_DIGEST",
  "sandbox_profile_sha256": "54339696b4f67a85d462512001dc6c391a9d3554e63a76394493fad4bd54e584",
  "parent_results": [
    {"request_path": "ABSOLUTE_REAL_REQUEST_1", "output_path": "ABSOLUTE_REAL_OUTPUT_1"},
    {"request_path": "ABSOLUTE_REAL_REQUEST_2", "output_path": "ABSOLUTE_REAL_OUTPUT_2"},
    {"request_path": "ABSOLUTE_REAL_REQUEST_3", "output_path": "ABSOLUTE_REAL_OUTPUT_3"},
    {"request_path": "ABSOLUTE_REAL_REQUEST_4", "output_path": "ABSOLUTE_REAL_OUTPUT_4"}
  ],
  "output_prefix": "campaigns/OPERATOR_CAMPAIGN_ID",
  "caps": {
    "quick_timeout_seconds": 600,
    "discovery_episode_timeout_seconds": 600,
    "round_wall_timeout_seconds": 7200,
    "campaign_wall_timeout_seconds": 14400
  }
}
```

Optional panel_plan_ref has the ordinary relative_path/sha256 shape. No quick result is needed or imported. Quick remains context for the real candidate stage, not fabricated parent evidence. Physical caps are derived from the actual sandbox profile; method/startup bounds come from the matching parent request. Search is fixed to 2 rounds, 1 hypothesis, 1 axis, 1 variant, 1 survivor, archive capacity 2, 96 KiB memory, no full source escape, and 1 simultaneous evaluation. The four explicit wall timeout fields are typed resource inputs so a controller-approved future bound can be authored before launching a new campaign; existing launch timestamps never refresh.

```powershell
python -B -m core.pit_optimizer_v5.cli import-development --artifact-root $runtime --input-file $inputFile --validate-only
python -B -m core.pit_optimizer_v5.cli import-development --artifact-root $runtime --input-file $inputFile
python -B -m core.pit_optimizer_v5.cli prepare-development --artifact-root $runtime --manifest-path $manifestPath --manifest-sha256 $manifestSha --source-root $sourceRoot --workspace-root $workspaceRoot --data-root $dataRoot --output-root $outputRoot --control-root $controlRoot --git-executable $gitExe --docker-executable $dockerExe --response-directory $responses --output-path $configPath
python -B -m core.pit_optimizer_v5.cli run-campaign --artifact-root $runtime --manifest-path $manifestPath --manifest-sha256 $manifestSha --adapter-config-path $configPath --adapter-config-sha256 $configSha --owner-token-sha256 $owner
python -B -m core.pit_optimizer_v5.cli controller-request --artifact-root $runtime --manifest-path $manifestPath --manifest-sha256 $manifestSha --request-path $pendingRequestPath --request-sha256 $pendingRequestArtifactSha
python -B -m core.pit_optimizer_v5.cli controller-response --artifact-root $runtime --manifest-path $manifestPath --manifest-sha256 $manifestSha --request-path $pendingRequestPath --request-sha256 $pendingRequestArtifactSha --response-file $absoluteResponseFile
python -B -m core.pit_optimizer_v5.cli resume-campaign --artifact-root $runtime --manifest-path $manifestPath --manifest-sha256 $manifestSha --adapter-config-path $configPath --adapter-config-sha256 $configSha --owner-token-sha256 $owner
python -B -m core.pit_optimizer_v5.cli verify-run --artifact-root $runtime --manifest-path $manifestPath --manifest-sha256 $manifestSha --adapter-config-path $configPath --adapter-config-sha256 $configSha
```

Setup emits actual config ref, generated/passed owner, resource/search caps and rendered resume command. Save those values unchanged. All source/workspace/data/output/control directories, response directory, and Git/Docker executables must exist. Data root contains exactly pit_bundle.sqlite3 and prices_provenance.json. Controller-request prints the authenticated request envelope; request artifact SHA differs from the semantic request_sha256 carried inside that envelope. Response file must contain schema_version=5, artifact_type=controller_role_response, the actual call_key_sha256 and semantic request_sha256, and response (the existing role response object). Import/request/response commands call no provider or evaluator. Pending campaign exits 0 and has no completed summary.

## Static evidence and remaining work

Source-only self-review covered canonical panel writes (the dedicated newline-bearing panel writer), nullable fingerprints, config derivation and later-round reconstruction, no-event investigator resume, pending before cleanup, exact sealed response reconstruction, production wrapper/export strictness, and source allowlist completeness. Scoped Ruff passes on all four changed Python modules. In-memory compile passes on those four sources. CLI help was exercised for prepare-development, import-development, and controller-response. No tests were read, created, changed, or run; no synthetic probes, full Git-tree exports, Docker/network/provider calls, pushes/uploads, or operator/data artifact mutations were performed by this implementation task.

Controller's original 15-session parent attempt failed because the real simulator requires 30 sessions. Controller replaced it with four 30-session windows (2021-01-04..2021-02-16, 2021-02-17..2021-03-30, 2021-03-31..2021-05-12, 2021-05-13..2021-06-24), same 32 observed lineages. Old discovery-1..4 files must not seed this import. Use the actual corrected discovery30 request/output pairs once all complete. The controller rebuilt runtime b24bebd9870c78084666491cd0b6c926a8e0b88b258e7ffb11c923e6eda275b5 and confirmed these four changed host modules are excluded from its image closure. No manifest or real campaign was executed by this task. Independent review, all four actual parent results, real controller-authored responses, actual candidate quick/discovery work, records/checkpoints/cleanup, and round-2 feedback evidence remain controller-owned.

## Independent review fixes — round 1

Addressed P1 and P2 from development-composition-review.md. Read-only verify-run/summarize now authenticate the complete manifest first and construct authorities for explicit disabled development before encountering the production-only loader. Standalone run/resume still pass through the unchanged production-only authority loader; a provider-free manifest is not implicitly treated as development.

Preparation now compares the selected plan target with the supplied target and validates all six episode/panel bindings and purposes before the validate-only return and before the first create-only write. For a supplied plan, this includes its own typed content identity, both actual data-reference files, and every mechanics/quick/discovery panel child through the existing authenticated repository loaders. Generated plans validate their actual in-memory panels before publication. Sandbox/resource compatibility is also checked before preflight success. Original request/output and imported path identity handling remains unchanged.

Scoped Ruff on cli.py and development_preparation.py: All checks passed. In-memory compile on those two files passed; source diff reviewed. No tests/probes, Docker/network/provider work, agent delegation, commit attempt, or hook changes occurred during these fixes. Only the two source modules and this report were changed; controller assets and the prior reviewed-stage snapshot were untouched. No implementation commit exists; HEAD remains a068e0aed40ecbe5e0078924071f374e7809b8c2. These changes are staged for scoped re-review, not claimed as executed campaign evidence. The controller owns the currently authorized 900-second actual run and future caps.
