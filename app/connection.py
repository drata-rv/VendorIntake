from flask import current_app

from . import auth
from .crypto import decrypt, encrypt
from .db import audit, get_db, iso, settings_row, tx
from .drata import ApiError, Budget, DrataError, Transport
from .errors import ApiFail

LOCK_WAIT_SECONDS = 50
PERMISSIONS = {
    "company": "Company Settings: Get Company Settings",
    "vendors": "Vendors: List Vendors",
    "vendor": "Vendors: Get Vendor",
    "definitions": "Custom Field Definitions: Get Custom Field Definitions",
}


def factory():
    return current_app.extensions["drata_factory"]


def acquire_write_lock(blocking_seconds: float = 0):
    lock = current_app.extensions["write_lock"]
    got = lock.acquire(timeout=blocking_seconds) if blocking_seconds else lock.acquire(blocking=False)
    if not got:
        raise ApiFail(503, "BRIDGE_BUSY", "Another write is in progress. Retry shortly.", headers={"Retry-After": "5"})
    return lock


def stored_key(row) -> str | None:
    return decrypt(row["api_key_enc"]) if row["api_key_enc"] else None


def client_for(row, budget: Budget | None = None):
    key = stored_key(row)
    if row["connection_state"] != "ACTIVE" or not key:
        raise ApiFail(503, "CONNECTION_UNAVAILABLE", "Drata connection is not active.")
    return factory()(key, budget)


def block_connection(conn, reason: str) -> None:
    with tx(conn):
        conn.execute("UPDATE settings SET connection_state = 'BLOCKED', writes_enabled = 0, updated_at = ? WHERE id = 1", (iso(),))
        audit(conn, "SYSTEM", "SETTINGS_UPDATED", None, {"connectionState": "BLOCKED", "reason": reason})


def view(row) -> dict:
    return {
        "configured": bool(row["api_key_enc"]),
        "credentialVersion": row["credential_version"],
        "connectionState": row["connection_state"],
        "account": {"id": row["account_id"], "name": row["account_name"], "domain": row["account_domain"]},
        "readVerified": row["connection_state"] == "ACTIVE",
        "writeScopeAttested": bool(row["attest_create"]),
        "writeObserved": bool(row["credential_version"]) and row["write_observed_version"] == row["credential_version"],
        "updatesEnabled": bool(row["updates_enabled"]),
        "updateScopeAttested": bool(row["attest_update"]),
        "customFieldsEnabled": bool(row["custom_fields_enabled"]),
        "connectedAt": row["connected_at"],
        "updatedAt": row["updated_at"],
        "retention": {"terminalDays": row["retention_terminal_days"],
                      "unresolvedDays": row["retention_unresolved_days"],
                      "ledgerDays": row["retention_ledger_days"]},
    }


def _fail(step: str, exc: DrataError) -> dict:
    if isinstance(exc, ApiError):
        status = exc.reply.status
        code = {401: "PERMISSION_DENIED", 403: "PERMISSION_DENIED", 402: "FEATURE_UNAVAILABLE",
                412: "TERMS_NOT_ACCEPTED"}.get(status, "UPSTREAM_ERROR")
        return {"step": step, "code": code, "httpStatus": status,
                "permission": PERMISSIONS.get(step), "message": _messages(code, step)}
    return {"step": step, "code": "UNREACHABLE", "httpStatus": None, "permission": None,
            "message": "Drata could not be reached or returned an unreadable response."}


def _messages(code: str, step: str) -> str:
    return {
        "PERMISSION_DENIED": f"Drata rejected the credential. Required permission: {PERMISSIONS.get(step)}.",
        "FEATURE_UNAVAILABLE": "The tenant lacks the Custom Fields and Formulas feature.",
        "TERMS_NOT_ACCEPTED": "Accept the required Drata terms, then retest.",
        "UPSTREAM_ERROR": "Drata returned an unexpected response.",
    }[code]


def summarize_definitions(defs: dict) -> list[dict]:
    keep = ("customFieldId", "name", "type", "isRequired", "isHidden", "readOnly", "entityTypes")
    return [{k: d.get(k) for k in keep} for d in defs.values() if "VENDOR" in (d.get("entityTypes") or [])]


def run_test(api_key: str, custom_fields_enabled: bool) -> dict:
    client = factory()(api_key, Budget())
    result = {"ok": False, "account": None, "readVerified": False, "getVendor": "not_exercised",
              "customFields": None, "failure": None}
    step = "company"
    try:
        company = client.company()
        result["account"] = {"id": company["accountId"], "name": company.get("name"), "domain": company.get("domain")}
        step = "vendors"
        vendor_id = client.first_vendor_id()
        result["readVerified"] = True
        if vendor_id:
            step = "vendor"
            client.get_vendor(vendor_id)
            result["getVendor"] = "verified"
        if custom_fields_enabled:
            step = "definitions"
            result["customFields"] = summarize_definitions(client.custom_field_definitions())
        result["ok"] = True
    except (ApiError, Transport, DrataError) as exc:
        result["failure"] = _fail(step, exc)
    finally:
        client.close()
    return result


