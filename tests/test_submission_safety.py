import copy
import uuid
from datetime import timedelta

import pytest
from conftest import answers, attempts, envelope, only_submission, settings, sub

from app import db, drata, mappings, submissions
from app.errors import ApiFail


def body_of(resp):
    return resp.get_json()


def test_same_key_replay_returns_same_result_with_one_post(requester, fake, conn):
    key = str(uuid.uuid4())
    first, second = requester.submit(answers(), key=key), requester.submit(answers(), key=key)
    assert (first.status_code, second.status_code) == (201, 200)
    assert body_of(first) == body_of(second)
    assert body_of(first)["state"] == "CREATED" and body_of(first)["drataId"] in fake.vendors
    assert fake.post_count == 1
    row = only_submission(conn)
    sent = fake.posts[0]
    assert sent["status"] == "PROSPECTIVE" and sent["contactEmail"] == "Sec@zorblax.example"
    assert sent["notes"].endswith(f"[VendorIntakeBridge:{settings(conn)['installation_uuid']}:{row['id']}]")


def test_same_key_with_different_body_conflicts(requester, fake):
    key = str(uuid.uuid4())
    requester.submit(answers(), key=key)
    resp = requester.submit(answers(vendor_name="Another Vendor"), key=key)
    assert resp.status_code == 409 and body_of(resp)["error"]["code"] == "IDEMPOTENCY_CONFLICT"
    assert fake.post_count == 1


def test_other_requesters_key_gives_generic_conflict_without_leaking(requester, other, fake):
    key = str(uuid.uuid4())
    mine = body_of(requester.submit(answers(), key=key))
    replies = [other.submit(answers(), key=key), other.submit(answers(vendor_name="Different Co"), key=key)]
    for resp in replies:
        assert resp.status_code == 409 and body_of(resp)["error"]["code"] == "IDEMPOTENCY_CONFLICT"
        text = resp.get_data(as_text=True)
        assert mine["submissionId"] not in text and "Zorblax" not in text and str(mine["drataId"]) not in text
    assert body_of(replies[0]) == body_of(replies[1])
    assert fake.post_count == 1


def test_busy_write_lock_returns_503_and_stores_nothing(app, requester, fake, conn):
    key = str(uuid.uuid4())
    lock = app.extensions["write_lock"]
    assert lock.acquire(blocking=False)
    try:
        resp = requester.submit(answers(), key=key)
    finally:
        lock.release()
    assert resp.status_code == 503 and body_of(resp)["error"]["code"] == "BRIDGE_BUSY"
    assert resp.headers["Retry-After"] == "5"
    assert conn.execute("SELECT COUNT(*) FROM submissions").fetchone()[0] == 0 and fake.calls == []
    assert requester.submit(answers(), key=key).status_code == 201 and fake.post_count == 1


def test_commit_then_timeout_is_unknown_and_never_reposted(admin, requester, fake, conn):
    fake.create_mode = "commit_timeout"
    key = str(uuid.uuid4())
    resp = requester.submit(answers(), key=key)
    sid = body_of(resp)["submissionId"]
    assert resp.status_code == 202 and body_of(resp)["state"] == "UNKNOWN"
    assert sub(conn, sid)["attempt_count"] == 1 and [a["dispatched"] for a in attempts(conn, sid)] == [1]
    replay = requester.submit(answers(), key=key)
    assert replay.status_code == 200 and body_of(replay)["state"] == "UNKNOWN"
    retry = admin.retry(sid)
    assert retry.status_code == 409 and body_of(retry)["error"]["code"] == "STATE_CONFLICT"
    assert fake.post_count == 1 and len(fake.vendors) == 1


@pytest.mark.parametrize("mode,tamper,state,reason,outcome", [
    ("commit_timeout", False, "CREATED", None, "MARKER_MATCH"),
    ("commit_timeout", True, "NEEDS_REVIEW", "VERIFY_MISMATCH", "MARKER_MATCH"),
    ("timeout", False, "UNKNOWN", "TRANSPORT_UNCERTAIN", "NO_MARKER_MATCH"),
])
def test_reconcile_finds_unknown_write_by_marker(admin, requester, fake, mode, tamper, state, reason, outcome):
    fake.create_mode = mode
    sid = body_of(requester.submit(answers()))["submissionId"]
    if tamper:
        for vendor in fake.vendors.values():
            vendor["name"] = "Edited In Drata"
    resp = admin.reconcile(sid)
    body = body_of(resp)
    assert resp.status_code == 200 and (body["state"], body["reason"], body["reconcile"]) == (state, reason, outcome)
    assert (body["drataId"] in fake.vendors) if state != "UNKNOWN" else body["drataId"] is None
    assert fake.post_count == 1


