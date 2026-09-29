import copy
import logging
import uuid

import pytest
from cryptography.fernet import Fernet

from app import JsonFormatter, auth, create_app, crypto, db, drata, forms

ORIGIN = "https://bridge.test"
FAKE_KEY = "fake-key-9c1e"
PASSWORD = "correct horse battery staple"
UNSET = object()
BASE_ANSWERS = {
    "vendor_name": "Zorblax Industries",
    "vendor_website": "https://zorblax.example",
    "services_provided": "Quantum widget hosting",
    "stores_pii": False,
    "is_subprocessor": True,
    "contact_email": "Sec@Zorblax.EXAMPLE",
    "business_justification": "Because Reasons 7731",
}


def answers(**overrides):
    return {**BASE_ANSWERS, **overrides}


def envelope(status, retry_after=None):
    body = {"statusCode": status, "message": "SECRET-DEBUG Zorblax", "code": 4001}
    return drata.Reply(status, body, retry_after)


class FakeDrata:
    def __init__(self):
        self.vendors, self.calls, self.posts, self.puts = {}, [], [], []
        self.account = {"accountId": "acct-1", "name": "Acme", "domain": "acme.test"}
        self.create_mode, self.create_reply = "ok", None
        self.get_fail, self.crash_on = False, set()
        self.definitions = {}
        self._ids = 1000

    @property
    def post_count(self):
        return len(self.posts)

    def factory(self, api_key, budget=None):
        self.budget = budget or drata.Budget()
        return self

    def add_vendor(self, id=None, **fields):
        self._ids += 1
        vid = id or self._ids
        self.vendors[vid] = {"id": vid, "name": "Unnamed", "url": None, "status": "PROSPECTIVE", "notes": None,
                             "updatedAt": "2026-01-01T00:00:00.000Z", **fields}
        return vid

    def company(self):
        self.calls.append("company")
        return dict(self.account)

    def first_vendor_id(self):
        self.calls.append("first_vendor_id")
        return next(iter(self.vendors), None)

    def get_vendor(self, vendor_id, custom_fields=False):
        self.calls.append("get_vendor")
        if self.get_fail:
            raise drata.Transport("timeout", False)
        if vendor_id not in self.vendors:
            raise drata.ApiError(envelope(404))
        return copy.deepcopy(self.vendors[vendor_id])

    def scan_vendors(self):
        self.calls.append("scan_vendors")
        if "scan" in self.crash_on:
            raise RuntimeError("process died")
        return copy.deepcopy(self.vendors)

    def custom_field_definitions(self):
        self.calls.append("custom_field_definitions")
        return copy.deepcopy(self.definitions)

    def _store(self, payload):
        fields = {k: v for k, v in payload.items() if k not in ("contactEmail", "customFields")}
        vid = self.add_vendor(**fields)
        if "contactEmail" in payload:
            self.vendors[vid]["contactsEmail"] = payload["contactEmail"]
        if "customFields" in payload:
            self.vendors[vid]["customFields"] = [{"customFieldId": c["id"], "value": c["value"]}
                                                 for c in payload["customFields"]]
        return self.vendors[vid]

    def create_vendor(self, payload):
        self.calls.append("POST")
        self.posts.append(copy.deepcopy(payload))
        mode = self.create_mode
        if "create" in self.crash_on:
            raise RuntimeError("process died")
        if mode == "not_sent":
            raise drata.Transport("connect_timeout", True)
        if mode == "timeout":
            raise drata.Transport("timeout", False)
        if mode == "reply":
            return self.create_reply
        vendor = self._store(payload)
        # Drata committed the vendor but the response never reached the bridge.
        if mode == "commit_timeout":
            raise drata.Transport("timeout", False)
        return drata.Reply(201, copy.deepcopy(vendor))

    def update_vendor(self, vendor_id, payload):
        self.calls.append("PUT")
        self.puts.append((vendor_id, copy.deepcopy(payload)))
        vendor = self.vendors[vendor_id]
        vendor.update({("contactsEmail" if k == "contactEmail" else k): v for k, v in payload.items()})
        return drata.Reply(200, copy.deepcopy(vendor))

    def close(self):
        pass


