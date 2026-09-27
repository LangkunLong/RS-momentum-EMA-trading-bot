# Historical provider incident index — issue #89

**Scope:** read-only index of four retained primary study attempts and eight exports. This index does not settle historical billing, reopen studies, authorize provider activity, or modify the retained evidence.

The current retained artifact root is .artifacts. The September 26 artifact README says its four study roots and eight exports were verified during relocation, with 3,843 selected files hash-checked. The original ignored evidence remains in the original checkout at C:\Projects\trading_bot\RS-momentum-EMA-trading-bot\.artifacts; it is absent from this issue worktree. The README explicitly identifies new-study, new-time-zero-export, and new-incomplete-export as study three.

The logical study_id value study-v5-two-round-example is reused across the records. It does not uniquely identify a study. Distinguish each attempt using its retained root, manifest SHA-256, store/root identity SHA-256, and request identity.

## Primary attempts

### 1. Study one — first live study

- **Retained root and exports:** evidence/unresolved-studies/study-one/; study-one-original-export/ and study-one-later-export/.
- **Incident:** evidence/incidents/2026-09-22-v5-first-live-study/.
- **Identity:** logical study ID study-v5-two-round-example; manifest ecbc73fb668050813f557060ebf916e823c31b2dbdd860da6faea800f1938eae; store/root 19d6d4b31d1bea76170588381206eea34048d78ccd376cb9949497f157143a76; issued grant fcd508f530a59a09894fee44eb716f6e6cb5b8ee409b15d097ff65957bc30f5; primary L request 014d048da95b185ed40c568afc62283f0bf3498b5f6d1bf5ce6dca6d64b9593e; pending reservation 111236eca8f9390b9c44169987f05c90c6fe8e41be22cf371aecae1598b78bd1.
- **Outcome:** one primary reservation and dispatch claim; no response, raw response, reconciliation, or terminal. The record does not establish provider receipt. No provider request ID is recorded. The withheld arm was not dispatched. Actual usage and cost are unknown.
- **Accounting note:** the persisted reservation is prospective and was calculated as $0.000000653315 because the grant used per-token price values where the ledger expected per-million values. The incident report says this is a factor of one million below the intended $0.653315 estimate. Neither value establishes actual spend.

### 2. Study two

- **Retained root and exports:** evidence/unresolved-studies/study-two/; study-two-pending-export/ and study-two-time-zero-export/.
- **Incidents:** evidence/incidents/2026-09-23-study-two-admission/, evidence/incidents/2026-09-23-study-two-live/, and evidence/incidents/study-two-closeout/.
- **Identity:** logical study ID study-v5-two-round-example; manifest 59aa27c117f7fa5c68685e965feb4027fc7e3751793a386dc6f16583e55532d8; store/root cb34d15a46acbc9365d7625888f7a7dbe78c9ed3aad6111a4010284e55cd4e7e; grant 56789b731e3929ac9913a05dc42d9e7cd838f545f3e6ccb7ee98ec8069a2298f; primary request 53ba33524f5e250061e9728439487326e43b2860b9606fcd49e2a96683d8548d; reservation 9a00922710a75004cd112d7e55bc86ac590d3ed3750adf5acaa65d7929a539a6.
- **Outcome:** primary accounting remains pending and the retained primary review records a null provider request ID. The withheld arm was not dispatched. Actual usage and cost are unknown. The admission packet is a separate proposal record; use the live and closeout records for the attempted-primary outcome.

### 3. Study three — new-study

- **Retained root and exports:** evidence/unresolved-studies/new-study/; new-time-zero-export/ and new-incomplete-export/. The retained README assigns these names to study three.
- **Incident:** evidence/incidents/2026-09-23-new-pair-execution/.
- **Identity:** logical study ID study-v5-two-round-example; manifest 16e63e3d57ad83c2dbaf38dbd4bc62b9eab3476ed8b4fd5c963e7a9e63379960; store/root 274404046f9c578dd9967668c4af09e6fe3bbb0877c7fa7e04e27318e38a4fc8; grant 6d6d6cb5dc937c4cd7a53fb7a9b90346ddd732c23fd774212467c1d25e7a9a81; primary request 19e3fa5ec3c174d4beed96233eabdd6bc2c8baac896dc4f0d786de58264f98f6; reservation cac8b8fcd00a4dc3de49034b3b72ba7071d475c38196d6cf30b24e93f83a0e8a; recorded provider request ID gen-1790208144-uE7ovkWeEnY5I14Wy1ef.
- **Outcome:** one authorized primary dispatch stopped during response extraction with pending accounting. The withheld arm was not started. Actual usage and cost are unknown.

