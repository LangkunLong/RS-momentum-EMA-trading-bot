# Lead C source and import inventory

Status: frozen integrated application candidate `d2c4082746017dd4ecb14d709d71a7c12b1fc2d5`, tree `1e511574341e425f6ec2c326991a8dfaa6f49f38`. Final independent integrated specification and quality review approve all nine bounded offline criteria. This is not principal acceptance, evaluator-closure or image acceptance. Later documentation-only publication commits retain this validated application/test target and require their own exact changed-path payload inventory.

Current source/import inventory: `.artifacts/lead-c/source-inventory-d2c4082.json`, SHA256 `bbdf9ab669e8c740a1e48ba347d4e6e02bee7e7af1f062a4a765f9ab88bbbb57`. It records all22changed paths from acceptedab385d7, their committed bytes/hashes and static imports. No retained B-manifest path changed, and calculator blob `71e562ffc00dc4e617f1cd625e3f13ea5fe3f81a` is preserved. The four additive modules and their direct dependencies are:

| Module | Raw committed SHA256 | Direct application dependencies |
| --- | --- | --- |
| `core/current_policy_inputs.py` | `a1af15fd7885857535a63d9c7b12b3736fdf0b9179f950c29bc1b5976b41ab6b` | pit_data, accepted pit_feature_snapshot, pit_provenance, strategy_policy contracts and market_context; NumPy/pandas plus standard library |
| `core/policy_execution_state.py` | `739a8a1b1fcc0000d0db027bcf813dddfed069693160cc66a367bf6fc7913032` | Standard library only |
| `core/policy_execution_store.py` | `db630ca4d775b0c5ec395251762b7018cd6679e1a553a0121a137e7cb1c122af` | New policy_execution_state plus standard library/SQLite |
| `core/strategy_policy/account_reconciliation.py` | `a5d28579374cfdbfb3528c53aca0fd217b6b3cc386ce437fe59490f1f7604f7f` | New policy_execution_state, existing adapter_v3 and contracts_v3 plus standard library |

The matching API/schema inventory is `.artifacts/lead-c/store-api-schema-d2c4082.json`, SHA256 `28cd7fc40cbc88f2220aba7d8a34a542d7e2bb6ad0e18aaf511948be7be39d6b`. Existing execution-store/workflow and existing imports are unchanged. Six selected offline modules passed107cases and all312committed Python files compiled under Python3.13 at this exact application head; full evidence and warnings are in `.artifacts/lead-c/integration-validation-d2c4082.md`. These checks do not establish dynamic imported-graph closure or authorize a runtime migration.

## Historical checkpoints

Earlier integrated checkpoint evidence: `.artifacts/lead-c/source-inventory-a3ef2b7.json`, SHA256 `6268d967718ed36a0acb67b2c516f2723c061926f2470419c02fc2cf16217196`, for exact lead head `a3ef2b78985c2fc11ab705dc3d044fbdef48bbf4`. This includes approved #98, approved #99 baseline, frozen #100 store1847869 under correction, and lead integration tests. API/schema definitions are separately retained in `.artifacts/lead-c/store-api-schema-1847869.json`. These records remain historical, superseded by the current application candidate above.

Previous checkpoint evidence: `.artifacts/lead-c/source-inventory-8190c15-f9575a5-446d2e1.json`, SHA256 `aeeeaed50086b0dae97fe4267ab06e592a6edfafa9f3e560ffa21ffc1cde11d3`. It covers approved #98 head8190c15, approved #99 consumer fixf9575a5 (including inherited producer6bbf20a), and #100 pure fix3446d2e1. The repeatable raw-Git/AST inventory helper is `.artifacts/lead-c/build_source_inventory.py`; it imports no application modules and inventories committed source only.

Initial historical evidence: `.artifacts/lead-c/source-inventory-checkpoints-43b4cd9-6bbf20a.json`, SHA256 `59cea2b9a4c7e9695b88be8075662d0a7ad65e5dd01feda9be69d62504dfe32c`. The inventory reads exact Git blobs and parses Python imports without importing application code. It records every changed path, raw Git SHA256, Git blob identity, byte count and static import locations. The following initial-checkpoint hashes remain historical; use the latest evidence above for corrected source.

## Checkpoints

All comparisons start at accepted `ab385d792e19ff6db39d87f1123f47f660fc1e1d`.

| Checkpoint | Exact head | Changed paths | Mapped path changes |
| --- | --- | --- | --- |
| #98 initial implementation and report | `43b4cd9b97dd17ce50d3f194d15ad698a4113a5c` | New current_policy_inputs module, dedicated test and fixture, two docs | None against B's retained manifest |
| #100 pure producer | `6bbf20ab2de0f177628bf623c8fa3c138679d868` | New policy_execution_state module, dedicated pure tests, interface doc | None against B's retained manifest |

`core/current_policy_inputs.py` raw Git SHA256 is `3d237da2e75404fee928392efd4c783443ef35c4cc211b91e722663519c164fb`. Application imports are pit_data, pit_feature_snapshot, pit_provenance, strategy_policy.contracts and strategy_policy.market_context, alongside NumPy, pandas and standard-library modules. Existing imports and scanner source are unchanged in this checkpoint.

`core/policy_execution_state.py` raw Git SHA256 is `446a5ab9603ae1614407bf71612d3b4f6f11fa2744cc8f94974b8f04e922e8f6`. Its imports are standard-library only: hashlib, json, dataclasses, datetime, decimal, enum and typing. Existing execution store/workflow entry points are unchanged in this checkpoint.

Both retain accepted calculator Git blob `71e562ffc00dc4e617f1cd625e3f13ea5fe3f81a` for `core/pit_feature_snapshot.py`.

## B evidence boundary

Reference manifest: B commit `a7cb3f44c3cd1156a172cce6e52c0c17c6a88a78`, `docs/lead-b-evaluator-a866-context-manifest.json`; evaluator map identity `ea44827dc78dcc3f4a4293929e4897345d1f0daa7faef6fbd50cdbb00757d2af`. B's ledger records separately accepted source/image evidence at a866 and normal PR115 merge ed42aac. This record does not relabel that evidence as a result for C's final source.

No evaluator image, runtime, provider or broker operation was performed to build this inventory. Static import listings are not a dynamic graph proof. Principal owns collection of the final C inventory for B's actual imported-graph assessment; rejected peer messaging is not bypassed.

The current candidate inventory above includes the corrected #98 adapter, #99 consumer, #100 transitions/store and combined tests. Any subsequent application/test change requires a refreshed source-bound inventory and relevant verification. B determines whether any accepted image evidence is invalidated; no such decision is inferred from an empty mapped-path intersection.
