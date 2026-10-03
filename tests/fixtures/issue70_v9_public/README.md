# Public Issue 70 v9 synthetic fixtures

These fixtures are small, controlled test inputs for generic date parsing, file-hash binding, and selected-member identity comparison. They are not an adopted exchange calendar, SEC member inventory, or evidence about any issuer.

- `synthetic_calendar.csv` contains three ordered dates used only to exercise `_calendar_dates` and `_validate_bound_file`. It does not claim to be an XNYS calendar or the accepted 4,024-row calendar.
- `synthetic_selected_member_manifest.json` contains 10 fabricated Submissions identities and 4 fabricated CompanyFacts identities. `CIK99999…` names and repeated hexadecimal digests are test values. The fixture is intentionally shaped to exercise same-basename members in different archive namespaces; it is not a substitute for the pinned 14 real selected-member identities.

The public test module normalizes fixture text to UTF-8 with LF endings, pins that canonical byte length and SHA-256, and validates the canonical copy. This keeps fixture identities stable across checkout line-ending settings. The complete adopted-calendar decision, 4,024-session check, retained-window overlap, real-member pins, and CLI admission failures remain in the retained exact-input suite.
