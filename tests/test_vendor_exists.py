import json
import uuid

import pytest
from conftest import answers, attempts, legacy_duplicate, make_actor, only_submission, sub

from app import submissions

MESSAGE = "Not submitted. This vendor is already in Drata."
FIELD_IDS = {"name": "vendor_name", "website": "vendor_website"}
EXISTING = [
    ("name, prospective", {"name": "  zorblax   INDUSTRIES ", "url": None, "status": "PROSPECTIVE"}, "prospective", ["name"]),
    ("name, null status", {"name": "Zorblax Industries", "url": None, "status": None}, "existing", ["name"]),
    ("name, status NONE", {"name": "Zorblax Industries", "url": None, "status": "NONE"}, "existing", ["name"]),
    ("host, archived", {"name": "Legacy Holdings", "url": "https://ZORBLAX.example./about", "status": "ARCHIVED"}, "existing", ["website"]),
    ("host, scheme-less", {"name": "Legacy Holdings", "url": "zorblax.example/x", "status": "PROSPECTIVE"}, "prospective", ["website"]),
    ("both, active", {"name": "Zorblax Industries", "url": "https://zorblax.example", "status": "ACTIVE"}, "existing", ["name", "website"]),
    ("both, prospective", {"name": "Zorblax Industries", "url": "https://zorblax.example", "status": "PROSPECTIVE"}, "prospective",
     ["name", "website"]),
]


@pytest.mark.parametrize("vendor,kind,fields", [c[1:] for c in EXISTING], ids=[c[0] for c in EXISTING])
def test_existing_vendor_is_rejected_and_nothing_is_sent(admin, requester, fake, conn, vendor, kind, fields):
    fake.add_vendor(id=987654321, **vendor)
    key = str(uuid.uuid4())
    resp = requester.submit(answers(), key=key)
    error, row = resp.get_json()["error"], only_submission(conn)
    assert resp.status_code == 409 and error["code"] == "VENDOR_EXISTS" and error["message"] == MESSAGE
    assert error["match"] == {"kind": kind, "fields": fields} and error["submissionId"] == row["id"]
    assert set(error["fieldErrors"]) == {FIELD_IDS[f] for f in fields}
    assert all(kind in text for text in error["fieldErrors"].values())
    assert (row["state"], row["state_reason"], row["candidates_enc"]) == ("CANCELLED", "VENDOR_EXISTS", None)
    assert row["terminal_at"] and fake.post_count == 0 and attempts(conn, row["id"]) == []

    mine = requester.get(f"/api/submissions/{row['id']}").get_json()
    assert (mine["state"], mine["message"], mine["drataId"], mine["link"]) == ("CANCELLED", MESSAGE, None, None)
    detail = admin.get(f"/api/submissions/{row['id']}").get_json()
    assert detail["candidates"] == [] and detail["reason"] == "VENDOR_EXISTS"
    assert detail["lastError"] == f"{kind}:{'+'.join(fields)}".upper()
    seen = resp.get_data(as_text=True) + json.dumps(mine) + json.dumps(detail)
    assert "987654321" not in seen and "Legacy" not in seen

    replay = requester.submit(answers(), key=key)
    assert replay.status_code == 200 and replay.get_json()["state"] == "CANCELLED" and replay.get_json()["message"] == MESSAGE
    assert fake.post_count == 0 and conn.execute("SELECT COUNT(*) FROM submissions").fetchone()[0] == 1


def test_similar_host_is_not_a_match(requester, fake):
    fake.add_vendor(name="Legacy Holdings", url="https://www.zorblax.example", status="ACTIVE")
    assert requester.submit(answers()).status_code == 201 and fake.post_count == 1


RANKED = [
    ("more fields win", [("Zorblax Industries", "https://zorblax.example", "PROSPECTIVE"), ("Zorblax Industries", None, "ACTIVE")],
     {"kind": "prospective", "fields": ["name", "website"]}),
    ("tie prefers existing", [("Zorblax Industries", None, "PROSPECTIVE"), ("Other", "https://zorblax.example", "ARCHIVED")],
     {"kind": "existing", "fields": ["website"]}),
]


@pytest.mark.parametrize("vendors,match", [c[1:] for c in RANKED], ids=[c[0] for c in RANKED])
def test_reported_match_is_the_strongest(requester, fake, vendors, match):
    for name, url, status in vendors:
        fake.add_vendor(name=name, url=url, status=status)
    assert requester.submit(answers()).get_json()["error"]["match"] == match


def test_cached_lookup_cannot_bypass_the_submit_scan(requester, fake, clock):
    assert requester.lookup(name="Zorblax Industries").get_json()["match"] is None
    fake.add_vendor(name="Zorblax Industries", status="ACTIVE")
    assert requester.lookup(name="Zorblax Industries").get_json()["match"] is None
    resp = requester.submit(answers())
    assert resp.status_code == 409 and resp.get_json()["error"]["code"] == "VENDOR_EXISTS" and fake.post_count == 0