def test_credential(body: dict) -> dict:
    row = settings_row(get_db())
    key = _resolve_key(body, row)
    result = run_test(key, bool(body.get("customFieldsEnabled")))
    if result["ok"] and row["account_id"] and result["account"]["id"] != row["account_id"]:
        result["ok"] = False
        result["failure"] = {"step": "company", "code": "WRONG_ACCOUNT", "httpStatus": None, "permission": None,
                             "message": "Credential belongs to a different Drata account than this installation."}
    return result


def _resolve_key(body: dict, row) -> str:
    if body.get("useStored"):
        key = stored_key(row)
        if not key:
            raise ApiFail(409, "NO_STORED_CREDENTIAL", "No credential is stored.")
        return key
    key = body.get("apiKey")
    if not isinstance(key, str) or not key.strip() or len(key) > 512:
        raise ApiFail(422, "INVALID_INPUT", "API key required.", {"apiKey": "Required."})
    return key.strip()


# Holds the write lock so a credential swap cannot race an in-flight write.
def save_connection(actor: str, body: dict) -> dict:
    auth.require_recent_login()
    conn = get_db()
    lock = acquire_write_lock(LOCK_WAIT_SECONDS)
    try:
        row = settings_row(conn)
        use_stored = bool(body.get("useStored"))
        key = _resolve_key(body, row)
        result = test_credential(body)
        if not result["ok"]:
            raise ApiFail(422, "CONNECTION_TEST_FAILED", result["failure"]["message"], extra={"test": result})
        account = result["account"]
        if body.get("confirmAccountId") != account["id"]:
            raise ApiFail(422, "ACCOUNT_NOT_CONFIRMED", "Confirm the displayed Drata account.")
        if body.get("attestCreate") is not True:
            raise ApiFail(422, "ATTESTATION_REQUIRED", "Attest that the key has the Create Vendor scope.")
        updates = body.get("updatesEnabled") is True
        if updates and body.get("attestUpdate") is not True:
            raise ApiFail(422, "ATTESTATION_REQUIRED", "Attest that the key has the Update Vendor scope.")
        version = row["credential_version"] + (0 if use_stored else 1)
        with tx(conn):
            conn.execute(
                "UPDATE settings SET account_id = ?, account_name = ?, account_domain = ?, api_key_enc = ?,"
                " credential_version = ?, connection_state = 'ACTIVE', writes_enabled = 1, updates_enabled = ?,"
                " attest_create = 1, attest_update = ?, custom_fields_enabled = ?, connected_at = ?, updated_at = ?"
                " WHERE id = 1",
                (account["id"], account["name"], account["domain"], encrypt(key), version, int(updates),
                 int(updates), int(bool(body.get("customFieldsEnabled"))), iso(), iso()))
            audit(conn, actor, "CONNECTION_SAVED", None,
                  {"credentialVersion": version, "accountId": account["id"], "updatesEnabled": updates})
    finally:
        lock.release()
    return view(settings_row(conn))


def disconnect(actor: str) -> dict:
    auth.require_recent_login()
    conn = get_db()
    lock = acquire_write_lock(LOCK_WAIT_SECONDS)
    try:
        with tx(conn):
            conn.execute(
                "UPDATE settings SET api_key_enc = NULL, connection_state = 'DISCONNECTED', writes_enabled = 0,"
                " updates_enabled = 0, attest_create = 0, attest_update = 0, updated_at = ? WHERE id = 1", (iso(),))
            conn.execute("UPDATE active_form SET enabled = 0 WHERE id = 1")
            audit(conn, actor, "CONNECTION_DISCONNECTED")
    finally:
        lock.release()
    return view(settings_row(conn))


def update_retention(actor: str, body: dict) -> dict:
    limits = {"terminalDays": ("retention_terminal_days", 1, 365), "unresolvedDays": ("retention_unresolved_days", 7, 365),
              "ledgerDays": ("retention_ledger_days", 30, 3650)}
    conn = get_db()
    values = {}
    for name, (column, low, high) in limits.items():
        value = body.get(name)
        if not isinstance(value, int) or isinstance(value, bool) or not low <= value <= high:
            raise ApiFail(422, "INVALID_INPUT", f"{name} must be {low}-{high}.", {name: f"{low}-{high}"})
        values[column] = value
    with tx(conn):
        conn.execute("UPDATE settings SET retention_terminal_days = ?, retention_unresolved_days = ?,"
                     " retention_ledger_days = ?, updated_at = ? WHERE id = 1",
                     (values["retention_terminal_days"], values["retention_unresolved_days"],
                      values["retention_ledger_days"], iso()))
        audit(conn, actor, "SETTINGS_UPDATED", None, {"retention": values})
    return view(settings_row(conn))
