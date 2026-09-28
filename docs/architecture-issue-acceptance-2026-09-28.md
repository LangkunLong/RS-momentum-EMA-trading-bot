# Acceptance of five architecture issues — 2026-09-28

The owner accepted issues [#66](https://github.com/LangkunLong/RS-momentum-EMA-trading-bot/issues/66), [#80](https://github.com/LangkunLong/RS-momentum-EMA-trading-bot/issues/80), [#81](https://github.com/LangkunLong/RS-momentum-EMA-trading-bot/issues/81), [#89](https://github.com/LangkunLong/RS-momentum-EMA-trading-bot/issues/89), and [#97](https://github.com/LangkunLong/RS-momentum-EMA-trading-bot/issues/97) for their published definition or evidence-index deliverables. This record maps their issue criteria to the published artifacts. It does not accept full historical acquisition, a current evaluator run, provider billing reconciliation, paper action adapters, or a ready paper runtime. [#107](https://github.com/LangkunLong/RS-momentum-EMA-trading-bot/issues/107) remains open.

The source review started at `c628a3af3c2c14dd684340d1b695ce91f8438842`, where the earlier research integration `1888016ccd6eac98aae946a47f9abb2752a01a8b` was already on main. The accepted #80, #81, #89 and #97 artifacts came from their named issue branches and bounded cross-reviews. #66's owner-reviewed local specification is published unchanged in its technical choices as [the tracked historical feature specification](historical-feature-specification-v1.md); only its acceptance/publication wording was updated.

## Criterion-to-evidence map

| Issue | Published criterion | Published evidence | Disposition |
| --- | --- | --- | --- |
| #66 | 1. Source/publication, units, lookback and missingness for every feature | [§§3–6 and 8](historical-feature-specification-v1.md) | Accepted definition |
| #66 | 2. Industry, sector, ownership quantity and optional sponsor/event scope | [§§5 and 8](historical-feature-specification-v1.md) | Accepted definition |
| #66 | 3. Production versus limited-development claims | [§§2 and 7](historical-feature-specification-v1.md) | Accepted definition |
| #66 | 4. Annual revenue/raw financials, ownership detail, float proxy and catalyst proxies | [§§4–5 and 8](historical-feature-specification-v1.md) | Accepted definition |
| #66 | 5. Quarterly/annual reports, Q4, foreign forms and earliest earnings releases | [§§3–4 and 9](historical-feature-specification-v1.md) | Accepted definition |
| #80 | 1. Six input/decision/state categories | [§§3–5](strategy-policy-contract-v1.md) and [deterministic fixtures](../tests/test_strategy_policy_contract_fixtures.py) | Accepted definition |
| #80 | 2. Strategy choices versus feature/accounting/execution invariants | [§§2–5](strategy-policy-contract-v1.md) | Accepted definition |
| #80 | 3. Historical/paper review and intentional simple baseline | [§§4 and 7](strategy-policy-contract-v1.md), [#97 crosswalk](qualified-policy-deployment-contract.md) | Accepted definition |
| #80 | 4. Optimizer 5, policy 3, PIT 2/3 and source-to-policy mapping | [§§1–2 and 6](strategy-policy-contract-v1.md) | Accepted definition |
| #81 | 1. Accepted integration and excluded local artifacts | [source identity and evidence map](research-reproducibility-index.md) | Accepted index; merge was already complete |
| #81 | 2. Source, interpreter, evaluator-image receipts and modes | [runtime, image and mode sections](research-reproducibility-index.md) | Accepted index; current image availability unverified |
| #81 | 3. Public versus local-only links and source-bound results | [evidence and path-resolution sections](research-reproducibility-index.md) | Accepted index; old PASS remains source-bound |
| #89 | 1. Four attempts and eight retained exports | [primary attempts and export count](historical-provider-incident-index.md) | Accepted index |
| #89 | 2. Unknown cost is not zero | [accounting section](historical-provider-incident-index.md) | Accepted index; all four primary actual costs unknown |
| #89 | 3. Optional future authoritative reconciliation | [reconciliation conditions](historical-provider-incident-index.md) | Accepted index; no retry authorized |
| #89 | 4. Four identities and unstarted withheld arms | [primary attempts](historical-provider-incident-index.md) | Accepted index |
| #97 | 1. Support status for all six actions | [§3 support matrix](qualified-policy-deployment-contract.md) | Accepted definition; current V3 paper adapters remain incomplete |
| #97 | 2. Host guards versus strategy decisions | [§4](qualified-policy-deployment-contract.md) | Accepted definition |
| #97 | 3. Selection, open holdings/pending orders and rollback aligned to #80 | [§§1, 5–7](qualified-policy-deployment-contract.md) | Accepted definition; no policy activated |

The #66/#80/#97 definitions agree on source-public date versus first eligible session, detailed-to-concise missingness with retained provenance, distinct version identities, the six policy decisions, and the V5 scale-out quantity basis. The source-public rule is settled; each adapter still must normalize its `public_date` representation exactly once. Proposed paper reservation and fill/tier rules are contract examples, not runtime evidence.

## Four separate status dimensions

| Issue | Implementation | Required inputs | Acceptance evidence | Dependencies |
| --- | --- | --- | --- | --- |
| #66 | Definition complete | Existing architecture/source/evidence available | Five criteria accepted | No issue-level start prerequisite; #67–79 consume the definitions |
| #80 | Shared definition and fixtures complete | Existing V3/V5 contracts and #66 source meanings available | Four criteria accepted; bounded fixture verification | No issue-level start prerequisite; later evaluator/paper work consumes the contract |
| #81 | Reproducibility index complete; source integration pre-existed | Retained original-checkout evidence available; current evaluator image availability unknown | Three index criteria accepted; no current evaluator execution claimed | No issue-level start prerequisite; #82–83 retain their own gates |
| #89 | Historical incident index complete | Retained local incident evidence available | Four index criteria accepted; four primary actual costs unknown | No issue-level start prerequisite or unrelated-work block |
| #97 | Deployment definition complete | Existing policy/paper architecture and #66/#80 contracts available | Three definition criteria accepted; no adapter/runtime acceptance implied | No issue-level start prerequisite; #98–106 and #109 retain their gates |

Research still requires accepted production data, baseline/evaluator evidence, controlled feedback, discovery, qualification and a separate replay before a candidate can be promoted. The paper execution path still requires current-data/account adapters, durable policy actions, compatibility checks and verified operations. Closing these five issues supplies definitions and indices to those later gates; it does not satisfy them by itself.
