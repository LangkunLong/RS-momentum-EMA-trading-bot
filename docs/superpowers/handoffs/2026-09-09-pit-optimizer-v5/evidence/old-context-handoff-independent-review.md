# Approved-context handoff independent static review

Specification verdict: **PASS**. Quality verdict: **APPROVE**. No blocking findings.

I explicitly approve helper SHA-256 `4e801fc0cf381b520ef147b07aa9f91bbc6edec62c058c9a9d2e3d6acfeb80ff` for **one actual source-mode serve in the preparation context and one actual receiver-mode invocation in the network-enabled launch context**, for the existing approved, unstarted/unspent campaign only. The receiver may invoke the exact existing saved launch once after its native diagnostic gates pass. This is not a new campaign, extra budget, configuration replacement, or permission to bypass native execution checks.

## Exact reviewed bytes

- `run-approved-context-handoff.py`: 22,109 bytes, SHA-256 `4e801fc0cf381b520ef147b07aa9f91bbc6edec62c058c9a9d2e3d6acfeb80ff`.
- `context-handoff-implementation-report.md`: 4,868 bytes, SHA-256 `6a25c0159701f5d05d949259cd355a4f197199b8e2d6ba3c6fb3f835620bdab0`.
- Both independently matched their exact `context-handoff-review-freeze-01` copies.
- Design `ebfcf2c4a59c439e16f7cfebaca330beeaf227e9da53a57c6a870119575f4fe7`; prior stage review `8dd4d849205a37ea90ca00ef834f3d791b9e7e01136e53f4b1fd8ee3dcd74288`; diagnosis `b59c1ad1a0c95fbe4dcdacb81c1a1c20fcda4e1ff1badc29c36f554a7b70268c`; actual diagnostic helper `218c8a53c6a1eb72aa461685e0d9c9dbf39c6b89fc3ee06aace863ae9323cc18`; grant `8ef007db9e1111833b57fee305b419b846b50b95e35a517c45b262b8464e591e`; launch envelope `e3c6c06daf69111fc566605eb3e1b55547c0257bc81c71b6e9c6fa972c5b9237`. All current bytes independently matched the helper's fixed pins.

## Concrete review conclusions

Both modes require bytecode suppression, an exact caller-supplied helper hash, and an exact hash of this fixed review path containing explicit approval for that helper. Fixed design, failure evidence, diagnostic, grant, envelope, and source authority are authenticated. The grant/campaign/root/worktree, manifest/config/policy/owner and canonical saved argv are bound to the same previously approved package. Saved argv hash independently matches `c1104cbe89d0ce3ab818fb5f3a959e26652bcbdb162ff21540f30e46fc077a54`. The exact diagnostic authenticates the current 22 source files and native saved resources before either mode can proceed to its operational step.

Source mode runs the pinned genuine composition diagnostic in-process with stdout/stderr captured in memory. It requires all four exact prepared/actual digest triples, full native host graph, exact campaign/manifest/config, 32 unchanged artifact files, and no provider/evaluator invocation. Only then does it take the actual executor's canonical sorted control-environment tuple. The native tuple shape agrees with `production_sandbox.py` construction. Source mode permits only the six reviewed unique string keys and writes no payload to disk or output.

The local random named pipe is first-instance-only, one-instance, message-mode, and rejects remote clients. The protocol uses byte messages and bounded JSON decoding rather than pickle/eval. The descriptor contains only the random endpoint. Both peers compare the canonical exact campaign/helper/review/argv claim; source serves one receiver and validates its acknowledgement. Source accept and receive waits share the 60-second deadline. Message sizes are capped at 128 KiB with corresponding pipe buffers. There is no reconnect/resend loop in the helper. The installed stdlib's exact byte-connection framing/connection semantics were inspected only as necessary for this concrete protocol; no pipe operation or platform probe was run.

Receiver writes its permanent exclusive claim before connecting and refuses existing claim/result paths. It removes all six control keys from a copy of its own environment, then inserts only the actual source tuple; missing keys remain missing and the receiver's own os.environ is unchanged. It performs the approved diagnostic in a child using that mapping. Diagnostic exit zero is insufficient: all four digest triples, the full graph, exact identities, file count and before/after preservation flags must pass. The payload is never treated as an authority substitute; existing native composition authenticates its effect.

Only after this proof can the sole Popen call use the exact unchanged saved_launch_argv, working directory, and same child environment. Existing awake/credential helpers and native CLI arguments remain intact. Child stdout/stderr are inherited, the receiver waits for that one child, and it records safe digest/boolean/exit metadata and returns the child exit code. Failure paths record the phase and exception type without payload or exception-value dumps; the exclusive claim prevents a second launch. No helper path rewrites campaign/source/config/policy/owner or expands caps. The prelaunch diagnostic preservation flags describe that diagnostic interval, not a claim that the later campaign leaves artifacts unchanged.

The 60-second handoff window is separate from diagnostic duration and the later native campaign deadline. The helper's child diagnostic and campaign wait do not add an independent process timeout; the actual campaign retains its approved native phase/cleanup and original six-hour limits. Observation timeout does not authorize relaunch, replacement, or a new claim.

## Invocation and readiness semantics

Use the existing worktree as working directory and the existing launcher for both modes. Argument vector:

`["C:/WINDOWS/py.EXE", "-3.13", "-B", "<absolute concrete-c-c/run-approved-context-handoff.py>", "source" or "receiver", "--approved-helper-sha256", "4e801fc0cf381b520ef147b07aa9f91bbc6edec62c058c9a9d2e3d6acfeb80ff", "--approved-review-sha256", "<exact SHA-256 of this completed review>"]`

Start source once in the preparation context. It performs genuine composition first, then publishes `context-handoff-descriptor.json` only after creating the listening endpoint. **Descriptor existence while the source process remains alive is readiness for the receiver.** Source does not print a separate ready line; its success line appears only after the handoff acknowledgement. Do not wait for source completion before starting receiver. Start receiver once in the network-enabled context promptly within the 60-second handoff window. Keep its actual execution session as the single campaign-observation session. The source exits after handoff; that is not campaign completion. Receiver output/exit and `actual-context-handoff-result.json` report the campaign child's outcome.

Supply this completed review's actual hash to both modes. Before the pair, retain the approved current-file checks already required by the concrete launch review. Do not delete or overwrite descriptor, claim, or result after failure; do not run trials or duplicate either invocation. If readiness, handoff, or diagnostic fails, preserve the actual result and stop.

## Scope and limits

The earlier actual launch reached no enrollment/launch/round/paid reservation; only its one-byte transition-lock marker was added. The current correction reuses that same campaign, original grant, limits, immutable policy/config and owner. It does not spend the earlier unrelated five-call remainder or establish a new grant. Credential resolution remains inside the already reviewed launch helper after the receiver's proof. No environment values or credentials are published by this handoff.

Review was static source/diff/hash and necessary protocol/property inspection. No mode, diagnostic, project import, pipe, credential resolution, test, synthetic trial, provider/Docker/evaluator operation, or campaign run was executed by this reviewer. Successful IPC/network access, actual provider/model availability, and eventual campaign outcomes remain unproven. No broader Windows/platform audit or production change was performed.