def test_provably_unsent_error_is_retryable_and_admin_retry_posts_once_more(admin, requester, fake, conn):
    fake.create_mode = "not_sent"
    resp = requester.submit(answers())
    sid = body_of(resp)["submissionId"]
    assert resp.status_code == 202 and body_of(resp)["state"] == "RETRYABLE"
    assert [a["dispatched"] for a in attempts(conn, sid)] == [0] and fake.post_count == 1
    fake.create_mode = "ok"
    retry = admin.retry(sid)
    assert retry.status_code == 200 and body_of(retry)["state"] == "CREATED"
    assert fake.post_count == 2 and len(fake.vendors) == 1
    assert [a["dispatched"] for a in attempts(conn, sid)] == [0, 1]


@pytest.mark.parametrize("crash,expected", [("create", "UNKNOWN"), ("scan", "RETRYABLE")])
def test_startup_recovery_of_interrupted_submission(requester, fake, conn, crash, expected):
    fake.crash_on.add(crash)
    requester.submit(answers())
    sid = only_submission(conn)["id"]
    conn.execute("UPDATE submissions SET state = 'IN_PROGRESS', state_reason = NULL WHERE id = ?", (sid,))
    calls = list(fake.calls)
    assert submissions.recover_startup(conn) == 1
    assert (sub(conn, sid)["state"], sub(conn, sid)["state_reason"]) == (expected, "RESTART_RECOVERY")
    assert fake.calls == calls


def test_verification_failure_after_201_keeps_id_and_never_reposts(admin, requester, fake, conn):
    fake.get_fail = True
    resp = requester.submit(answers())
    sid = body_of(resp)["submissionId"]
    row = sub(conn, sid)
    assert resp.status_code == 202 and (row["state"], row["state_reason"]) == ("NEEDS_REVIEW", "VERIFY_PENDING")
    assert row["result_drata_id"] in fake.vendors
    fake.get_fail = False
    done = body_of(admin.reconcile(sid))
    assert (done["state"], done["reconcile"]) == ("CREATED", "VERIFIED") and fake.post_count == 1


DUPLICATES = [
    ("same name, null status", {"name": "  zorblax   INDUSTRIES ", "url": None, "status": None}, True),
    ("same host, archived", {"name": "Legacy Holdings", "url": "https://ZORBLAX.example./about", "status": "ARCHIVED"}, True),
    ("www host not collapsed", {"name": "Legacy Holdings", "url": "https://www.zorblax.example", "status": "ACTIVE"}, False),
]


@pytest.mark.parametrize("existing,is_dup", [c[1:] for c in DUPLICATES], ids=[c[0] for c in DUPLICATES])
def test_tenant_duplicate_needs_review_and_hides_candidates_from_requester(admin, requester, fake, conn, existing, is_dup):
    fake.add_vendor(id=987654321, **existing)
    resp = requester.submit(answers())
    sid = body_of(resp)["submissionId"]
    if not is_dup:
        assert resp.status_code == 201 and fake.post_count == 1
        return
    assert resp.status_code == 202 and body_of(resp)["state"] == "NEEDS_REVIEW" and fake.post_count == 0
    assert sub(conn, sid)["state_reason"] == "DUPLICATE_SUSPECTED"
    seen = resp.get_data(as_text=True) + requester.get(f"/api/submissions/{sid}").get_data(as_text=True)
    assert "987654321" not in seen and "Legacy" not in seen and "candidates" not in seen
    candidates = body_of(admin.get(f"/api/submissions/{sid}"))["candidates"]
    assert [c["id"] for c in candidates] == [987654321]


