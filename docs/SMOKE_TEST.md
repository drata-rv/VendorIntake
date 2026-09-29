# Controlled tenant smoke test

Run once per tenant before publishing the form or enabling updates. Use a non-production tenant, or a clearly labeled real test vendor with authorized manual cleanup (no delete scope is granted). Offline preview never writes. Record date, base URL, tenant ID, image version, fields tested, sanitized outcomes, created vendor ID. Never record credentials or raw responses.

## Procedure

1. Connection page: test candidate key. Expect account name/domain/ID, `Read verified`, custom-field definitions when enabled. Confirm account, attest create scope, save.
2. Form page: save, preview (banner "Preview only — nothing sent to Drata."), publish.
3. Submit a vendor named `BRIDGE TEST <timestamp>` with URL on a unique hostname, explicit Yes/No answers, contact email. Expect `CREATED`, numeric Drata ID, read-back status `PROSPECTIVE`, marker present in `notes`.
4. Replay the identical request with the same `Idempotency-Key`. Expect HTTP 200, same submission, `attempts` count unchanged, no second vendor. Same key with a changed body: `409 IDEMPOTENCY_CONFLICT`.
5. Custom fields (if enabled): include one fixture per enabled type (`TEXT`, `LONG_TEXT`, `NUMBER`, `URL`). Confirm read-back with `expand[]=customFields`.
6. Updates (if needed): submit the same vendor name/URL again with different services and contact email. Expect `NEEDS_REVIEW / DUPLICATE_SUSPECTED` and no POST. Update-preview against the step-3 vendor selecting contact email and services only; confirm. Verify those fields changed and every other seeded field and `PROSPECTIVE` status did not. Enable updates only after this passes.
7. Replace the credential with a same-tenant key and retest. A wrong-tenant key must be rejected. Revoke the retired key in Drata.
8. Restart the container: history persists. `backup`, restore into an isolated instance, run `restore-check`: decryption and login recovery work; writes stay disabled; no outbound calls.

Failure paths (timeouts, 5xx, 401/403, 429, malformed responses) run against fake HTTP in `tests/`, not against the tenant.

## Result log

Run 2026-09-29 by the implementing engineer, in-process (Flask test client over the real Drata adapter), SQLite on local disk. No container image was built or run (Docker not available on the authoring host).

| Item | Value |
|---|---|
| API base URL | `https://public-api.drata.com/public/v2` |
| Tenant ID | `5f1f8fba-9139-4dfc-870f-c98519a0edb1` (SOCpilot, Inc) |
| Image version | none (source tree at the commit that added this log) |
| Created vendor | 1190, `BRIDGE TEST 20260929-041255`, `PROSPECTIVE`. Left in tenant; archive manually (no delete scope) |
| Custom fixtures | 169 `TEXT`, 193 `LONG_TEXT`, 183 `NUMBER` |

| Step | Outcome |
|---|---|
| 1 Connection test | `Read verified`; company, list, get-vendor, definitions all 200; account matched; no credential in `GET /api/admin/connection` |
| 2 Form | Save, offline preview (banner present), publish OK |
| 3 Create | 201, `CREATED`, ID 1190. Read-back: status `PROSPECTIVE`, marker in `notes`, name/url/privacyUrl/contact/booleans/category/type/services/dataStored match, custom values read back (`value` present; NUMBER returned as JSON number 12.5) |
| 3a First read-back | `VERIFY_MISMATCH` on `contactEmail`: Drata lowercases the whole address (sent `Bridge-Test@Example.COM`, read `bridge-test@example.com`). Compare made case-insensitive; reconcile then moved the record to `CREATED` with no second POST |
| 4 Replay | Same key: 200, same submission, 1 attempt row. Same key with changed body: 409 `IDEMPOTENCY_CONFLICT` |
| 5 Custom fields | `TEXT`, `LONG_TEXT`, `NUMBER` verified. `URL` not verified: tenant has no `URL` definition |
| 6 Duplicate | Same name and host: `NEEDS_REVIEW` / `DUPLICATE_SUSPECTED`, candidates NAME+HOST, 0 POST |
| 7 Update | Preview diff for contact email and services; confirm: 200, `UPDATED`. All other seeded fields and `PROSPECTIVE` status unchanged. Update scope present on the key |
| 8 Credential retest | Stored credential retest OK, version unchanged, `Write observed` true. Wrong-tenant rejection covered by fake in `tests/test_access.py`, not live |
| 9 Restart | Second app instance on the same database ran startup checks; history intact |
| 10 Backup / restore | Backup mode 0600; `restore-check` on the copy: sessions cleared, writes disabled, connection `RESTORED_UNVERIFIED`; copy decrypts; no outbound call (factory stubbed to fail) |

Not exercised: container build/run, HTTPS ingress, `URL` custom type, live wrong-tenant key, live failure paths (fake HTTP in `tests/`).
