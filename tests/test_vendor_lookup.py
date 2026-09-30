import threading
import time

import pytest
from conftest import answers, envelope

from app import drata, load_config
from app.vendor_index import Unavailable

NONE = {"available": True, "match": None, "stale": False}
UNAVAILABLE = {"available": False, "reason": "UNAVAILABLE"}


def found(kind, *fields, stale=False):
    return {"available": True, "match": {"kind": kind, "fields": list(fields)}, "stale": stale}


def scans(fake):
    return fake.calls.count("scan_vendors")


LEGACY = {"name": "Legacy Holdings", "url": "https://Legacy.example/about"}
MATCHES = [
    ("prospective name", {"status": "PROSPECTIVE"}, {"name": "  legacy   HOLDINGS "}, found("prospective", "name")),
    ("null status is existing", {"status": None}, {"name": "legacy holdings"}, found("existing", "name")),
    ("NONE status is existing", {"status": "NONE"}, {"name": "legacy holdings"}, found("existing", "name")),
    ("archived, scheme-less host", {"status": "ARCHIVED"}, {"website": "LEGACY.example./other"}, found("existing", "website")),
    ("host with port and path", {"status": "PROSPECTIVE"}, {"website": "legacy.example:8443/x?y=1"}, found("prospective", "website")),
    ("both fields", {"status": "ACTIVE"}, {"name": "Legacy Holdings", "website": "https://legacy.example"},
     found("existing", "name", "website")),
    ("www host is distinct", {"status": "ACTIVE"}, {"website": "www.legacy.example"}, NONE),
    ("unrelated vendor", {"status": "PROSPECTIVE"}, {"name": "Other Co", "website": "other.example"}, NONE),
]


@pytest.mark.parametrize("vendor,query,expected", [c[1:] for c in MATCHES], ids=[c[0] for c in MATCHES])
def test_match_kind_and_fields(requester, fake, vendor, query, expected):
    fake.add_vendor(**LEGACY, **vendor)
    resp = requester.lookup(**query)
    assert resp.status_code == 200 and resp.get_json() == expected


RANKING = [
    ("more fields beat existing", [{"name": "Acme", "url": "https://acme.example", "status": "PROSPECTIVE"},
                                   {"name": "Acme", "url": None, "status": "ACTIVE"}], found("prospective", "name", "website")),
    ("tie prefers existing", [{"name": "Acme", "url": None, "status": "PROSPECTIVE"},
                              {"name": "Other", "url": "https://acme.example", "status": "ARCHIVED"}], found("existing", "website")),
]


@pytest.mark.parametrize("vendors,expected", [c[1:] for c in RANKING], ids=[c[0] for c in RANKING])
def test_best_match_among_several_vendors(requester, fake, vendors, expected):
    for vendor in vendors:
        fake.add_vendor(**vendor)
    assert requester.lookup(name="acme", website="acme.example").get_json() == expected


INVALID = [
    ({}, "_form"), ({"name": "   ", "website": ""}, "_form"), ({"name": "a"}, "name"), ({"name": " a "}, "name"),
    ({"name": "a", "website": "acme.example"}, "name"), ({"name": "n" * 192}, "name"), ({"website": "h" * 769}, "website"),
]


@pytest.mark.parametrize("query,field", INVALID, ids=[str(c[0])[:30] for c in INVALID])
def test_invalid_queries_are_rejected_without_contacting_drata(requester, fake, query, field):
    resp = requester.lookup(**query)
    error = resp.get_json()["error"]
    assert resp.status_code == 422 and error["code"] == "VALIDATION_FAILED" and field in error["fieldErrors"]
    assert fake.calls == []


@pytest.mark.parametrize("query", [{"name": "ab"}, {"name": "n" * 191}, {"website": "h" * 768}, {"website": "acme.example"}])
def test_boundary_queries_are_accepted(requester, query):
    assert requester.lookup(**query).get_json() == NONE


