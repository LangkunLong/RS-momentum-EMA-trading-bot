# Local work preserved on 2026-09-18

This archive preserves 727 distinct previously ignored files from local worktrees and V5 development directories. It contains helper source, progress ledgers, reviews, patches, and development documents. It does not replace the current [paused V5 handoff](../../handoffs/2026-09-09-pit-optimizer-v5/README.md).

The [index](INDEX.json) maps all 1301 inspected file occurrences to their original paths, hashes, and either an archived copy or an existing blob in main history. 762 occurrences are archived; 539 already exist in main history. Repeated contents share one archive file.

## Reading and restoring records

Files retain their original content and use a `.reference` suffix so historical scripts cannot be mistaken for active application modules. File names are their archived SHA-256 hashes. Use `INDEX.json` to find an original path and its `archive_path`. A history-only entry can be read with `git show <main_blob>`; its original local bytes may differ only in Windows line endings.

Three historical review patches have one credential-like literal redacted apiece. Their index entries retain both the original source hash and the archived hash and identify the redacted line. All original local files remain unchanged. All other archived copies retain exact original bytes.

These are historical records, including superseded plans, source preimages, review failures, and old instructions. Archiving does not endorse or execute their instructions. The original datasets, environments, caches, provider responses, and runtime state remain local. The dataset-dependent V5 worktree must be retained at its original path; this archive alone cannot recreate the prepared campaign.

## V5 campaign helpers

- [acquire-fmp-index-sources.py](files/85e6a0bfe0385b0c28de4d356d2bf5da27e8312c036551cb5c8699f586ac7df6.py.reference)
- [acquire-sec-sic-sources.py](files/8d967159ffcf86dd17c1307aa363a321230d2012429d41f1d5a2015994c5d669.py.reference)
- [acquire-v5-reference-prices.py](files/ad4eb90881b88082e60245da3f35fc86c23b16d6c38537e7ba94df6aa570dce2.py.reference)
- [build-exact-evaluator.py](files/44f862ce0aa2d5aa6de4305bfef360cb4583bed23e33239d1dbeedea9ff80f19.py.reference)
- [compare-development-runs.py](files/2895a06b1a16400ecb62c75caf89704bef9b8cd1f99ec49dc810465049f69b5e.py.reference)
- [diagnose-data-access.py](files/b679f99fb12e974e7d9adea23b8f64e5e00821753327e5058d3884d9a809d23b.py.reference)
- [import-controller-response.py](files/2e5b6fe03dab70d988dbb611702489ef45ae79170886e5b2c56994bf177e5d3c.py.reference)
- [import-local-v5-sources.py](files/23a0a7e8e0a751cef8c910c72704c9171c1947c69a2846fbe622ed071d6dd1e5.py.reference)
- [import-v5-cache-material.py](files/887a35fb2764ca4ce5aed21c2b4701e2cb2a12ae38b79edcacca54577d259421.py.reference)
- [inspect-owned-controller-container.py](files/1a788bd1dc62e3328e629b4314f2ab10d654d8a6beb7f4a32d6655bb4c1e2fcf.py.reference)
- [launch-development-panel.py](files/5c959e77064cdba16c991e8b02f01bf995c29ed2c94e71a9d7599bd18638e0e0.py.reference)
- [nasdaq-acquisition-audit.py](files/54e20ff31226fdc9db20d790b085a42eb49938c82362c5b4536ea295e7b747e6.py.reference)
- [nasdaq-acquisition-download.py](files/3230dbe2f426ede93fa84621454665b8574e2e21cebafeec261653e92937883a.py.reference)
- [nasdaq-acquisition-supplement.py](files/0ce21069eda55a1bc96934feb3b84b5254a5fee8ac57079947e6b244fa88c7e5.py.reference)
- [nasdaq-acquisition-ticker-evidence.py](files/97ef4dcc448d45ce4bab0d19e48085bc0589c829f8e76ca0c5c1ddf87c3a401e.py.reference)
- [prepare-controller-runtime-03.py](files/4d69dc15c3d84d58ce5ce05cdcd4d6ec3cda40d6e4446ddbe7694ce76b449f7e.py.reference)
- [prepare-controller-runtime-04.py](files/e5eddcf397b5b37b50b738ba3e221c215a0d3d0efa5aa47ecaa0a9f7092c37c2.py.reference)
- [prepare-development-prices.py](files/75c6f6cb434f14086f15b9974853159ef8f23d0f8a27b0a0f4c25e65cb3156f7.py.reference)
- [prepare-provider-development.py](files/0be7c4d12c63decb5ff3d08828d16c88918ff43bfa114d121b811dcc8c09b010.py.reference)
- [record-evaluator-image.py](files/f70c6fbc96f37cd1cf51574c1a40698918d5adb7e13ce8668d60ba105ec1d099.py.reference)
- [record-v5-acquisition-status.py](files/8e983eba4462da45cd2f77b00eedb5e5e14ef8dde58451edd20d7887ab45c3c4.py.reference)
- [record-vendor-inquiry-submissions.py](files/13f3891f306dbf484ca600e8e01ba8ed42f698e745d656440512aa6e67aa5382.py.reference)
- [refresh-development-baselines.py](files/b341017b182a5a248c2f4621127627a7b92a11cd981c2360f1a6faa40803613e.py.reference)
- [resume-controller-campaign.py](files/0368357d5c0b86b35f876829416977762070c837f8b7e0e66d1504b99afedca2.py.reference)
- [run-development-feedback.py](files/012691d3e32244c5e17f7aca616b0672964ac7f9513c2edc6ab3ce4865a5f913.py.reference)
- [seal-built-evaluator-profile.py](files/8fc8cb54d97fae78ad189235abc4bb5962ed46a8a90440d88f129063196f62bf.py.reference)
- [seal-development-run.py](files/bd2c5566aebb7341312867eab1684f077137f8c310e5f0ba1b8d9a65171d4ecf.py.reference)
- [seal-v5-preparation.py](files/4edf98a30516426342f229863394c897537028f7d8a0d8fd171d392ab7d3dffd.py.reference)
- [task-3-direct.py](files/ea4ae6b7068a9796450b9a6a645ef096d8f43e34755258d0d632373703bf982a.py.reference)

## V5 progress ledgers

- [2026-09-04-pit-optimizer-v5-evaluator-truth](files/ccb7f6219b8273f98c963e327bc95cfa293604def744c63c18ff3335f115a923.md.reference)
- [2026-09-04-pit-optimizer-v5-learning-search](files/d725b85dc98833717ff2ce73c86c4e945b8db2689ddea7bf1affecb5194555a1.md.reference)
- [2026-09-04-pit-optimizer-v5-roadmap](files/24110407c02b3ae67003dbfd5bfc2f8dcd4e0e2c1164ccfb02947b984cc56c73.md.reference)
- [2026-09-04-pit-optimizer-v5-strategy-universe](files/aeed8f4bb96bbb65656f628e01f344936052928805ef8537cb86bd24880a0b91.md.reference)

## Verification

Every source was rehashed before capture, and archive objects are identified by their SHA-256 hashes. A credential-pattern scan covered new archive candidates; the three flagged literals were redacted. This is a targeted scan, not a guarantee that all sensitive content is detectable.

In-memory syntax compilation checked 112 unique archived Python sources. No syntax errors were found.

No archived helper, campaign, trading operation, or test was executed. The pre-commit exclusion applies only to immutable archive objects; the index, README, and repository configuration remain checked. Dataset bytes are not part of this archive.
