import math
import threading
import time
from collections import deque
from concurrent.futures import Future
from typing import NamedTuple

from flask import current_app

from . import mappings
from .connection import client_for
from .db import get_db, settings_row
from .drata import Budget
from .errors import ApiFail

MAX_AGE = 1800.0
FAILURE_BACKOFF = 10.0
LOOKUP_LIMIT, LOOKUP_WINDOW = 60, 60.0
NAME_MIN, NAME_MAX, WEBSITE_MAX = 2, 191, 768
UNAVAILABLE = {"available": False, "reason": "UNAVAILABLE"}
FIELD_OF_REASON = {"NAME": "name", "HOST": "website"}


class Unavailable(Exception):
    pass


class Indexed(NamedTuple):
    id: int
    name: str
    host: str | None
    status: str | None


class Snapshot(NamedTuple):
    vendors: dict
    built: float


def kind_of(status) -> str:
    return "prospective" if status == "PROSPECTIVE" else "existing"


# Most matched fields wins; a tie prefers an existing vendor.
def best_match(pairs) -> dict | None:
    best, rank = None, (0, False)
    for status, fields in pairs:
        kind = kind_of(status)
        if fields and (len(fields), kind == "existing") > rank:
            best, rank = {"kind": kind, "fields": list(fields)}, (len(fields), kind == "existing")
    return best


def candidate_match(candidates: list[dict]) -> dict | None:
    return best_match((c["status"], [FIELD_OF_REASON[r] for r in c["reasons"] if r in FIELD_OF_REASON]) for c in candidates)


def find_match(vendors, name: str, website: str) -> dict | None:
    want_name, want_host = mappings.normalized_name(name), mappings.normalized_host(website)

    def fields(vendor):
        return [f for f, hit in (("name", want_name and vendor.name == want_name),
                                 ("website", want_host and vendor.host == want_host)) if hit]

    return best_match((v.status, fields(v)) for v in vendors)


def parse_query(args) -> tuple[str, str]:
    name, website = (args.get("name") or "").strip(), (args.get("website") or "").strip()
    errors = {}
    if not name and not website:
        errors["_form"] = "Provide a vendor name or website."
    if name and len(name) < NAME_MIN:
        errors["name"] = f"Enter at least {NAME_MIN} characters."
    if len(name) > NAME_MAX:
        errors["name"] = f"Maximum {NAME_MAX} characters."
    if len(website) > WEBSITE_MAX:
        errors["website"] = f"Maximum {WEBSITE_MAX} characters."
    if errors:
        raise ApiFail(422, "VALIDATION_FAILED", "Correct the highlighted fields.", errors)
    return name, website


def _index(raw: dict) -> dict:
    items = (Indexed(vid, mappings.normalized_name(v.get("name")), mappings.normalized_host(v.get("url")), v.get("status"))
             for vid, v in raw.items())
    return {i.id: i for i in items if i.name or i.host}


class VendorIndex:
    def __init__(self, app, clock=time.monotonic):
        self.app, self.clock = app, clock
        self._lock = threading.Lock()
        self._snap: Snapshot | None = None
        self._flight: Future | None = None
        self._added: dict[int, tuple[float, Indexed]] = {}
        self._retry_at = 0.0

    def lookup(self, name: str, website: str) -> dict:
        row = settings_row(get_db())
        if row["connection_state"] != "ACTIVE" or not row["api_key_enc"]:
            return dict(UNAVAILABLE)
        try:
            snap, stale = self.snapshot()
        except Unavailable:
            return dict(UNAVAILABLE)
        return {"available": True, "match": find_match(snap.vendors.values(), name, website), "stale": stale}

    def snapshot(self) -> tuple[Snapshot, bool]:
        with self._lock:
            snap = self._snap
        if snap is not None:
            age = self.clock() - snap.built
            if age < self.app.config["VENDOR_INDEX_TTL"]:
                return snap, False
            if age < MAX_AGE:
                self._revalidate()
                return snap, True
        return self._cold_refresh(), False

    def add(self, vendor_id: int, name, url, status: str = "PROSPECTIVE") -> None:
        item = Indexed(vendor_id, mappings.normalized_name(name), mappings.normalized_host(url), status)
        with self._lock:
            if self._flight is not None:
                self._added[vendor_id] = (self.clock(), item)
            if self._snap is not None and vendor_id not in self._snap.vendors:
                self._snap = Snapshot({**self._snap.vendors, vendor_id: item}, self._snap.built)

    def _join_or_lead(self) -> tuple[Future, float | None]:
        with self._lock:
            if self._flight is not None:
                return self._flight, None
            self._flight = Future()
            return self._flight, self.clock()

    def _revalidate(self) -> None:
        if self.clock() < self._retry_at:
            return
        flight, started = self._join_or_lead()
        if started is None:
            return
        if self.app.config["VENDOR_INDEX_ASYNC_REFRESH"]:
            threading.Thread(target=self._lead, args=(flight, started), name="vendor-index", daemon=True).start()
        else:
            self._lead(flight, started)

    # Only the caller that starts the scan waits for it; others fail fast so a cold cache cannot pin every worker thread.
    def _cold_refresh(self) -> Snapshot:
        if self.clock() < self._retry_at:
            raise Unavailable()
        flight, started = self._join_or_lead()
        if started is None:
            raise Unavailable()
        self._lead(flight, started)
        return flight.result()

    def _lead(self, flight: Future, started: float) -> None:
        try:
            vendors = self._scan()
        except Exception as exc:
            self.app.logger.warning("vendor index refresh failed", extra={"fields": {"errorType": exc.__class__.__name__}})
            with self._lock:
                self._added.clear()
                self._flight, self._retry_at = None, self.clock() + FAILURE_BACKOFF
            flight.set_exception(Unavailable())
            return
        with self._lock:
            for vendor_id, (stamp, item) in self._added.items():
                if stamp >= started:
                    vendors.setdefault(vendor_id, item)
            self._added.clear()
            self._snap = snap = Snapshot(vendors, self.clock())
            self._flight, self._retry_at = None, 0.0
        flight.set_result(snap)

    def _scan(self) -> dict:
        with self.app.app_context():
            client = client_for(settings_row(get_db()), Budget())
            client.gate = self.app.extensions["index_gate"]
            try:
                return _index(client.scan_vendors())
            finally:
                client.close()


def note_created(vendor_id: int, name, url) -> None:
    current_app.extensions["vendor_index"].add(vendor_id, name, url)


class Throttle:
    def __init__(self, limit: int = LOOKUP_LIMIT, window: float = LOOKUP_WINDOW, clock=time.monotonic):
        self.limit, self.window, self.clock = limit, window, clock
        self._lock = threading.Lock()
        self._hits: dict[str, deque] = {}

    def enforce(self, key: str) -> None:
        now = self.clock()
        with self._lock:
            if len(self._hits) > 512:
                self._hits = {k: q for k, q in self._hits.items() if q and q[-1] > now - self.window}
            hits = self._hits.setdefault(key, deque())
            while hits and hits[0] <= now - self.window:
                hits.popleft()
            if len(hits) >= self.limit:
                wait = max(1, math.ceil(hits[0] + self.window - now))
                raise ApiFail(429, "RATE_LIMITED", "Too many lookups. Try again shortly.", headers={"Retry-After": str(wait)})
            hits.append(now)
