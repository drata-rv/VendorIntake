import re
import uuid
from datetime import timedelta

import pytest
from conftest import ORIGIN, PASSWORD, Actor, answers, envelope, only_submission, settings

from app import auth, crypto, db


def routes(app, prefix):
    return [(method, re.sub(r"<[^>]+>", "x", rule.rule)) for rule in app.url_map.iter_rules()
            if rule.rule.startswith(prefix) for method in sorted(rule.methods - {"HEAD", "OPTIONS"})]


def call(actor, method, path):
    return actor.get(path) if method == "GET" else actor.post(path)


def code_of(resp):
    return resp.get_json()["error"]["code"]


def test_unauthenticated_api_calls_get_401_json(app, anon, fake):
    found = routes(app, "/api/")
    assert len(found) >= 20
    for method, path in found:
        resp = call(anon, method, path)
        assert resp.status_code == 401 and code_of(resp) == "UNAUTHENTICATED", (method, path)
    assert fake.calls == []


def test_requester_forbidden_on_every_admin_api_route(app, requester, fake):
    found = routes(app, "/api/admin/")
    assert len(found) >= 15
    for method, path in found:
        resp = call(requester, method, path)
        assert resp.status_code == 403 and code_of(resp) == "FORBIDDEN", (method, path)
    assert fake.calls == []


def test_admin_pages_redirect_non_admins(app, requester, anon):
    pages = [re.sub(r"<[^>]+>", "x", r.rule) for r in app.url_map.iter_rules()
             if r.rule.startswith("/admin/") and "GET" in r.methods] + ["/submissions/x"]
    assert len(pages) >= 4
    for page in pages:
        for actor, target in ((requester, "/intake"), (anon, "/login")):
            resp = actor.get(page)
            assert resp.status_code == 302 and target in resp.headers["Location"], page


def test_requester_cannot_read_another_requesters_submission(requester, other, admin):
    sid = requester.submit(answers()).get_json()["submissionId"]
    path = f"/api/submissions/{sid}"
    foreign, missing = other.get(path), other.get(f"/api/submissions/{uuid.uuid4()}")
    assert foreign.status_code == missing.status_code == 404
    assert foreign.get_json() == missing.get_json() and "Zorblax" not in foreign.get_data(as_text=True)
    mine, adm = requester.get(path).get_json(), admin.get(path).get_json()
    assert mine["id"] == adm["id"] == sid
    assert not {"candidates", "attempts", "events", "payload", "requesterEmail"} & set(mine)
    assert {"candidates", "attempts", "events", "payload", "requesterEmail"} <= set(adm)


def test_state_change_needs_csrf_token_and_exact_origin(requester, fake, conn):
    cases = [({"csrf": None}, "CSRF_INVALID"), ({"csrf": "wrong-token"}, "CSRF_INVALID"),
             ({"origin": "https://evil.test"}, "CSRF_ORIGIN"), ({"origin": "http://bridge.test"}, "CSRF_ORIGIN"),
             ({"origin": None}, "CSRF_ORIGIN")]
    for override, code in cases:
        resp = requester.post("/api/submissions", {"formVersion": 1, "answers": answers()}, key=str(uuid.uuid4()), **override)
        assert resp.status_code == 403 and code_of(resp) == code, override
    assert conn.execute("SELECT COUNT(*) FROM submissions").fetchone()[0] == 0 and fake.calls == []


def test_non_json_and_oversized_bodies_rejected(requester, fake):
    key = str(uuid.uuid4())
    plain = requester.post("/api/submissions", data="{}", content_type="text/plain", key=key)
    form = requester.post("/api/submissions", data={"formVersion": "1"}, key=key)
    assert plain.status_code == form.status_code == 415 and code_of(plain) == "UNSUPPORTED_MEDIA_TYPE"
    big = requester.post("/api/submissions", data=b'{"pad": "' + b"x" * 140000 + b'"}',
                         content_type="application/json", key=key)
    assert big.status_code == 413 and code_of(big) == "PAYLOAD_TOO_LARGE"
    assert fake.calls == []