def test_uncertain_write_blocks_second_submission_of_same_vendor(requester, other, fake, conn):
    # The uncommitted timeout leaves nothing in the tenant, so only the local ledger can flag the repeat.
    fake.create_mode = "timeout"
    first = body_of(requester.submit(answers()))["submissionId"]
    fake.create_mode = "ok"
    for actor in (requester, other):
        resp = actor.submit(answers())
        assert resp.status_code == 202 and body_of(resp)["state"] == "NEEDS_REVIEW"
    assert {r["state_reason"] for r in conn.execute("SELECT * FROM submissions WHERE id != ?", (first,))} == {"LOCAL_UNRESOLVED_MATCH"}
    assert fake.post_count == 1


CLASSIFY = [
    ("400 recognized", envelope(400), "NEEDS_CORRECTION", "DRATA_VALIDATION"),
    ("401 recognized", envelope(401), "BLOCKED", "DRATA_PERMISSION"),
    ("402 recognized", envelope(402), "BLOCKED", "CUSTOM_FIELDS_UNAVAILABLE"),
    ("412 recognized", envelope(412), "BLOCKED", "TERMS_NOT_ACCEPTED"),
    ("429 recognized", envelope(429, 30.0), "RETRYABLE", "RATE_LIMITED"),
    ("500 recognized", envelope(500), "UNKNOWN", "UPSTREAM_UNCERTAIN"),
    ("502 bare", drata.Reply(502, None), "UNKNOWN", "UPSTREAM_UNCERTAIN"),
    ("400 unrecognized", drata.Reply(400, {"error": "proxy"}), "UNKNOWN", "UPSTREAM_UNCERTAIN"),
    ("201 malformed", drata.Reply(201, {"id": "abc"}), "UNKNOWN", "UPSTREAM_UNCERTAIN"),
]


@pytest.mark.parametrize("reply,state,reason", [c[1:] for c in CLASSIFY], ids=[c[0] for c in CLASSIFY])
def test_drata_response_classification_sends_exactly_one_post(admin, requester, fake, conn, reply, state, reason):
    fake.create_mode, fake.create_reply = "reply", reply
    resp = requester.submit(answers())
    row = only_submission(conn)
    assert (row["state"], row["state_reason"]) == (state, reason)
    if state == "NEEDS_CORRECTION":
        error = body_of(resp)["error"]
        assert resp.status_code == 422 and error["code"] == "DRATA_REJECTED" and error["submissionId"] == row["id"]
    else:
        assert resp.status_code == 202 and body_of(resp)["state"] == state
    (attempt,) = attempts(conn, row["id"])
    assert attempt["dispatched"] == 1 and attempt["http_status"] == reply.status
    everything = resp.get_data(as_text=True) + str(row["last_error"]) + str(dict(admin.get(f"/api/submissions/{row['id']}").get_json()))
    assert "SECRET-DEBUG" not in everything
    assert fake.post_count == 1


def test_permission_rejection_blocks_connection_and_later_writes(requester, fake, conn):
    fake.create_mode, fake.create_reply = "reply", envelope(401)
    requester.submit(answers())
    row = settings(conn)
    assert (row["connection_state"], row["writes_enabled"]) == ("BLOCKED", 0)
    resp = requester.submit(answers(vendor_name="Second Vendor", vendor_website="https://second.example"))
    assert resp.status_code == 503 and body_of(resp)["error"]["code"] == "CONNECTION_UNAVAILABLE"
    assert fake.post_count == 1


def test_rate_limit_not_before_blocks_retry_until_it_passes(admin, requester, fake, conn):
    fake.create_mode, fake.create_reply = "reply", envelope(429, 120.0)
    sid = body_of(requester.submit(answers()))["submissionId"]
    limit = db.parse_iso(settings(conn)["rate_limit_until"])
    assert timedelta(seconds=100) < limit - db.utcnow() <= timedelta(seconds=120)
    fake.create_mode = "ok"
    blocked = admin.retry(sid)
    assert blocked.status_code == 409 and body_of(blocked)["error"]["code"] == "RATE_LIMITED"
    other_vendor = requester.submit(answers(vendor_name="Second Vendor", vendor_website="https://second.example"))
    assert body_of(other_vendor)["state"] == "RETRYABLE" and fake.post_count == 1
    conn.execute("UPDATE settings SET rate_limit_until = ?", (db.iso(db.utcnow() - timedelta(seconds=1)),))
    retry = admin.retry(sid)
    assert retry.status_code == 200 and body_of(retry)["state"] == "CREATED" and fake.post_count == 2


