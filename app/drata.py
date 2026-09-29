import json
import threading
import time
from dataclasses import dataclass
from email.utils import parsedate_to_datetime
from urllib.parse import urlsplit

import requests
from requests.adapters import HTTPAdapter
from urllib3.exceptions import ConnectTimeoutError, NewConnectionError, MaxRetryError, SSLError as Urllib3SSLError

DEFAULT_BASE_URL = "https://public-api.drata.com/public/v2"
USER_AGENT = "VendorIntakeBridge/1.0"
CONNECT_TIMEOUT = 5
READ_TIMEOUT = 15
TOTAL_BUDGET = 45
MAX_PAGES = 20
MAX_BODY_BYTES = 32 * 1024 * 1024
ALL_STATUSES = ["PROSPECTIVE", "ACTIVE", "ARCHIVED", "APPROVED", "REJECTED", "FLAGGED",
                "ON_HOLD", "OFFBOARDED", "UNDER_REVIEW", "NONE"]


class DrataError(Exception):
    pass


class Transport(DrataError):
    def __init__(self, kind: str, not_sent: bool):
        super().__init__(kind)
        self.kind, self.not_sent = kind, not_sent


class BudgetExceeded(DrataError):
    not_sent = True
    kind = "budget"


class ScanIncomplete(DrataError):
    pass


@dataclass
class Reply:
    status: int
    body: object
    retry_after: float | None = None
    retry_after_invalid: bool = False

    @property
    def recognized_error(self) -> bool:
        # Drata error envelope: {statusCode, message, code, debugInfo}
        return (isinstance(self.body, dict) and self.body.get("statusCode") == self.status
                and isinstance(self.body.get("code"), int))


class ApiError(DrataError):
    def __init__(self, reply: Reply):
        super().__init__(f"HTTP {reply.status}")
        self.reply = reply


class Budget:
    def __init__(self, total: float = TOTAL_BUDGET):
        self.deadline = time.monotonic() + total

    def remaining(self) -> float:
        return self.deadline - time.monotonic()


# Process-wide minimum interval between outbound requests.
class Gate:
    def __init__(self, interval: float = 1.0):
        self.interval, self._last, self._lock = interval, 0.0, threading.Lock()

    def wait(self, budget: Budget) -> None:
        with self._lock:
            delay = self._last + self.interval - time.monotonic()
            if delay > 0:
                if delay >= budget.remaining():
                    raise BudgetExceeded()
                time.sleep(delay)
            self._last = time.monotonic()


def _never_connected(exc: requests.exceptions.ConnectionError) -> bool:
    inner = exc.args[0] if exc.args else None
    if isinstance(inner, MaxRetryError):
        return isinstance(inner.reason, (NewConnectionError, ConnectTimeoutError, Urllib3SSLError))
    return False


def parse_retry_after(value: str | None) -> tuple[float | None, bool]:
    if value is None:
        return None, False
    value = value.strip()
    if value.isdigit():
        return float(value), False
    try:
        when = parsedate_to_datetime(value)
        return max(0.0, when.timestamp() - time.time()), False
    except (TypeError, ValueError):
        return None, True


def safe_link(href, allowed_hosts) -> str | None:
    if not isinstance(href, str) or not allowed_hosts:
        return None
    parts = urlsplit(href)
    if parts.scheme != "https" or parts.username or parts.password or parts.hostname not in allowed_hosts:
        return None
    return href