def test_credentials_never_appear_in_responses_or_at_rest(app, admin, requester, fake, conn):
    new_key = "fake-key-NEW-77"
    old_cipher = settings(conn)["api_key_enc"]
    seen = []

    def grab(resp):
        seen.append(resp.get_data(as_text=True))
        return resp

    connection = grab(admin.get("/api/admin/connection")).get_json()
    assert {"configured", "credentialVersion", "connectionState"} <= set(connection) and connection["configured"]
    grab(admin.post("/api/admin/connection/test", {"apiKey": new_key}))
    assert grab(admin.post("/api/admin/connection", {"apiKey": new_key, "confirmAccountId": "acct-1",
                                                     "attestCreate": True})).status_code == 200
    sid = grab(requester.submit(answers())).get_json()["submissionId"]
    for resp in (requester.get(f"/api/submissions/{sid}"), admin.get(f"/api/submissions/{sid}"),
                 admin.post(f"/api/admin/submissions/{sid}/export"), admin.get("/api/admin/connection")):
        grab(resp)
    new_cipher = settings(conn)["api_key_enc"]
    assert new_cipher != old_cipher
    with app.app_context():
        assert crypto.decrypt(new_cipher) == new_key
    for text in seen:
        for secret in ("fake-key", new_key, old_cipher, new_cipher, "gAAAA", "api_key_enc"):
            assert secret not in text
    row = only_submission(conn)
    assert "Zorblax" not in row["answers_enc"] + row["payload_enc"] and "fake-key" not in new_cipher


def test_wrong_tenant_credential_rejected_and_old_one_kept(admin, fake, conn):
    before = settings(conn)
    fake.account = {"accountId": "acct-2", "name": "Other", "domain": "other.test"}
    resp = admin.post("/api/admin/connection", {"apiKey": "fake-key-OTHER", "confirmAccountId": "acct-2",
                                                "attestCreate": True})
    after = settings(conn)
    assert resp.status_code == 422 and code_of(resp) == "CONNECTION_TEST_FAILED"
    assert (after["account_id"], after["api_key_enc"], after["credential_version"]) == (
        before["account_id"], before["api_key_enc"], before["credential_version"])


def test_logs_never_contain_secrets_answers_or_vendor_names(admin, requester, fake, logs, conn):
    new_key = "fake-key-NEW-88"
    requester.submit(answers())
    fake.create_mode = "commit_timeout"
    sid = requester.submit(answers(vendor_name="Yggdrasil Freight", vendor_website="https://yggdrasil.example")
                           ).get_json()["submissionId"]
    admin.post("/api/admin/connection/test", {"apiKey": new_key})
    admin.post("/api/admin/connection", {"apiKey": new_key, "confirmAccountId": "acct-1", "attestCreate": True})
    admin.get(f"/api/submissions/{sid}")
    admin.reconcile(sid)
    requester.post("/api/submissions", {"formVersion": 1, "answers": answers()}, csrf="wrong")
    fake.create_mode, fake.create_reply = "reply", envelope(401)
    requester.submit(answers(vendor_name="Third Party Ltd", vendor_website="https://third.example"))
    text = "\n".join(logs)
    assert len(logs) >= 8 and '"request"' in text
    for secret in ("fake-key", new_key, "gAAAA", "Zorblax", "Yggdrasil", "Third Party", "zorblax.example",
                   "Quantum widget", "Because Reasons", "SECRET-DEBUG", "Authorization"):
        assert secret not in text