### 4. Study four

- **Retained root and exports:** evidence/unresolved-studies/study-four-full-root/; study-four-incomplete-primary-export/ and study-four-time-zero-export/.
- **Incident:** evidence/incidents/study-four-execution/.
- **Identity:** logical study ID study-v5-two-round-example; manifest d60db4a101e9af4692afdd0425d1720eefeb488d370331390002ed1a7220b3ff; store/root f9f391ff623c5e62422ffb8f1821db47304c441a2009d7a8f5f729eaa6d054ee; issued grant 9159b09c3d7f0760f230864e8c69078cab25bb9ea26e04517784455e3529a80f; primary L request fc378dc94956a9606f071b09a2e342822be19b13041b837af583ce31389528c2.
- **Outcome:** the one primary attempt ended with api_connection_error, null HTTP status, null provider request ID, no terminal, and pending accounting. Delivery and charge are not established. The withheld arm was not started. Actual usage and cost are unknown.

## Export count and accounting

There are exactly eight named exports: two for study one, two for study two, two for study three (new-study), and two for study four. These are export variants of four roots, not eight attempts. A retained withheld descendant or export is not evidence that a withheld provider call occurred. All four withheld arms remain unstarted.

Actual primary usage and cost remain **unknown for all four studies**. Missing responses, pending ledgers, failed or unavailable lookups, request limits, reservations, and caps do not establish zero usage or a final bill. The records contain prospective figures, including the study-two $1.228030 pair estimate and the study-three $1.346685 cap; these are planning values, not charges or verified aggregate spend bounds.

The separate diagnostic in the study-four incident settled at $0.000470 for 68 input and 20 output tokens, with diagnostic provider request ID gen-1790220560-9GFqCyI55Ja2WuHlZlSL. Keep that diagnostic attributed only to its own operation; it does not settle or reduce any study's unknown primary cost.

No reconciliation was performed for this index. Any future reconciliation requires separate authorization and authoritative provider evidence that matches the specific primary request and relevant account/project. Preserve the original records and leave a study unknown if the exact identity cannot be matched. An aggregate account total or a prospective hold is insufficient.

## Four separate issue statuses

- **Implementation:** this issue deliverable is a documentation index; no provider implementation change was needed.
- **Required inputs:** the retained artifact root and incident records are available in the original checkout according to .artifacts/README.md and the relocation manifest. They are not present in this issue worktree.
- **Acceptance evidence:** targeted reads of the named incident records support the identities and states above. The relocation README reports the saved 3,843-file hash verification. This index did not recalculate those hashes or change evidence. Independent acceptance review remains open; the saved issue status is not changed by this document.
- **Dependencies:** ready to start as recorded in the issue register. Historical billing reconciliation is not required to accept a truthful index.

## Records reviewed

- .artifacts/README.md
- .artifacts/evidence/incidents/2026-09-22-v5-first-live-study/stage2-proposed-grant.json
- .artifacts/evidence/incidents/2026-09-22-v5-first-live-study/stage2-issued-grant-readback.json
- .artifacts/evidence/incidents/2026-09-23-study-two-live/principal-primary-pending-review.json
- .artifacts/evidence/incidents/2026-09-23-new-pair-execution/primary-attempt-record.json
- .artifacts/evidence/incidents/2026-09-23-new-pair-execution/principal-post-primary-seal.json
- .artifacts/evidence/incidents/study-four-execution/primary-attempt-closeout.md
- .artifacts/evidence/incidents/study-four-execution/primary-wrapper-claim.json

2026-09-23-study-two-admission/admission-packet.md is retained as a nonissued proposal, not as proof of an attempted primary. The current primary state is taken from the separate live and closeout records.
