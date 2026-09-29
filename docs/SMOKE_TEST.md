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

See section below; filled by the run that produced this commit.