def test_login_cooldown_after_ten_failures(app, conn, requester, other):
    ip = "203.0.113.9"
    with app.test_request_context(base_url=ORIGIN):
        for _ in range(9):
            assert auth.authenticate(requester.email, "wrong password 123", ip) is None
        assert not auth.login_blocked(conn, requester.email, ip)
        assert auth.authenticate(requester.email, "wrong password 123", ip) is None
        assert auth.login_blocked(conn, requester.email, ip)
        assert auth.authenticate(requester.email, PASSWORD, ip) is None
    for (until,) in conn.execute("SELECT cooldown_until FROM login_limits"):
        assert timedelta(minutes=14) < db.parse_iso(until) - db.utcnow() <= timedelta(minutes=15)

    def login(email, address):
        visitor, nonce = Actor(app), "n" * 24
        visitor.client.set_cookie(auth.PRELOGIN_COOKIE, app.extensions["fernet"].encrypt(nonce.encode()).decode(),
                                  domain="bridge.test", secure=True)
        return visitor.client.post("/login", base_url=ORIGIN, headers={"Origin": ORIGIN, "X-Forwarded-For": address},
                                   data={"email": email, "password": PASSWORD, "csrf_token": nonce})

    def has_session(resp):
        return any(auth.SESSION_COOKIE in c for c in resp.headers.getlist("Set-Cookie"))

    # Cooldown is keyed by account too, so a fresh source IP does not lift it.
    locked, control = login(requester.email, "198.51.100.7"), login(other.email, "198.51.100.8")
    assert locked.status_code != 302 and not has_session(locked)
    assert control.status_code == 302 and has_session(control)


@pytest.mark.parametrize("change", [{"active": False}, {"role": "ADMIN"}, "flag"], ids=["disable", "role", "flag"])
def test_disabled_or_changed_user_loses_session(admin, requester, conn, change):
    probe = f"/api/submissions/{uuid.uuid4()}"
    assert requester.get(probe).status_code == 404
    if change == "flag":
        conn.execute("UPDATE users SET active = 0 WHERE id = ?", (requester.id,))
    else:
        assert admin.post(f"/api/admin/users/{requester.id}", change).status_code == 200
    resp = requester.get(probe)
    assert resp.status_code == 401 and code_of(resp) == "UNAUTHENTICATED"


def test_temporary_password_blocks_api_until_changed(app, admin, conn, fake):
    created = admin.post("/api/admin/users", {"email": "New.Hire@bridge.test"})
    body = created.get_json()
    assert created.status_code == 201 and body["email"] == "new.hire@bridge.test" and "argon2" not in str(body)
    user = Actor(app, body["id"], body["email"], *auth.issue_session(conn, body["id"]))
    assert conn.execute("SELECT must_change_password FROM users WHERE id = ?", (user.id,)).fetchone()[0] == 1
    probe = f"/api/submissions/{uuid.uuid4()}"
    for resp in (user.get(probe), user.submit(answers())):
        assert resp.status_code == 403 and code_of(resp) == "PASSWORD_CHANGE_REQUIRED"
    assert fake.calls == []
    weak = user.post("/api/account/password", {"currentPassword": body["temporaryPassword"], "newPassword": "short"})
    wrong = user.post("/api/account/password", {"currentPassword": "nope", "newPassword": PASSWORD + "!"})
    assert (weak.status_code, code_of(weak), wrong.status_code) == (422, "PASSWORD_POLICY", 403)
    done = user.post("/api/account/password", {"currentPassword": body["temporaryPassword"], "newPassword": PASSWORD + "!"})
    assert done.status_code == 200 and user.get(probe).status_code == 404


def test_credential_change_requires_recent_login(admin, fake, conn):
    conn.execute("UPDATE sessions SET created_at = ? WHERE user_id = ?",
                 (db.iso(db.utcnow() - timedelta(minutes=10)), admin.id))
    cipher = settings(conn)["api_key_enc"]
    save = {"apiKey": "fake-key-NEW-99", "confirmAccountId": "acct-1", "attestCreate": True}
    for path, body in (("/api/admin/connection", save), ("/api/admin/connection/disconnect", {})):
        resp = admin.post(path, body)
        assert resp.status_code == 403 and code_of(resp) == "REAUTH_REQUIRED", path
    assert settings(conn)["api_key_enc"] == cipher and fake.calls == []
    assert admin.post("/api/account/reauth", {"password": "wrong"}).status_code == 403
    reauth = admin.post("/api/account/reauth", {"password": PASSWORD})
    admin.csrf = reauth.get_json()["csrfToken"]
    assert reauth.status_code == 200 and admin.post("/api/admin/connection", save).status_code == 200
    assert settings(conn)["credential_version"] == 2
    off = admin.post("/api/admin/connection/disconnect")
    assert off.status_code == 200 and settings(conn)["api_key_enc"] is None and not off.get_json()["configured"]