def test_form_changed_returns_409_with_zero_drata_calls(admin, requester, fake, conn):
    schema = copy.deepcopy(mappings.normalize_form(mappings.starter_form()))
    schema["fields"][0]["label"] = "Legal vendor name"
    assert admin.post("/api/admin/form", {"schema": schema}).status_code == 201
    assert admin.post("/api/admin/form/publish", {"version": 2}).status_code == 200
    resp = requester.submit(answers(), version=1)
    assert resp.status_code == 409 and body_of(resp)["error"]["code"] == "FORM_CHANGED"
    assert fake.calls == [] and conn.execute("SELECT COUNT(*) FROM submissions").fetchone()[0] == 0
    assert requester.submit(answers(), version=2).status_code == 201


def test_nonce_is_single_use_bound_and_expiring(app, requester, fake, conn):
    fake.create_mode = "timeout"
    sid = body_of(requester.submit(answers()))["submissionId"]

    def rejected(token, action="LINK_EXISTING", target=5):
        with pytest.raises(ApiFail) as failure:
            submissions.consume_nonce(conn, sid, action, token, target)
        assert failure.value.code == "NONCE_INVALID"

    with app.app_context():
        token = submissions.issue_nonce(conn, sid, "LINK_EXISTING", 5)["nonce"]
        rejected(token, "CANCEL")
        rejected(token, target=6)
        rejected("not-the-nonce")
        submissions.consume_nonce(conn, sid, "LINK_EXISTING", token, 5)
        rejected(token)
        token = submissions.issue_nonce(conn, sid, "LINK_EXISTING", 5)["nonce"]
        conn.execute("UPDATE submissions SET action_expires_at = ? WHERE id = ?",
                     (db.iso(db.utcnow() - timedelta(seconds=1)), sid))
        rejected(token)


RACES = [
    ("UPDATE active_form SET enabled = 0", 503, "FORM_DISABLED"),
    ("INSERT INTO form_versions (schema_json, created_by, created_at) SELECT schema_json, NULL, created_at"
     " FROM form_versions WHERE version = 1; UPDATE active_form SET version = 2", 409, "FORM_CHANGED"),
    ("UPDATE settings SET writes_enabled = 0", 503, "CONNECTION_UNAVAILABLE"),
]


@pytest.mark.parametrize("change,status,code", RACES, ids=["disabled", "republished", "writes-off"])
def test_form_and_connection_rechecked_after_lock_acquired(app, requester, fake, conn, change, status, code):
    real = app.extensions["write_lock"]

    # An admin change lands after the pre-lock checks and before the lock is taken.
    class RacyLock:
        release = real.release

        def acquire(self, *args, **kwargs):
            conn.executescript(change)
            return real.acquire(*args, **kwargs)

    app.extensions["write_lock"] = RacyLock()
    resp = requester.submit(answers())
    assert resp.status_code == status and body_of(resp)["error"]["code"] == code
    assert fake.calls == [] and conn.execute("SELECT COUNT(*) FROM submissions").fetchone()[0] == 0


def test_correction_links_to_original_only_for_own_rejected_submission(requester, other, fake, conn):
    fake.create_mode, fake.create_reply = "reply", envelope(400)
    requester.submit(answers())
    original = only_submission(conn)["id"]
    fake.create_mode = "ok"
    fixed = {"formVersion": 1, "answers": answers(vendor_name="Zorblax Industries Ltd")}
    foreign = other.post("/api/submissions", {**fixed, "originSubmissionId": original}, key=str(uuid.uuid4()))
    assert foreign.status_code == 422 and fake.post_count == 1
    linked = requester.post("/api/submissions", {**fixed, "originSubmissionId": original}, key=str(uuid.uuid4()))
    assert linked.status_code == 201
    assert sub(conn, body_of(linked)["submissionId"])["origin_submission_id"] == original
    again = requester.post("/api/submissions", {**fixed, "originSubmissionId": original}, key=str(uuid.uuid4()))
    assert again.status_code in (201, 202) and sub(conn, original)["state"] == "NEEDS_CORRECTION"