class Actor:
    def __init__(self, app, user_id=None, email=None, token=None, csrf=None):
        self.client, self.id, self.email, self.token, self.csrf = app.test_client(), user_id, email, token, csrf
        if token:
            self.client.set_cookie(auth.SESSION_COOKIE, token, domain="bridge.test", secure=True, httponly=True)

    def get(self, path, **kw):
        return self.client.get(path, base_url=ORIGIN, **kw)

    def post(self, path, json=None, *, key=None, csrf=UNSET, origin=ORIGIN, headers=None, **kw):
        hdrs = {"Origin": origin} if origin else {}
        token = self.csrf if csrf is UNSET else csrf
        if token:
            hdrs["X-CSRF-Token"] = token
        if key:
            hdrs["Idempotency-Key"] = key
        hdrs.update(headers or {})
        if "data" not in kw:
            kw["json"] = {} if json is None else json
        return self.client.post(path, base_url=ORIGIN, headers=hdrs, **kw)

    def submit(self, body, key=None, version=1):
        return self.post("/api/submissions", {"formVersion": version, "answers": body}, key=key or str(uuid.uuid4()))

    def nonce(self, sid, action, **extra):
        return self.post(f"/api/admin/submissions/{sid}/action-nonce", {"action": action, **extra}).get_json()["nonce"]

    def retry(self, sid):
        return self.post(f"/api/admin/submissions/{sid}/retry", {"nonce": self.nonce(sid, "RETRY")})

    def reconcile(self, sid):
        return self.post(f"/api/admin/submissions/{sid}/reconcile")


def activate_connection(conn, fernet, key=FAKE_KEY):
    conn.execute(
        "UPDATE settings SET account_id = 'acct-1', account_name = 'Acme', account_domain = 'acme.test',"
        " api_key_enc = ?, credential_version = 1, connection_state = 'ACTIVE', writes_enabled = 1,"
        " attest_create = 1, updated_at = ? WHERE id = 1", (crypto.encrypt(key, fernet), db.iso()))


def publish_starter(conn):
    conn.execute("UPDATE active_form SET version = 1, enabled = 1 WHERE id = 1")


def make_actor(app, conn, email, role="REQUESTER", must_change=False):
    uid = auth.create_user(conn, email, role, PASSWORD, must_change, "SYSTEM")
    token, csrf = auth.issue_session(conn, uid)
    return Actor(app, uid, email, token, csrf)


def sub(conn, sid):
    return conn.execute("SELECT * FROM submissions WHERE id = ?", (sid,)).fetchone()


def only_submission(conn):
    rows = conn.execute("SELECT * FROM submissions").fetchall()
    assert len(rows) == 1
    return rows[0]


def attempts(conn, sid):
    return conn.execute("SELECT * FROM attempts WHERE submission_id = ? ORDER BY attempt_number", (sid,)).fetchall()


def settings(conn):
    return db.settings_row(conn)


@pytest.fixture
def fake():
    return FakeDrata()


@pytest.fixture
def app(tmp_path, fake):
    key_file = tmp_path / "bridge.key"
    key_file.write_bytes(Fernet.generate_key())
    path = str(tmp_path / "bridge.sqlite3")
    conn = db.connect(path)
    db.migrate(conn)
    conn.execute("INSERT INTO settings (id, installation_uuid, updated_at) VALUES (1, ?, ?)", (db.new_id(), db.iso()))
    forms.seed_starter(conn)
    conn.close()
    application = create_app({"APP_BASE_URL": ORIGIN, "DATABASE_PATH": path, "ENCRYPTION_KEY_FILE": str(key_file),
                              "SKIP_STARTUP": True, "DRATA_MIN_INTERVAL": 0})
    application.extensions["drata_factory"] = fake.factory
    return application


@pytest.fixture
def conn(app):
    connection = db.connect(app.config["DATABASE_PATH"])
    yield connection
    connection.close()


@pytest.fixture
def ready(app, conn):
    activate_connection(conn, app.extensions["fernet"])
    publish_starter(conn)


@pytest.fixture
def admin(app, conn, ready):
    return make_actor(app, conn, "admin@bridge.test", "ADMIN")


@pytest.fixture
def requester(app, conn, ready):
    return make_actor(app, conn, "alice@bridge.test")


@pytest.fixture
def other(app, conn, ready):
    return make_actor(app, conn, "bob@bridge.test")


@pytest.fixture
def anon(app):
    return Actor(app)


@pytest.fixture
def logs(app):
    lines = []

    class Capture(logging.Handler):
        def emit(self, record):
            lines.append(JsonFormatter().format(record))

    handler = Capture()
    app.logger.addHandler(handler)
    yield lines
    app.logger.removeHandler(handler)