def test_lookup_needs_a_session_and_is_open_to_every_role(admin, requester, anon, fake):
    fake.add_vendor(**LEGACY)
    resp = anon.lookup(name="legacy holdings")
    assert resp.status_code == 401 and resp.get_json()["error"]["code"] == "UNAUTHENTICATED" and fake.calls == []
    assert admin.lookup(name="legacy holdings").get_json() == requester.lookup(name="legacy holdings").get_json()


def test_response_and_logs_disclose_only_kind_and_fields(admin, requester, fake, logs):
    fake.add_vendor(id=987654321, **LEGACY, status="PROSPECTIVE", notes="SECRET-NOTE",
                    _links={"self": {"href": "https://app.drata.test/vendors/987654321"}})
    bodies = []
    for actor in (admin, requester):
        for query in ({"name": "legacy holdings"}, {"website": "legacy.example"}, {"name": "Legacy Holdings", "website": "legacy.example"}):
            payload = actor.lookup(**query).get_json()
            assert set(payload) == {"available", "match", "stale"} and set(payload["match"]) == {"kind", "fields"}
            bodies.append(str(payload))
    seen = ("".join(bodies) + "\n".join(logs)).lower()
    for leak in ("987654321", "legacy", "holdings", "secret-path", "secret-note", "drata.test"):
        assert leak not in seen
    assert any('"lookup": "match"' in line for line in logs)


def test_lookup_throttle_is_per_user_and_expires(requester, other, fake, clock):
    statuses = [requester.lookup(name="Acme").status_code for _ in range(61)]
    assert statuses == [200] * 60 + [429]
    blocked = requester.lookup(name="Acme")
    assert blocked.get_json()["error"]["code"] == "RATE_LIMITED" and blocked.headers["Retry-After"] == "60"
    assert other.lookup(name="Acme").status_code == 200
    clock.now += 61
    assert requester.lookup(name="Acme").status_code == 200


def test_cache_serves_until_ttl_then_revalidates_while_serving_stale(requester, fake, clock):
    fake.add_vendor(name="Acme Corp", status="ACTIVE")
    assert requester.lookup(name="acme corp").get_json() == found("existing", "name")
    fake.add_vendor(name="Brand New", status="ACTIVE")
    clock.now += 299
    assert requester.lookup(name="brand new").get_json() == NONE and scans(fake) == 1
    clock.now += 2
    assert requester.lookup(name="brand new").get_json() == {**NONE, "stale": True} and scans(fake) == 2
    assert requester.lookup(name="brand new").get_json() == found("existing", "name") and scans(fake) == 2


def test_stale_entry_survives_failed_revalidation_but_not_thirty_minutes(requester, fake, clock):
    fake.add_vendor(name="Acme Corp")
    assert requester.lookup(name="acme corp").get_json() == found("prospective", "name")
    fake.scan_error = drata.Transport("timeout", False)
    clock.now += 400
    assert requester.lookup(name="acme corp").get_json() == found("prospective", "name", stale=True)
    clock.now += 5
    assert requester.lookup(name="acme corp").get_json()["stale"] is True and scans(fake) == 2
    clock.now += 1400
    assert requester.lookup(name="acme corp").get_json() == UNAVAILABLE


FAILURES = [drata.Transport("timeout", False), drata.ScanIncomplete("page limit"), drata.ApiError(envelope(500)),
            drata.ApiError(envelope(403)), RuntimeError("unexpected")]


@pytest.mark.parametrize("error", FAILURES, ids=[type(e).__name__ + str(i) for i, e in enumerate(FAILURES)])
def test_scan_failure_without_cache_is_unavailable_then_recovers(requester, fake, clock, error):
    fake.scan_error = error
    first = requester.lookup(name="acme corp")
    assert first.status_code == 200 and first.get_json() == UNAVAILABLE
    assert requester.lookup(name="acme corp").get_json() == UNAVAILABLE and scans(fake) == 1
    fake.scan_error = None
    clock.now += 11
    assert requester.lookup(name="acme corp").get_json() == NONE


