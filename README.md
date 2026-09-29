# Vendor Intake Bridge

Customer-hosted Flask app. Requester submits vendor-intake answers; server maps them to Drata Public API v2 fields and creates a `PROSPECTIVE` vendor. Administrator configures connection and form, reviews failures, resolves duplicates, and optionally applies an intake to a selected existing prospective vendor.

## Scope

- Tenant: one Drata tenant, pinned at first connection. Process: one Gunicorn worker, four threads. Storage: one SQLite file. Form: one enabled version.
- Accounts: local `ADMIN` and `REQUESTER`.
- Writes: synchronous; administrator-initiated retry only.
- Custom fields: `TEXT`, `LONG_TEXT`, `URL`, `NUMBER`. `OPTIONS`, `OPTIONS_NUMERIC`, `CURRENCY` raise a setup blocker.
- Deployment: single host; restart interrupts in-flight requests.

## Layout

| Path | Role |
|---|---|
| `app/drata.py` | HTTP adapter: timeouts, 1 req/s gate, pagination, error normalization |
| `app/mappings.py` | Field catalog, validation, create/update payloads, marker, read-back compare |
| `app/submissions.py` | State machine, idempotency, duplicate scan, reconcile, update flow, retention |
| `app/connection.py` | Credential test/save/disconnect, pinned account |
| `app/forms.py` | Immutable form versions, publish checks, custom-field drift fingerprint |
| `app/auth.py` | Argon2id, server-side sessions, CSRF/Origin, login limits |
| `app/cli.py` | `init-db migrate check-schema create-admin reset-password backup restore-check cleanup rotate-key` |

## Configuration

| Variable | Default | Note |
|---|---|---|
| `APP_BASE_URL` | required | `https://` origin, no path. `http://` allowed for loopback only. Exact `Origin` match enforced. |
| `DATABASE_PATH` | `/data/bridge.sqlite3` | Local disk only, not NFS. |
| `ENCRYPTION_KEY_FILE` | `/run/secrets/bridge.key` | Fernet key. Startup fails if absent, invalid, or cannot decrypt stored credential. |
| `DRATA_BASE_URL` | `https://public-api.drata.com/public/v2` | Fixed deployment setting. `https` required. |
| `DRATA_LINK_HOSTS` | empty | Comma list of exact hostnames allowed for `_links.self.href`. Empty shows Drata ID only. |
| `TRUST_PROXY_HOPS` | `1` | Proxy hops for `X-Forwarded-*`. `0` disables. |
| `MAX_CONTENT_LENGTH` | `131072` | Request body cap, bytes. |

## Install (single host)

```sh
docker build -t vendor-intake-bridge:1.0.0 .
printf 'BRIDGE_IMAGE=vendor-intake-bridge:1.0.0\nAPP_BASE_URL=https://intake.customer.example\n' > .env
umask 077 && mkdir -p data secrets && chmod 0700 data secrets
docker run --rm vendor-intake-bridge:1.0.0 python -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())' > secrets/bridge.key
sudo chown -R 10001:10001 data && sudo chown 10001:10001 secrets/bridge.key && sudo chmod 0400 secrets/bridge.key
docker compose run --rm bridge flask --app app init-db
docker compose run --rm bridge flask --app app create-admin
docker compose up -d
```

Then: point HTTPS proxy at `127.0.0.1:8000` (response timeout >= 70 s, body limit 128 KiB), sign in, connect scoped Drata key, configure form, inspect offline preview, publish.

## Local run (development)

```sh
python3.13 -m venv .venv && .venv/bin/pip install -r requirements-dev.txt
umask 077 && .venv/bin/python -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())' > /tmp/bridge.key
export APP_BASE_URL=http://localhost:8000 DATABASE_PATH=/tmp/bridge.sqlite3 ENCRYPTION_KEY_FILE=/tmp/bridge.key
.venv/bin/flask --app app init-db && .venv/bin/flask --app app create-admin
.venv/bin/gunicorn 'app:create_app()' --bind 127.0.0.1:8000 --workers 1 --threads 4
```

## Checks

```sh
.venv/bin/python -m pytest -q
```

## Drata API key

Create key `Vendor Intake Bridge` with custom scopes. Create-only: Company Settings: Get Company Settings, Vendors: List Vendors, Vendors: Get Vendor, Vendors: Create Vendor. Add Vendors: Update Vendor for updates, Custom Field Definitions: Get Custom Field Definitions for custom fields. No delete or questionnaire scopes. Set expiration; restrict source IP when stable. Rate limit 500 req/min per source IP.

## Operator responsibilities

- Host, HTTPS ingress, patching, time sync, egress to Drata.
- Key file and database backups, stored separately, both required for recovery.
- Local accounts and temporary-password delivery.
- Revoke retired Drata keys in Drata Settings. Disconnect does not revoke.
- Run `flask --app app cleanup` when the app is idle or stopped to meet retention deadlines.
- Name an application maintainer (dependency/API updates) and an operational owner.

## Limits

- Duplicate detection compares name and hostname equality. Another system can create a vendor between the scan and the write.
- Update runs GET, then PUT. The pair is not atomic; enable updates when the bridge owns the selected fields during execution.
- Deleting a bridge record leaves the Drata record in place.
- Container build and `docker compose` run: untested in the authoring environment.
