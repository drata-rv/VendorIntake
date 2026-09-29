# Operations

## Runtime

- One container, one Gunicorn worker, four threads. Never raise workers or run a second instance: the write lock is process-local.
- Durable state: `/data/bridge.sqlite3` (WAL). Key: `/run/secrets/bridge.key` (read-only mount).
- `GET /healthz` process only. `GET /readyz` database, schema, key loaded. Neither probes Drata.
- Logs: JSON to stdout. Fields: `ts, level, requestId, route, method, status, ms, code, submissionId`. No bodies, headers, credentials, answers, vendor names, SQL parameters. Gunicorn access log prints path without query string. Rotate at 30 days.

## Secrets

- Key generation: `umask 077`; `Fernet.generate_key()`; never regenerate on restart.
- Encrypted columns: `settings.api_key_enc`, `submissions.{answers,payload,snapshot,candidates}_enc`.
- Key rotation (offline):
  1. Stop container. Back up database and old key.
  2. Generate new key file.
  3. `docker compose run --rm -v $PWD/secrets/new.key:/run/secrets/new.key:ro bridge flask --app app rotate-key --new-key-file /run/secrets/new.key`
  4. Replace `secrets/bridge.key` with the new key (mode 0400, owner 10001). Start.
  5. Keep the old key while backups that need it exist.
- Lost key = encrypted rows unrecoverable. Decrypt failure stops the request; nothing is overwritten.
- Drata key replacement: Connection page, enter new key, confirm account, attest scopes. Wrong-tenant key is rejected. Revoke the old key in Drata after the new one works.
- Disconnect removes the stored credential and disables the form. It does not revoke in Drata.

## Accounts

- First admin: `flask --app app create-admin` (prompts, no echo).
- Requester accounts: Users page. Temporary password shown once; deliver out of band. Forced change at first login. Min 15, max 128 characters.
- Recovery: `flask --app app reset-password` (clears all login cooldowns and the user's sessions).
- Disabling a user or changing role deletes their sessions.
- Sessions: idle 30 min, absolute 8 h. Login cooldown 15 min after 10 failures per account or per source IP within 15 min. Credential changes need a login within 5 min.

## Submission states

| State | Meaning | Admin action |
|---|---|---|
| `IN_PROGRESS` | Being processed | none; restart converts (below) |
| `CREATED` / `UPDATED` | Read-back matched | none |
| `NEEDS_REVIEW` | Possible duplicate, local unresolved match, custom-field drift, verify pending/mismatch, target missing, multiple marker matches, restore reconcile | reconcile, link existing, confirm new, update, cancel |
| `NEEDS_CORRECTION` | Drata rejected input | requester resubmits; cancel |
| `BLOCKED` | 401/403/402/412 | fix credential/scope/terms, retest connection, retry |
| `RETRYABLE` | Evidence nothing was sent, or recognized 429 | retry (blocked while rate-limit not-before is in the future) |
| `UNKNOWN` | Write may have committed | reconcile, link verified record, cancel, or recreate with written reason |
| `CANCELLED` | Closed (`LINKED_EXISTING`, `CANCELLED_BY_ADMIN`, `RECREATE_AUTHORIZED`, `RETENTION_EXPIRED`) | none |

Startup: `IN_PROGRESS` with a dispatched attempt becomes `UNKNOWN`, otherwise `RETRYABLE`. Nothing is resent at startup.

Reconcile reads only. It matches the marker in vendor `notes`: one match verifies and closes; several match to `NEEDS_REVIEW`; none leaves `UNKNOWN`. Absence of a marker is not proof of no write.

## Retention

Defaults editable on the Connection page: encrypted payloads purged 30 days after terminal state; unresolved submissions cancelled with `RETENTION_EXPIRED` and purged at 90 days (warning from day 60); ledger and audit rows deleted at 365 days. `flask --app app cleanup` runs at startup, at most daily from an administrator page load, and on demand. Idle or stopped app needs manual invocation. Bridge deletion does not delete the Drata record.

## Backup

```sh
docker compose exec bridge flask --app app backup --output /data/backups/bridge-$(date +%Y%m%d).sqlite3
```

Uses the SQLite backup API, mode 0600, refuses to overwrite. Copy to encrypted off-host storage, delete the staging copy. Back up the key file separately. Keep 14 daily copies unless policy differs.

## Restore

1. Disable ingress, stop the container, confirm no second instance.
2. Restore database and its matching key with correct ownership (10001).
3. `docker compose run --rm bridge flask --app app restore-check` : clears sessions, sets connection `RESTORED_UNVERIFIED`, disables writes, moves `RETRYABLE`/`BLOCKED` submissions to `NEEDS_REVIEW` / `RESTORE_RECONCILE`. No Drata calls.
4. `docker compose run --rm bridge flask --app app cleanup`, then start.
5. Re-enter or retest the Drata credential (Connection page, "Use stored credential"), verify the pinned account.
6. Reconcile every `NEEDS_REVIEW` / `RESTORE_RECONCILE` and `UNKNOWN` row before any retry. A backup can predate a successful Drata write. A marker match verifies the vendor. No match leaves the row in review (absence is not proof): link a known vendor, cancel, or use "Confirm distinct new vendor" with a written reason (rescans before creating).

## Upgrade

1. Disable the form; wait for in-flight writes.
2. Back up database and key.
3. Stop; run `docker compose run --rm bridge flask --app app migrate` with the new image; start.
4. Check `/readyz`, login, connection, preview; re-enable the form.

Migrations never run on ordinary startup: an out-of-date schema stops the container (`check-schema`).

Rollback: previous image only when schema-compatible; otherwise restore the matched database and key and follow Restore. Restoring SQLite does not undo Drata changes. Never `docker compose down -v` as an upgrade step.

## Ingress

- Proxy to `127.0.0.1:8000`; response timeout >= 70 s; request body limit 128 KiB; HSTS after TLS verified.
- Restrict to the customer network or VPN. Internet exposure or SSO is a separate decision.
- Egress: HTTPS to `public-api.drata.com` (or the verified regional endpoint set in `DRATA_BASE_URL`).
- Containerized proxy: use a private Docker network, not loopback.
- `TRUST_PROXY_HOPS` must equal the real proxy count; block direct client access to port 8000.
- State-changing requests need `Origin` equal to `APP_BASE_URL`. `Referrer-Policy: no-referrer` makes browsers send `Origin: null` on same-origin form posts; those are accepted only with `Sec-Fetch-Site: same-origin`. The proxy must pass `Origin` and `Sec-Fetch-Site` through unchanged.