@pytest.mark.parametrize("state", ["BLOCKED", "DISCONNECTED", "RESTORED_UNVERIFIED"])
def test_inactive_connection_is_unavailable_even_with_a_cache(requester, fake, conn, state):
    assert requester.lookup(name="acme corp").get_json() == NONE
    conn.execute("UPDATE settings SET connection_state = ?", (state,))
    calls = list(fake.calls)
    assert requester.lookup(name="acme corp").get_json() == UNAVAILABLE and fake.calls == calls


def test_cold_cache_scans_once_and_concurrent_lookups_do_not_wait(app, ready, fake, clock):
    index = app.extensions["vendor_index"]
    entered, release, outcomes = threading.Event(), threading.Event(), []
    real = fake.scan_vendors

    def slow():
        entered.set()
        release.wait(5)
        return real()

    def call():
        try:
            outcomes.append(index.snapshot()[0])
        except Unavailable:
            outcomes.append(None)

    fake.scan_vendors = slow
    leader = threading.Thread(target=call)
    leader.start()
    assert entered.wait(5)
    followers = [threading.Thread(target=call) for _ in range(4)]
    for thread in followers:
        thread.start()
    for thread in followers:
        thread.join(2)
    assert not any(t.is_alive() for t in followers) and outcomes == [None] * 4
    release.set()
    leader.join(5)
    assert len(outcomes) == 5 and outcomes[-1] is not None and index.snapshot()[0] is outcomes[-1] and scans(fake) == 1


def test_async_revalidation_returns_stale_immediately(app, requester, fake, clock):
    app.config["VENDOR_INDEX_ASYNC_REFRESH"] = True
    index = app.extensions["vendor_index"]
    fake.add_vendor(name="Acme Corp")
    requester.lookup(name="acme corp")
    entered, release, real = threading.Event(), threading.Event(), fake.scan_vendors

    def slow():
        entered.set()
        release.wait(5)
        return real()

    fake.scan_vendors = slow
    fake.add_vendor(name="Brand New")
    clock.now += 301
    assert requester.lookup(name="brand new").get_json() == {**NONE, "stale": True}
    assert entered.wait(5) and requester.lookup(name="brand new").get_json()["stale"] is True
    release.set()
    deadline = time.monotonic() + 5
    while index.snapshot()[1] and time.monotonic() < deadline:
        time.sleep(0.01)
    assert requester.lookup(name="brand new").get_json() == found("prospective", "name") and scans(fake) == 2


def test_bridge_create_is_indexed_without_a_rescan(requester, fake, clock):
    assert requester.lookup(name="Zorblax Industries").get_json() == NONE
    assert requester.submit(answers()).status_code == 201
    scanned = scans(fake)
    assert requester.lookup(name="zorblax industries", website="zorblax.example").get_json() == found("prospective", "name", "website")
    assert scans(fake) == scanned


def test_vendor_created_during_a_refresh_survives_it(app, ready, fake, clock):
    index = app.extensions["vendor_index"]
    entered, release, real = threading.Event(), threading.Event(), fake.scan_vendors

    def slow():
        entered.set()
        release.wait(5)
        return real()

    fake.scan_vendors = slow
    worker = threading.Thread(target=index.snapshot)
    worker.start()
    assert entered.wait(5)
    index.add(5, "Late Vendor", "https://late.example")
    release.set()
    worker.join(5)
    assert index.snapshot()[0].vendors[5].name == "late vendor"


TTL = [("300", 300.0), ("3600", 1800.0), ("0", 1.0), ("-5", 1.0), ("0.2", 1.0), ("1800", 1800.0)]


@pytest.mark.parametrize("raw,expected", TTL, ids=[c[0] for c in TTL])
def test_index_ttl_is_clamped_below_the_serving_limit(raw, expected):
    env = {"APP_BASE_URL": "https://bridge.test", "VENDOR_INDEX_TTL": raw}
    assert load_config(env)["VENDOR_INDEX_TTL"] == expected
    assert load_config({"APP_BASE_URL": "https://bridge.test"}, {"VENDOR_INDEX_TTL": raw})["VENDOR_INDEX_TTL"] == expected
