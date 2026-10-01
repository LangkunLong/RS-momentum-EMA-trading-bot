# Fiserv listing identity source receipt v1

Assessment date: 2026-10-01

This receipt supplements the earlier [price identity transition evidence ledger](price-identity-transition-evidence-ledger-v1.json). It records rights and byte-retrieval status for the cited FISV→FI→FISV issuer evidence. It is not a price identity segment contract, production transition, or membership artifact.

## Rights and retrieval

The SEC says government-created and EDGAR public filing content is free to access and reuse. Its access guidance asks scripted clients to identify a user agent, download only needed material, and remain under 10 requests per second. A direct raw-byte GET for the 2023 Exhibit 99.1 URL returned HTTP 403 on 2026-10-01; no body was saved. The 2025 SEC filing was not raw-byte captured after that failure. The official web reader exposed the filing text for inspection, but that rendered text is not a byte receipt.

Fiserv's public Terms of Use, last updated 2025-04-10, allow viewing, printing, and downloading site materials for internal informational use while preserving notices; copying or distributing those materials outside the organization requires prior written permission. The cited investor relations page is publicly accessible and the retained copy is for internal audit only. This does not authorize external distribution or license a broader historical membership database.

| Source | Raw-byte receipt | Authority / scope |
| --- | --- | --- |
| Fiserv 2025-10-29 listing-transfer release: [official page](https://investors.fiserv.com/news-releases/news-release-details/fiserv-announces-transfer-stock-exchange-listing-nasdaq) | Retained privately at `.artifacts/data/acquisition/acquisition/raw/source-evidence/fiserv-listing/fiserv-ir-2025-10-29-listing-transfer.html`; 40,553 bytes; SHA-256 `4e4e6e92fed77e8771cf195f3578f04589a93c501dd67c7eb8ca9e27976f657e`; retrieved 2026-10-01 12:38:45 UTC. | Fiserv IR, page title “Fiserv Announces Transfer of Stock Exchange Listing to Nasdaq.” Full-release opening paragraph states the Class A common stock is expected to begin Nasdaq trading on 2025-11-11 under FISV. This supports the announced second edge, but the retained page alone does not prove completed trading on that date. Internal-use rights follow the Fiserv terms below; raw file stays in ignored `.artifacts`. |
| Fiserv Terms of Use: [official terms](https://www.fiserv.com/en/about-fiserv/terms-of-use.html) | Retained privately at `.artifacts/data/acquisition/acquisition/raw/source-evidence/fiserv-listing/fiserv-terms-of-use.html`; 107,150 bytes; SHA-256 `3f4c76749fd156f28275bd00610448cf344bde48f70befc34d3d1c43c3b264b9`; retrieved 2026-10-01 12:38:46 UTC. | Terms page says last updated 2025-04-10. The internal informational-use permission requires copyright/proprietary notices to remain intact and does not authorize external copying or distribution. |
| Fiserv 2023-05-25 transfer-to-NYSE release: [official page](https://investors.fiserv.com/news-releases/news-release-details/fiserv-transfer-listing-new-york-stock-exchange) | Browser-readable text verified, but an attempted raw-byte capture hung and left no retained file or SHA-256. | The issuer states its common stock was Nasdaq FISV and expected to begin NYSE trading on 2023-06-07 as FI. This is a primary-source lead for the first edge, not a source-byte assertion. |
| SEC 2023-05-25 Form 8-K Exhibit 99.1, accession `0001193125-23-154199`: [official filing exhibit](https://www.sec.gov/Archives/edgar/data/798354/000119312523154199/d470900dex991.htm) | Raw-byte attempt returned HTTP 403; no retained file and no SHA-256. Do not use the web reader's rendered text as a substitute. | Exhibit 99.1, opening news-release paragraph after the title. It identifies Fiserv common stock as Nasdaq FISV and says NYSE trading under FI is expected to begin 2023-06-07. |
| SEC 2025-11-10 Form 8-A, accession `0001193125-25-274207`: [official filing](https://www.sec.gov/Archives/edgar/data/798354/000119312525274207/d53898d8a12b.htm) | No raw-byte capture attempt after the preceding SEC 403; no retained file and no SHA-256. | Explanatory Note and common-stock listing table state NYSE listing ceases on or about 2025-11-10 and Nasdaq trading under FISV begins at market open on or about 2025-11-11. |

The SEC rights basis is its [Webmaster FAQ](https://www.sec.gov/about/webmaster-frequently-asked-questions) and [EDGAR access guidance](https://www.sec.gov/search-filings/edgar-search-assistance/accessing-edgar-data). The Fiserv rights basis is the Terms of Use linked above. No provider contact, form, account, purchase, or restricted-page access was used.

## Reconciliation to the retained price identity request contract

The original retained price provenance is `C:/Projects/trading_bot/RS-momentum-EMA-trading-bot/.artifacts/data/acquisition/acquisition/raw/local-cache/prices/prices_provenance.json`, SHA-256 `7fca2de6d408e232bca560f1df10aeae88433f3a58e1bb64f7bef20944f95ca5`. Its canonical `price_identity_request_contracts` contains 607 keys, digest `273727c248f57b7376b6cf269312325cdd059287e1f4ed16ab5ce63617ceabc7`; the pinned evidence ledger records the digest verification as true.

| Parent identity | Current request-contract interval | Relevant fields |
| --- | --- | --- |
| FISV | 2020-01-01 through 2025-12-31 | `chain_id=fiserv`; `continuity_kind=same_issuer_ticker_reuse`; `factor_anchor=true`. |
| FI | 2023-06-07 through 2025-11-10 | `chain_id=fiserv`; `continuity_kind=same_issuer_ticker_reuse`; `warmup_predecessor=FISV`; `factor_anchor=false`. |

The parent rows overlap from 2023-06-07 through 2025-11-10. The source chronology requires three nonoverlapping symbol episodes: FISV through 2023-06-06; FI from 2023-06-07 through 2025-11-10; and FISV from 2025-11-11 through 2025-12-31. A future segment contract must split the one FISV parent identity into those two episodes, preserve the first FISV episode as the sole factor anchor, retain `chain_id=fiserv`, and bind to the exact 607-key parent digest above. These intervals are recorded as the intended reconciliation only; no segment rows or transitions were emitted.

## Remaining evidence and admission boundary

- Exact SEC source bytes, hashes, and retained paths are still missing for both SEC documents. The 2023 request received HTTP 403; the 2025 SEC document was not raw-byte requested. The 2023 issuer page is browser-verified but likewise has no raw-byte receipt. Rendered text is reference-only.
- A complete segment assertion set must bind each source URL/accession, document date, exact document locator, effective date, supported same-security statement, source-byte SHA-256, and retained source-document path. The retained 2025 Fiserv IR bytes support its announced second edge; the first edge lacks any retained primary source bytes, and completed effective-date corroboration for both edges remains to be reviewed. The cited SEC filings or equivalent authoritative records could supply that corroboration if their exact bytes are available under the applicable terms.
- The existing price provenance still contains no `price_identity_segments_v1` object or `price_identity_segments_v1_sha256`; it also has no admitted `price_identity_transitions` list. The 2025 issuer release copy and terms snapshot remain private ignored files, not Git-tracked source content.
- Do not emit either Fiserv transition or a segment contract until every required primary-source assertion is byte-backed and reviewed against the 607-key parent contract. V3 production membership remains rejected pending a reviewed provider-native adapter; V5's approved evidence-pair set remains empty.