class DrataClient:
    def __init__(self, base_url: str, api_key: str, budget: Budget | None = None, gate: Gate | None = None):
        self.base_url = base_url.rstrip("/")
        self._key = api_key
        self.budget = budget or Budget()
        self.gate = gate or Gate(0)
        self._session = requests.Session()
        self._session.mount("https://", HTTPAdapter(max_retries=0))
        self._session.mount("http://", HTTPAdapter(max_retries=0))

    def close(self) -> None:
        self._session.close()

    def request(self, method: str, path: str, params=None, body=None) -> Reply:
        left = self.budget.remaining()
        if left <= 0:
            raise BudgetExceeded()
        self.gate.wait(self.budget)
        left = self.budget.remaining()
        if left <= 0:
            raise BudgetExceeded()
        headers = {"Authorization": f"Bearer {self._key}", "Accept": "application/json",
                   "Content-Type": "application/json", "User-Agent": USER_AGENT}
        data = None if body is None else json.dumps(body, separators=(",", ":"), ensure_ascii=False).encode()
        resp = None
        try:
            resp = self._session.request(
                method, self.base_url + path, params=params, data=data, headers=headers,
                timeout=(min(CONNECT_TIMEOUT, left), min(READ_TIMEOUT, left)),
                allow_redirects=False, verify=True, stream=True)
            chunks, size = [], 0
            for chunk in resp.iter_content(65536):
                size += len(chunk)
                if size > MAX_BODY_BYTES:
                    raise Transport("oversize", False)
                chunks.append(chunk)
            raw = b"".join(chunks)
        except requests.exceptions.ConnectTimeout as exc:
            raise Transport("connect_timeout", True) from exc
        except requests.exceptions.ConnectionError as exc:
            raise Transport("connection", _never_connected(exc)) from exc
        except requests.exceptions.Timeout as exc:
            raise Transport("timeout", False) from exc
        except requests.exceptions.RequestException as exc:
            raise Transport("request", False) from exc
        finally:
            if resp is not None:
                resp.close()
        try:
            parsed = json.loads(raw) if raw else None
        except ValueError:
            parsed = None
        after, invalid = parse_retry_after(resp.headers.get("Retry-After"))
        return Reply(resp.status_code, parsed, after, invalid)

    def _get(self, path, params=None, expect=200) -> Reply:
        reply = self.request("GET", path, params)
        if reply.status != expect:
            raise ApiError(reply)
        return reply

    def company(self) -> dict:
        body = self._get("/company").body
        if not isinstance(body, dict) or not body.get("accountId"):
            raise Transport("malformed", True)
        return body

    def _pages(self, path, base_params, max_pages=MAX_PAGES):
        cursor, seen, out = None, set(), {}
        for _ in range(max_pages):
            params = list(base_params) + ([("cursor", cursor)] if cursor else [])
            body = self._get(path, params).body
            if not isinstance(body, dict) or not isinstance(body.get("data"), list):
                raise Transport("malformed", True)
            for item in body["data"]:
                if isinstance(item, dict):
                    out[item.get("id", item.get("customFieldId"))] = item
            cursor = (body.get("pagination") or {}).get("cursor")
            if not cursor:
                return out
            if cursor in seen:
                raise ScanIncomplete("repeated cursor")
            seen.add(cursor)
        raise ScanIncomplete("page limit")

    def scan_vendors(self) -> dict:
        # Unfiltered listing returns null-status vendors that a statuses[] filter drops.
        merged = self._pages("/vendors", [("size", 500)])
        merged.update(self._pages("/vendors", [("size", 500)] + [("statuses[]", s) for s in ALL_STATUSES]))
        return merged

    def first_vendor_id(self) -> int | None:
        body = self._get("/vendors", [("size", 1)]).body
        data = body.get("data") if isinstance(body, dict) else None
        return data[0].get("id") if data else None

    def get_vendor(self, vendor_id: int, custom_fields: bool = False) -> dict:
        params = [("expand[]", "customFields")] if custom_fields else None
        body = self._get(f"/vendors/{int(vendor_id)}", params).body
        if not isinstance(body, dict) or body.get("id") != int(vendor_id):
            raise Transport("malformed", True)
        return body

    def custom_field_definitions(self) -> dict:
        return self._pages("/custom-field-definitions", [("entityType", "VENDOR"), ("size", 500)])

    def create_vendor(self, payload: dict) -> Reply:
        return self.request("POST", "/vendors", body=payload)

    def update_vendor(self, vendor_id: int, payload: dict) -> Reply:
        return self.request("PUT", f"/vendors/{int(vendor_id)}", body=payload)