def test_retry_closes_when_the_vendor_appeared_meanwhile(admin, requester, fake, conn):
    fake.create_mode = "not_sent"
    sid = requester.submit(answers()).get_json()["submissionId"]
    fake.add_vendor(name="Zorblax Industries", status="ACTIVE")
    resp = admin.retry(sid)
    error = resp.get_json()["error"]
    assert resp.status_code == 409 and error["code"] == "VENDOR_EXISTS" and error["match"] == {"kind": "existing", "fields": ["name"]}
    assert (sub(conn, sid)["state"], sub(conn, sid)["state_reason"]) == ("CANCELLED", "VENDOR_EXISTS")
    assert fake.post_count == 1 and len(attempts(conn, sid)) == 1


def test_closed_submission_does_not_block_a_later_one_for_a_vanished_vendor(requester, fake):
    vendor = fake.add_vendor(name="Zorblax Industries", status="ACTIVE")
    assert requester.submit(answers()).status_code == 409
    del fake.vendors[vendor]
    assert requester.submit(answers()).status_code == 201 and fake.post_count == 1


def resolve(admin, sid, **body):
    decision = body.pop("decision")
    return admin.post(f"/api/admin/submissions/{sid}/resolve", {
        "decision": decision, "reason": "confirmed distinct", "nonce": admin.nonce(sid, decision), **body})


def test_legacy_duplicate_row_can_still_be_confirmed(app, admin, requester, fake, conn):
    vendor = fake.add_vendor(name="Zorblax Industries", status="ACTIVE")
    sid = legacy_duplicate(app, conn, requester, vendor)
    done = resolve(admin, sid, decision="CONFIRM_NEW")
    assert done.status_code == 200 and sub(conn, sid)["state"] == "CREATED" and fake.post_count == 1


def test_legacy_confirm_with_a_new_match_returns_to_review(app, admin, requester, fake, conn):
    vendor = fake.add_vendor(name="Zorblax Industries", status="ACTIVE")
    sid = legacy_duplicate(app, conn, requester, vendor)
    fake.add_vendor(name="Another Name", url="https://zorblax.example", status="ARCHIVED")
    resp = resolve(admin, sid, decision="CONFIRM_NEW")
    assert resp.status_code == 409 and resp.get_json()["error"]["code"] == "CANDIDATES_CHANGED"
    assert (sub(conn, sid)["state"], sub(conn, sid)["state_reason"]) == ("NEEDS_REVIEW", "DUPLICATE_SUSPECTED")
    assert fake.post_count == 0


def held_by_local_match(app, conn, admin, requester, other, fake):
    fake.create_mode = "not_sent"
    first = requester.submit(answers()).get_json()["submissionId"]
    fake.create_mode = "ok"
    second = other.submit(answers())
    assert second.status_code == 202
    assert resolve(admin, first, decision="CANCEL").status_code == 200
    return admin, second.get_json()["submissionId"]


# Restoring a backup revokes every session, so the reviewing administrator signs in again.
def held_by_restore(app, conn, admin, requester, other, fake):
    fake.create_mode = "not_sent"
    sid = requester.submit(answers()).get_json()["submissionId"]
    fake.create_mode = "ok"
    assert submissions.restore_flag(conn) == 1
    restored = make_actor(app, conn, "restored-admin@example.com", "ADMIN")
    conn.execute("UPDATE settings SET connection_state = 'ACTIVE', writes_enabled = 1")
    assert restored.reconcile(sid).get_json()["reconcile"] == "NO_MARKER_MATCH"
    return restored, sid


HOLDS = {"local match": held_by_local_match, "restored record": held_by_restore}


@pytest.mark.parametrize("hold", HOLDS.values(), ids=HOLDS.keys())
def test_confirm_new_cannot_override_a_drata_match(app, conn, admin, requester, other, fake, hold):
    reviewer, sid = hold(app, conn, admin, requester, other, fake)
    assert sub(conn, sid)["state"] == "NEEDS_REVIEW"
    posts = fake.post_count
    fake.add_vendor(name="Zorblax Industries", status="ACTIVE")
    resp = resolve(reviewer, sid, decision="CONFIRM_NEW")
    error = resp.get_json()["error"]
    assert resp.status_code == 409 and error["code"] == "VENDOR_EXISTS" and error["match"] == {"kind": "existing", "fields": ["name"]}
    assert (sub(conn, sid)["state"], sub(conn, sid)["state_reason"]) == ("CANCELLED", "VENDOR_EXISTS")
    assert resolve(reviewer, sid, decision="CONFIRM_NEW").status_code == 409
    assert fake.post_count == posts


def test_confirm_new_without_a_match_still_creates(app, conn, admin, requester, other, fake):
    reviewer, sid = held_by_local_match(app, conn, admin, requester, other, fake)
    done = resolve(reviewer, sid, decision="CONFIRM_NEW")
    assert done.status_code == 200 and sub(conn, sid)["state"] == "CREATED" and fake.post_count == 2
