import hashlib
import json
import secrets
import sqlite3
import uuid
from datetime import timedelta

from flask import current_app

from . import forms, mappings
from .connection import acquire_write_lock, block_connection, client_for
from .crypto import dec_json, enc_json
from .db import audit, get_db, in_seconds, iso, new_id, parse_iso, settings_row, tx, utcnow
from .drata import ApiError, Budget, BudgetExceeded, DrataError, ScanIncomplete, Transport, safe_link
from .errors import ApiFail

TERMINAL = {"CREATED", "UPDATED", "CANCELLED"}
UNRESOLVED_WRITE = ("IN_PROGRESS", "UNKNOWN", "NEEDS_REVIEW", "RETRYABLE", "BLOCKED")
NONCE_TTL = 300
DEFAULT_RATE_LIMIT_SECONDS = 60
ENC_COLUMNS = ("answers_enc", "payload_enc", "snapshot_enc", "candidates_enc")
UPDATE_PROPS = sorted({(s.get("update_prop") or s["prop"]) for k, s in mappings.NATIVE.items() if k in mappings.UPDATABLE})

MESSAGES = {
    "CREATED": "Prospective vendor created in Drata.",
    "UPDATED": "Existing prospective vendor updated in Drata.",
    "IN_PROGRESS": "Submission is being processed.",
    "NEEDS_REVIEW": "Submission needs administrator review before anything is sent to Drata.",
    "NEEDS_CORRECTION": "Drata rejected the submission. Correct the entry and submit again.",
    "BLOCKED": "Submission is blocked by the Drata connection. An administrator must resolve it.",
    "RETRYABLE": "Nothing was sent to Drata. An administrator can retry.",
    "UNKNOWN": "Outcome not confirmed. An administrator must reconcile with Drata before anything else is sent.",
    "CANCELLED": "Submission closed.",
}


def row_or_404(conn, sid: str):
    row = conn.execute("SELECT * FROM submissions WHERE id = ?", (sid,)).fetchone()
    if row is None:
        raise ApiFail(404, "NOT_FOUND", "Submission not found.")
    return row


def authorized_row(conn, sid: str, user: dict):
    row = row_or_404(conn, sid)
    if user["role"] != "ADMIN" and row["requester_id"] != user["id"]:
        raise ApiFail(404, "NOT_FOUND", "Submission not found.")
    return row


def set_state(conn, sid: str, state: str, reason: str | None = None, error: str | None = None, actor: str = "SYSTEM", **cols):
    now = iso()
    sets = {"state": state, "state_reason": reason, "last_error": error, "updated_at": now,
            "terminal_at": now if state in TERMINAL else None, **cols}
    with tx(conn):
        prev = conn.execute("SELECT state FROM submissions WHERE id = ?", (sid,)).fetchone()["state"]
        conn.execute(f"UPDATE submissions SET {', '.join(k + ' = ?' for k in sets)} WHERE id = ?", (*sets.values(), sid))
        audit(conn, actor, "SUBMISSION_STATE", sid, {"from": prev, "to": state, "reason": reason})


def sha(obj) -> str:
    return hashlib.sha256(json.dumps(obj, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def request_hash(requester_id: str, form_version: int, clean: dict) -> str:
    return sha({"requesterId": requester_id, "formVersion": form_version, "answers": clean})


def load_link(reply_vendor: dict) -> str | None:
    href = ((reply_vendor.get("_links") or {}).get("self") or {}).get("href")
    return safe_link(href, current_app.config["DRATA_LINK_HOSTS"])


def public_body(row, admin: bool = False) -> dict:
    body = {"submissionId": row["id"], "state": row["state"], "message": MESSAGES[row["state"]],
            "drataId": row["result_drata_id"], "link": row["result_link"], "operation": row["operation"]}
    if admin:
        body["reason"] = row["state_reason"]
    return body


def respond(row, replay: bool = False):
    body = public_body(row)
    state = row["state"]
    if replay:
        return 200, body
    if state == "CREATED":
        return 201, body
    if state == "UPDATED":
        return 200, body
    if state == "NEEDS_CORRECTION":
        raise ApiFail(422, "DRATA_REJECTED", MESSAGES[state], submission_id=row["id"], extra={"state": state})
    return 202, body


def _decrypt_payload(row):
    return dec_json(row["payload_enc"]) if row["payload_enc"] else None


# ---------------------------------------------------------------- nonce

def _candidate_ids(row) -> list:
    return sorted(str(c["id"]) for c in (dec_json(row["candidates_enc"]) or [])) if row["candidates_enc"] else []


def _binding(row, action: str, extra) -> str:
    return sha([row["id"], action, _candidate_ids(row), extra])


def issue_nonce(conn, sid: str, action: str, extra=None) -> dict:
    row = row_or_404(conn, sid)
    nonce, expires = secrets.token_urlsafe(24), in_seconds(NONCE_TTL)
    with tx(conn):
        conn.execute("UPDATE submissions SET action_nonce = ?, action_binding = ?, action_expires_at = ? WHERE id = ?",
                     (sha(nonce), _binding(row, action, extra), expires, sid))
    return {"nonce": nonce, "expiresAt": expires}


def consume_nonce(conn, sid: str, action: str, nonce, extra=None) -> None:
    row = row_or_404(conn, sid)
    if not isinstance(nonce, str) or not nonce:
        raise ApiFail(409, "NONCE_INVALID", "Confirmation expired or already used. Review and confirm again.")
    with tx(conn):
        cur = conn.execute(
            "UPDATE submissions SET action_nonce = NULL, action_binding = NULL, action_expires_at = NULL"
            " WHERE id = ? AND action_nonce = ? AND action_binding = ? AND action_expires_at > ?",
            (sid, sha(nonce), _binding(row, action, extra), iso()))
    if cur.rowcount != 1:
        raise ApiFail(409, "NONCE_INVALID", "Confirmation expired or already used. Review and confirm again.")


# ---------------------------------------------------------------- submit

def _clean_key(key: str | None) -> str:
    try:
        return str(uuid.UUID(key or ""))
    except ValueError as exc:
        raise ApiFail(400, "IDEMPOTENCY_KEY_REQUIRED", "Idempotency-Key header must be a UUID.") from exc


def _by_key(conn, key: str):
    return conn.execute("SELECT * FROM submissions WHERE idempotency_key = ?", (key,)).fetchone()


def _replay(row, user, digest_value):
    if row["requester_id"] != user["id"] or row["request_hash"] != digest_value:
        raise ApiFail(409, "IDEMPOTENCY_CONFLICT", "This idempotency key was already used for a different request.")
    if row["payload_purged_at"] and row["state_reason"] == "RETENTION_EXPIRED":
        raise ApiFail(410, "PAYLOAD_EXPIRED", "Submission expired and was purged.", submission_id=row["id"])
    return respond(row, replay=True)


def _require_active_form(conn, version: int) -> None:
    active = forms.active(conn)
    if not active["enabled"]:
        raise ApiFail(503, "FORM_DISABLED", "Intake is not accepting submissions.")
    if active["version"] != version:
        raise ApiFail(409, "FORM_CHANGED", "Form changed. Review current fields before submitting.")


def _origin_of(conn, user, origin_id) -> str | None:
    if origin_id is None:
        return None
    row = conn.execute("SELECT requester_id, state FROM submissions WHERE id = ?", (origin_id,)).fetchone()
    if row is None or row["requester_id"] != user["id"] or row["state"] != "NEEDS_CORRECTION":
        raise ApiFail(422, "INVALID_INPUT", "originSubmissionId must reference your submission awaiting correction.")
    return origin_id


def submit(user: dict, body, idem_key: str | None):
    conn = get_db()
    key = _clean_key(idem_key)
    if not isinstance(body, dict) or not isinstance(body.get("formVersion"), int) or isinstance(body.get("formVersion"), bool):
        raise ApiFail(400, "INVALID_INPUT", "Body must be {formVersion, answers}.")
    schema = forms.get_version(conn, body["formVersion"])
    if schema is None:
        raise ApiFail(409, "FORM_CHANGED", "Form changed. Review current fields before submitting.")
    clean, errors = mappings.validate_answers(schema, body.get("answers"))
    if errors:
        raise ApiFail(422, "VALIDATION_FAILED", "Correct the highlighted fields.", errors)
    digest_value = request_hash(user["id"], body["formVersion"], clean)
    existing = _by_key(conn, key)
    if existing:
        return _replay(existing, user, digest_value)
    _require_active_form(conn, body["formVersion"])

    lock = acquire_write_lock()
    try:
        existing = _by_key(conn, key)
        if existing:
            return _replay(existing, user, digest_value)
        _require_active_form(conn, body["formVersion"])
        settings = settings_row(conn)
        if (settings["connection_state"] != "ACTIVE" or not settings["writes_enabled"]
                or not settings["attest_create"] or not settings["account_id"]):
            raise ApiFail(503, "CONNECTION_UNAVAILABLE", "Drata connection is not active.")
        origin = _origin_of(conn, user, body.get("originSubmissionId"))
        sid = new_id()
        payload = mappings.build_create_payload(schema, clean, mappings.marker(settings["installation_uuid"], sid))
        now = iso()
        try:
            with tx(conn):
                conn.execute(
                    "INSERT INTO submissions (id, requester_id, form_version, idempotency_key, request_hash, state, operation,"
                    " answers_enc, payload_enc, credential_version, origin_submission_id, created_at, updated_at)"
                    " VALUES (?, ?, ?, ?, ?, 'IN_PROGRESS', 'CREATE', ?, ?, ?, ?, ?, ?)",
                    (sid, user["id"], body["formVersion"], key, digest_value, enc_json(clean), enc_json(payload),
                     settings["credential_version"], origin, now, now))
                audit(conn, user["id"], "SUBMISSION_ACCEPTED", sid, {"formVersion": body["formVersion"]})
        except sqlite3.IntegrityError:
            return _replay(_by_key(conn, key), user, digest_value)
        _run_create(conn, sid, user["id"])
        return respond(row_or_404(conn, sid))
    finally:
        lock.release()


# ---------------------------------------------------------------- duplicate detection

def find_duplicates(vendors: dict, payload: dict) -> list[dict]:
    name, host = mappings.normalized_name(payload.get("name")), mappings.normalized_host(payload.get("url"))
    found = []
    for vendor in vendors.values():
        reasons = []
        if mappings.normalized_name(vendor.get("name")) == name:
            reasons.append("NAME")
        if host and mappings.normalized_host(vendor.get("url")) == host:
            reasons.append("HOST")
        if reasons:
            found.append({"id": vendor["id"], "name": vendor.get("name"), "url": vendor.get("url"),
                          "status": vendor.get("status"), "reasons": reasons})
    return found


def marker_matches(vendors: dict, mark: str) -> list[dict]:
    return [v for v in vendors.values() if mark in (v.get("notes") or "")]


def local_matches(conn, sid: str, payload: dict) -> list[dict]:
    name, host = mappings.normalized_name(payload.get("name")), mappings.normalized_host(payload.get("url"))
    marks = ",".join("?" * len(UNRESOLVED_WRITE))
    rows = conn.execute(
        f"SELECT id, payload_enc FROM submissions WHERE id != ? AND operation = 'CREATE' AND state IN ({marks})"
        " AND payload_enc IS NOT NULL", (sid, *UNRESOLVED_WRITE)).fetchall()
    found = []
    for other in rows:
        other_payload = dec_json(other["payload_enc"])
        same_name = mappings.normalized_name(other_payload.get("name")) == name
        same_host = host and mappings.normalized_host(other_payload.get("url")) == host
        if same_name or same_host:
            found.append({"id": f"L:{other['id']}", "name": None, "url": None, "status": None, "reasons": ["LOCAL_UNRESOLVED"]})
    return found


def _drifted(client, schema: dict) -> bool:
    ids = forms.custom_ids(schema)
    if not ids:
        return False
    stored = forms.active(get_db())["definitions"]
    current = forms.definition_fingerprint(client.custom_field_definitions(), ids)
    return stored != current


# ---------------------------------------------------------------- create pipeline

def _rate_limit_pending(settings) -> bool:
    return bool(settings["rate_limit_until"]) and settings["rate_limit_until"] > iso()


def _note_rate_limit(conn, reply) -> None:
    seconds = reply.retry_after if reply.retry_after is not None else DEFAULT_RATE_LIMIT_SECONDS
    with tx(conn):
        conn.execute("UPDATE settings SET rate_limit_until = ? WHERE id = 1", (in_seconds(seconds),))


def _preflight_failure(conn, sid, exc, actor):
    if isinstance(exc, ScanIncomplete):
        return set_state(conn, sid, "RETRYABLE", "SCAN_INCOMPLETE", "SCAN_INCOMPLETE", actor)
    if isinstance(exc, (Transport, BudgetExceeded)):
        return set_state(conn, sid, "RETRYABLE", "PREFLIGHT_TIMEOUT", exc.kind.upper(), actor)
    status = exc.reply.status
    if status in (401, 403):
        block_connection(conn, "PREFLIGHT_PERMISSION")
        return set_state(conn, sid, "BLOCKED", "DRATA_PERMISSION", f"HTTP_{status}", actor)
    if status == 402:
        return set_state(conn, sid, "BLOCKED", "CUSTOM_FIELDS_UNAVAILABLE", "HTTP_402", actor)
    if status == 412:
        return set_state(conn, sid, "BLOCKED", "TERMS_NOT_ACCEPTED", "HTTP_412", actor)
    if status == 429:
        _note_rate_limit(conn, exc.reply)
        return set_state(conn, sid, "RETRYABLE", "RATE_LIMITED", "HTTP_429", actor)
    if status >= 500:
        return set_state(conn, sid, "RETRYABLE", "PREFLIGHT_UPSTREAM", f"HTTP_{status}", actor)
    return set_state(conn, sid, "BLOCKED", "PREFLIGHT_REJECTED", f"HTTP_{status}", actor)


def _run_create(conn, sid: str, actor: str, approved: set | None = None) -> None:
    row = row_or_404(conn, sid)
    settings = settings_row(conn)
    schema, payload = forms.get_version(conn, row["form_version"]), _decrypt_payload(row)
    with tx(conn):
        set_state(conn, sid, "IN_PROGRESS", "PROCESSING", None, actor)
    if _rate_limit_pending(settings):
        return set_state(conn, sid, "RETRYABLE", "RATE_LIMITED", "RATE_LIMIT_WINDOW", actor)
    client = client_for(settings, Budget())
    mark = mappings.marker(settings["installation_uuid"], sid)
    try:
        try:
            if _drifted(client, schema):
                return set_state(conn, sid, "NEEDS_REVIEW", "CUSTOM_FIELD_DRIFT", None, actor)
            vendors = client.scan_vendors()
        except (ApiError, DrataError) as exc:
            return _preflight_failure(conn, sid, exc, actor)
        hits = marker_matches(vendors, mark)
        if hits:
            return _adopt_marker(conn, sid, client, hits, actor)
        candidates = find_duplicates(vendors, payload)
        local = local_matches(conn, sid, payload)
        pending = [c for c in candidates if approved is None or c["id"] not in approved]
        if pending or local:
            reason = "LOCAL_UNRESOLVED_MATCH" if local else "DUPLICATE_SUSPECTED"
            with tx(conn):
                set_state(conn, sid, "NEEDS_REVIEW", reason, None, actor, candidates_enc=enc_json(candidates + local))
            return None
        _dispatch_create(conn, sid, client, payload, row, actor)
    finally:
        client.close()


def _start_attempt(conn, sid: str, method: str, path: str, credential_version: int) -> int:
    with tx(conn):
        n = conn.execute("SELECT attempt_count FROM submissions WHERE id = ?", (sid,)).fetchone()["attempt_count"] + 1
        cur = conn.execute(
            "INSERT INTO attempts (submission_id, attempt_number, method, path_template, started_at, credential_version, dispatched)"
            " VALUES (?, ?, ?, ?, ?, ?, 1)", (sid, n, method, path, iso(), credential_version))
        conn.execute("UPDATE submissions SET attempt_count = ?, updated_at = ? WHERE id = ?", (n, iso(), sid))
    return cur.lastrowid


def _finish_attempt(conn, attempt_id: int, http_status, outcome: str, dispatched: bool = True) -> None:
    with tx(conn):
        conn.execute("UPDATE attempts SET finished_at = ?, http_status = ?, outcome = ?, dispatched = ? WHERE id = ?",
                     (iso(), http_status, outcome, int(dispatched), attempt_id))


def _dispatch_create(conn, sid, client, payload, row, actor):
    settings = settings_row(conn)
    attempt = _start_attempt(conn, sid, "POST", "/vendors", settings["credential_version"])
    # attempt row is committed with dispatched=1 before the send; a crash from here on resolves to UNKNOWN.
    try:
        reply = client.create_vendor(payload)
    except (Transport, BudgetExceeded) as exc:
        if exc.not_sent:
            _finish_attempt(conn, attempt, None, "NOT_SENT_" + exc.kind.upper(), dispatched=False)
            return set_state(conn, sid, "RETRYABLE", "NOT_SENT", exc.kind.upper(), actor)
        _finish_attempt(conn, attempt, None, "TRANSPORT_" + exc.kind.upper())
        return set_state(conn, sid, "UNKNOWN", "TRANSPORT_UNCERTAIN", exc.kind.upper(), actor)
    except Exception as exc:
        _finish_attempt(conn, attempt, None, "INTERNAL_" + exc.__class__.__name__)
        current_app.logger.error("dispatch failed", extra={"fields": {"submissionId": sid, "errorType": exc.__class__.__name__}})
        return set_state(conn, sid, "UNKNOWN", "INTERNAL_ERROR", exc.__class__.__name__, actor)
    body = reply.body
    if reply.status == 201 and isinstance(body, dict) and isinstance(body.get("id"), int) and not isinstance(body["id"], bool) and body["id"] > 0:
        _finish_attempt(conn, attempt, 201, "CREATED_RESPONSE")
        with tx(conn):
            conn.execute("UPDATE submissions SET result_drata_id = ?, updated_at = ? WHERE id = ?", (body["id"], iso(), sid))
            conn.execute("UPDATE settings SET write_observed_version = credential_version WHERE id = 1")
        return _verify_created(conn, sid, client, body["id"], payload, actor)
    _finish_attempt(conn, attempt, reply.status, "REJECTED" if reply.recognized_error else "UNRECOGNIZED")
    return _classify_failure(conn, sid, reply, actor)


def _classify_failure(conn, sid, reply, actor, update: bool = False):
    status, recognized = reply.status, reply.recognized_error
    if recognized and status == 400:
        return set_state(conn, sid, "NEEDS_CORRECTION", "DRATA_VALIDATION", "HTTP_400", actor)
    if recognized and status in (401, 403):
        block_connection(conn, "WRITE_PERMISSION")
        return set_state(conn, sid, "BLOCKED", "DRATA_PERMISSION", f"HTTP_{status}", actor)
    if recognized and status == 402:
        return set_state(conn, sid, "BLOCKED", "CUSTOM_FIELDS_UNAVAILABLE", "HTTP_402", actor)
    if recognized and status == 412:
        return set_state(conn, sid, "BLOCKED", "TERMS_NOT_ACCEPTED", "HTTP_412", actor)
    if recognized and status == 429:
        _note_rate_limit(conn, reply)
        return set_state(conn, sid, "RETRYABLE", "RATE_LIMITED", "HTTP_429", actor)
    if recognized and status == 404 and update:
        return set_state(conn, sid, "NEEDS_REVIEW", "TARGET_MISSING", "HTTP_404", actor)
    return set_state(conn, sid, "UNKNOWN", "UPSTREAM_UNCERTAIN", f"HTTP_{status}", actor)


def _verify_created(conn, sid, client, vendor_id, payload, actor):
    try:
        vendor = client.get_vendor(vendor_id, custom_fields="customFields" in payload)
    except (ApiError, DrataError):
        return set_state(conn, sid, "NEEDS_REVIEW", "VERIFY_PENDING", "VERIFY_UNAVAILABLE", actor)
    bad = mappings.mismatches(vendor, payload)
    if bad:
        return set_state(conn, sid, "NEEDS_REVIEW", "VERIFY_MISMATCH", ",".join(bad)[:500], actor,
                         result_link=load_link(vendor))
    with tx(conn):
        set_state(conn, sid, "CREATED", None, None, actor, result_link=load_link(vendor))


def _adopt_marker(conn, sid, client, hits, actor):
    if len(hits) > 1:
        cands = [{"id": v["id"], "name": v.get("name"), "url": v.get("url"), "status": v.get("status"), "reasons": ["MARKER"]} for v in hits]
        with tx(conn):
            return set_state(conn, sid, "NEEDS_REVIEW", "MULTIPLE_MARKER_MATCHES", None, actor, candidates_enc=enc_json(cands))
    vendor_id = hits[0]["id"]
    with tx(conn):
        conn.execute("UPDATE submissions SET result_drata_id = ? WHERE id = ?", (vendor_id, sid))
    return _verify_created(conn, sid, client, vendor_id, _decrypt_payload(row_or_404(conn, sid)), actor)


# ---------------------------------------------------------------- admin actions

def _ensure(row, states, code="STATE_CONFLICT", message="Submission is not in a state that allows this action."):
    if row["state"] not in states:
        raise ApiFail(409, code, message, submission_id=row["id"])


def _require_payload(row):
    if row["payload_purged_at"] or not row["payload_enc"]:
        raise ApiFail(410, "PAYLOAD_EXPIRED", "Submission payload was purged.", submission_id=row["id"])


def retry(actor: str, sid: str, nonce):
    conn = get_db()
    row = row_or_404(conn, sid)
    _ensure(row, ("RETRYABLE", "BLOCKED"))
    if row["operation"] != "CREATE":
        raise ApiFail(409, "USE_UPDATE_PREVIEW", "Updates need a fresh preview and approval.")
    _require_payload(row)
    settings = settings_row(conn)
    if row["state"] == "BLOCKED" and settings["connection_state"] != "ACTIVE":
        raise ApiFail(409, "CONNECTION_UNAVAILABLE", "Retest the Drata connection first.")
    if _rate_limit_pending(settings):
        raise ApiFail(409, "RATE_LIMITED", "Drata rate limit active.", extra={"retryAfter": settings["rate_limit_until"]})
    lock = acquire_write_lock()
    try:
        consume_nonce(conn, sid, "RETRY", nonce)
        with tx(conn):
            audit(conn, actor, "SUBMISSION_RETRY", sid)
        _run_create(conn, sid, actor)
    finally:
        lock.release()
    return respond(row_or_404(conn, sid), replay=True)


def reconcile(actor: str, sid: str):
    conn = get_db()
    row = row_or_404(conn, sid)
    _ensure(row, ("UNKNOWN", "NEEDS_REVIEW", "RETRYABLE", "BLOCKED"))
    settings = settings_row(conn)
    lock = acquire_write_lock()
    try:
        with tx(conn):
            audit(conn, actor, "SUBMISSION_RECONCILE", sid)
        client = client_for(settings, Budget())
        try:
            outcome = _reconcile_update(conn, row, client, actor) if row["operation"] == "UPDATE" else _reconcile_create(conn, row, client, actor)
        except (ApiError, DrataError) as exc:
            code = getattr(getattr(exc, "reply", None), "status", None)
            raise ApiFail(502, "RECONCILE_UNAVAILABLE", "Drata could not be read.", extra={"upstream": code}) from exc
        finally:
            client.close()
    finally:
        lock.release()
    body = public_body(row_or_404(conn, sid), admin=True)
    body["reconcile"] = outcome
    return 200, body


def _reconcile_create(conn, row, client, actor) -> str:
    sid, settings = row["id"], settings_row(conn)
    payload = _decrypt_payload(row)
    if row["result_drata_id"]:
        _verify_created(conn, sid, client, row["result_drata_id"], payload, actor)
        return "VERIFIED"
    _require_payload(row)
    vendors = client.scan_vendors()
    hits = marker_matches(vendors, mappings.marker(settings["installation_uuid"], sid))
    if hits:
        _adopt_marker(conn, sid, client, hits, actor)
        return "MARKER_MATCH"
    if row["state"] == "NEEDS_REVIEW" and row["state_reason"] == "RESTORE_RECONCILE":
        with tx(conn):
            set_state(conn, sid, "RETRYABLE", "RECONCILED_NO_MARKER", None, actor)
        return "NO_MARKER_MATCH_RETRYABLE"
    return "NO_MARKER_MATCH"


def resolve(actor: str, sid: str, body: dict):
    conn = get_db()
    row = row_or_404(conn, sid)
    decision = body.get("decision")
    if decision == "LINK_EXISTING":
        return _link_existing(conn, row, actor, body)
    if decision == "CANCEL":
        return _cancel(conn, row, actor, body)
    if decision == "CONFIRM_NEW":
        return _confirm_new(conn, row, actor, body)
    if decision == "RECREATE":
        return _recreate(conn, row, actor, body)
    raise ApiFail(422, "INVALID_INPUT", "decision must be LINK_EXISTING, CONFIRM_NEW, CANCEL or RECREATE.")


def _reason(body, minimum=10) -> str:
    text = str(body.get("reason") or "").strip()
    if not minimum <= len(text) <= 500:
        raise ApiFail(422, "INVALID_INPUT", f"Written reason of {minimum}-500 characters required.", {"reason": "Required."})
    return text


def _cancel(conn, row, actor, body):
    _ensure(row, ("UNKNOWN", "NEEDS_REVIEW", "RETRYABLE", "BLOCKED", "NEEDS_CORRECTION"))
    reason = _reason(body)
    consume_nonce(conn, row["id"], "CANCEL", body.get("nonce"))
    with tx(conn):
        set_state(conn, row["id"], "CANCELLED", "CANCELLED_BY_ADMIN", None, actor, duplicate_reason=reason)
        audit(conn, actor, "SUBMISSION_RESOLVE", row["id"], {"decision": "CANCEL"})
    return 200, public_body(row_or_404(conn, row["id"]), admin=True)


def _link_existing(conn, row, actor, body):
    _ensure(row, ("UNKNOWN", "NEEDS_REVIEW", "RETRYABLE", "BLOCKED"))
    target = body.get("targetDrataId")
    if not isinstance(target, int) or isinstance(target, bool) or target <= 0:
        raise ApiFail(422, "INVALID_INPUT", "targetDrataId required.", {"targetDrataId": "Required."})
    reason = _reason(body, 3)
    lock = acquire_write_lock()
    try:
        consume_nonce(conn, row["id"], "LINK_EXISTING", body.get("nonce"), target)
        client = client_for(settings_row(conn), Budget())
        try:
            vendor = client.get_vendor(target)
        except ApiError as exc:
            raise ApiFail(404 if exc.reply.status == 404 else 502, "TARGET_UNAVAILABLE", "Target vendor could not be verified.") from exc
        except DrataError as exc:
            raise ApiFail(502, "TARGET_UNAVAILABLE", "Target vendor could not be verified.") from exc
        finally:
            client.close()
        with tx(conn):
            set_state(conn, row["id"], "CANCELLED", "LINKED_EXISTING", None, actor, result_drata_id=target,
                      result_link=load_link(vendor), duplicate_reason=reason)
            audit(conn, actor, "SUBMISSION_RESOLVE", row["id"], {"decision": "LINK_EXISTING", "target": target})
    finally:
        lock.release()
    return 200, public_body(row_or_404(conn, row["id"]), admin=True)


def _confirm_new(conn, row, actor, body):
    _ensure(row, ("NEEDS_REVIEW",))
    if row["state_reason"] not in ("DUPLICATE_SUSPECTED", "LOCAL_UNRESOLVED_MATCH") or row["operation"] != "CREATE":
        raise ApiFail(409, "STATE_CONFLICT", "This review reason cannot be confirmed as new.")
    _require_payload(row)
    reason = _reason(body)
    approved = {c["id"] for c in dec_json(row["candidates_enc"]) or [] if not str(c["id"]).startswith("L:")}
    lock = acquire_write_lock()
    try:
        consume_nonce(conn, row["id"], "CONFIRM_NEW", body.get("nonce"))
        with tx(conn):
            conn.execute("UPDATE submissions SET duplicate_reason = ? WHERE id = ?", (reason, row["id"]))
            audit(conn, actor, "SUBMISSION_RESOLVE", row["id"], {"decision": "CONFIRM_NEW"})
        _run_create(conn, row["id"], actor, approved=approved)
    finally:
        lock.release()
    fresh = row_or_404(conn, row["id"])
    if fresh["state"] == "NEEDS_REVIEW":
        raise ApiFail(409, "CANDIDATES_CHANGED", "Matches changed since review. Review the new list.", submission_id=row["id"])
    return 200, public_body(fresh, admin=True)


def _recreate(conn, row, actor, body):
    _ensure(row, ("UNKNOWN",))
    _require_payload(row)
    reason = _reason(body)
    if body.get("acknowledgeDuplicateRisk") is not True:
        raise ApiFail(422, "ACKNOWLEDGEMENT_REQUIRED", "Acknowledge the duplicate risk.")
    settings = settings_row(conn)
    schema, clean = forms.get_version(conn, row["form_version"]), dec_json(row["answers_enc"])
    new_sid, now = new_id(), iso()
    payload = mappings.build_create_payload(schema, clean, mappings.marker(settings["installation_uuid"], new_sid))
    lock = acquire_write_lock()
    try:
        consume_nonce(conn, row["id"], "RECREATE", body.get("nonce"))
        with tx(conn):
            set_state(conn, row["id"], "CANCELLED", "RECREATE_AUTHORIZED", None, actor, duplicate_reason=reason)
            conn.execute(
                "INSERT INTO submissions (id, requester_id, form_version, idempotency_key, request_hash, state, operation,"
                " answers_enc, payload_enc, credential_version, origin_submission_id, duplicate_reason, created_at, updated_at)"
                " VALUES (?, ?, ?, ?, ?, 'IN_PROGRESS', 'CREATE', ?, ?, ?, ?, ?, ?, ?)",
                (new_sid, row["requester_id"], row["form_version"], new_id(), row["request_hash"], enc_json(clean),
                 enc_json(payload), settings["credential_version"], row["id"], reason, now, now))
            audit(conn, actor, "SUBMISSION_RESOLVE", row["id"], {"decision": "RECREATE", "newSubmission": new_sid})
        _run_create(conn, new_sid, actor)
    finally:
        lock.release()
    return 200, public_body(row_or_404(conn, new_sid), admin=True)


# ---------------------------------------------------------------- update flow

def _update_eligible(row) -> bool:
    if row["operation"] == "UPDATE":
        return row["state"] in ("NEEDS_REVIEW", "RETRYABLE", "NEEDS_CORRECTION")
    return row["state"] == "NEEDS_REVIEW" and row["state_reason"] in ("DUPLICATE_SUSPECTED", "LOCAL_UNRESOLVED_MATCH", "MULTIPLE_MARKER_MATCHES")


def _dest_for_prop(prop: str) -> str:
    for name, spec in mappings.NATIVE.items():
        if prop in (spec["prop"], spec.get("update_prop")):
            return name
    return prop


def _build_diff(vendor, desired: dict) -> list[dict]:
    return [{"field": _dest_for_prop(p), "property": p, "before": mappings.vendor_prop(vendor, p), "after": v,
             "changed": not mappings.same(p, v, mappings.vendor_prop(vendor, p))} for p, v in desired.items()]


def _update_gate(conn):
    settings = settings_row(conn)
    if not settings["updates_enabled"] or settings["connection_state"] != "ACTIVE" or not settings["writes_enabled"]:
        raise ApiFail(409, "UPDATES_DISABLED", "Updates are disabled on the connection.")
    return settings


def update_preview(actor: str, sid: str, body: dict):
    conn = get_db()
    row = row_or_404(conn, sid)
    settings = _update_gate(conn)
    if not _update_eligible(row):
        raise ApiFail(409, "STATE_CONFLICT", "Submission is not eligible for an update.")
    _require_payload(row)
    target, fields = body.get("targetDrataId"), body.get("fields")
    if not isinstance(target, int) or isinstance(target, bool) or target <= 0 or not isinstance(fields, list) or not fields:
        raise ApiFail(422, "INVALID_INPUT", "targetDrataId and fields required.")
    schema, clean = forms.get_version(conn, row["form_version"]), dec_json(row["answers_enc"])
    allowed = mappings.updatable_fields(schema, clean)
    if any(f not in allowed for f in fields):
        raise ApiFail(422, "INVALID_INPUT", "Field not eligible for update.", {"fields": "Unsupported field selected."})
    desired = mappings.build_update_payload(schema, clean, fields)
    client = client_for(settings, Budget())
    try:
        vendor = client.get_vendor(target)
    except ApiError as exc:
        raise ApiFail(404 if exc.reply.status == 404 else 502, "TARGET_UNAVAILABLE", "Target vendor could not be read.") from exc
    except DrataError as exc:
        raise ApiFail(502, "TARGET_UNAVAILABLE", "Target vendor could not be read.") from exc
    finally:
        client.close()
    if vendor.get("status") != "PROSPECTIVE":
        raise ApiFail(409, "TARGET_NOT_PROSPECTIVE", "Only prospective vendors can be updated.")
    snapshot = {"targetId": target, "updatedAt": vendor.get("updatedAt"), "desired": desired, "fields": fields,
                "before": {p: mappings.vendor_prop(vendor, p) for p in UPDATE_PROPS}, "approvedAt": iso()}
    with tx(conn):
        conn.execute("UPDATE submissions SET snapshot_enc = ?, target_drata_id = ?, updated_at = ? WHERE id = ?",
                     (enc_json(snapshot), target, iso(), sid))
        audit(conn, actor, "UPDATE_PREVIEW", sid, {"target": target, "fields": fields})
    token = issue_nonce(conn, sid, "UPDATE_CONFIRM", sha({"t": target, "d": desired}))
    return 200, {"diff": _build_diff(vendor, desired), "target": {"id": target, "name": vendor.get("name")}, **token}


def update_confirm(actor: str, sid: str, nonce):
    conn = get_db()
    row = row_or_404(conn, sid)
    settings = _update_gate(conn)
    if not row["snapshot_enc"] or not _update_eligible(row):
        raise ApiFail(409, "STATE_CONFLICT", "Preview the update first.")
    snap = dec_json(row["snapshot_enc"])
    lock = acquire_write_lock()
    try:
        consume_nonce(conn, sid, "UPDATE_CONFIRM", nonce, sha({"t": snap["targetId"], "d": snap["desired"]}))
        client = client_for(settings, Budget())
        try:
            fresh = client.get_vendor(snap["targetId"])
        except ApiError as exc:
            raise ApiFail(404 if exc.reply.status == 404 else 502, "TARGET_UNAVAILABLE", "Target vendor could not be read.") from exc
        except DrataError as exc:
            raise ApiFail(502, "TARGET_UNAVAILABLE", "Target vendor could not be read.") from exc
        try:
            if _update_drifted(fresh, snap):
                lock.release()
                lock = None
                _, preview_body = update_preview(actor, sid, {"targetDrataId": snap["targetId"], "fields": snap["fields"]})
                raise ApiFail(409, "UPDATE_DRIFT", "Vendor changed since preview. Review the new diff.", extra={"preview": preview_body})
            _dispatch_update(conn, sid, client, snap, actor)
        finally:
            client.close()
    finally:
        if lock is not None:
            lock.release()
    return 200, public_body(row_or_404(conn, sid), admin=True)


def _update_drifted(vendor, snap) -> bool:
    if vendor.get("status") != "PROSPECTIVE" or vendor.get("updatedAt") != snap["updatedAt"]:
        return True
    return any(not mappings.same(p, snap["before"].get(p), mappings.vendor_prop(vendor, p)) for p in snap["desired"])


def _dispatch_update(conn, sid, client, snap, actor):
    settings = settings_row(conn)
    with tx(conn):
        set_state(conn, sid, "IN_PROGRESS", "UPDATE_PROCESSING", None, actor, operation="UPDATE")
        audit(conn, actor, "UPDATE_CONFIRM", sid, {"target": snap["targetId"]})
    attempt = _start_attempt(conn, sid, "PUT", "/vendors/{id}", settings["credential_version"])
    try:
        reply = client.update_vendor(snap["targetId"], snap["desired"])
    except (Transport, BudgetExceeded) as exc:
        if exc.not_sent:
            _finish_attempt(conn, attempt, None, "NOT_SENT_" + exc.kind.upper(), dispatched=False)
            return set_state(conn, sid, "RETRYABLE", "NOT_SENT", exc.kind.upper(), actor)
        _finish_attempt(conn, attempt, None, "TRANSPORT_" + exc.kind.upper())
        return set_state(conn, sid, "UNKNOWN", "TRANSPORT_UNCERTAIN", exc.kind.upper(), actor)
    except Exception as exc:
        _finish_attempt(conn, attempt, None, "INTERNAL_" + exc.__class__.__name__)
        return set_state(conn, sid, "UNKNOWN", "INTERNAL_ERROR", exc.__class__.__name__, actor)
    body = reply.body
    if reply.status == 200 and isinstance(body, dict) and body.get("id") == snap["targetId"]:
        _finish_attempt(conn, attempt, 200, "UPDATED_RESPONSE")
        with tx(conn):
            conn.execute("UPDATE submissions SET result_drata_id = ? WHERE id = ?", (snap["targetId"], sid))
        return _verify_updated(conn, sid, client, snap, actor)
    _finish_attempt(conn, attempt, reply.status, "REJECTED" if reply.recognized_error else "UNRECOGNIZED")
    return _classify_failure(conn, sid, reply, actor, update=True)


def _verify_updated(conn, sid, client, snap, actor):
    try:
        vendor = client.get_vendor(snap["targetId"])
    except (ApiError, DrataError):
        return set_state(conn, sid, "NEEDS_REVIEW", "VERIFY_PENDING", "VERIFY_UNAVAILABLE", actor)
    bad = [p for p, want in snap["desired"].items() if not mappings.same(p, want, mappings.vendor_prop(vendor, p))]
    bad += [p for p in UPDATE_PROPS if p not in snap["desired"] and not mappings.same(p, snap["before"].get(p), mappings.vendor_prop(vendor, p))]
    if vendor.get("status") != "PROSPECTIVE":
        bad.append("status")
    if bad:
        return set_state(conn, sid, "NEEDS_REVIEW", "VERIFY_MISMATCH", ",".join(bad)[:500], actor, result_link=load_link(vendor))
    with tx(conn):
        set_state(conn, sid, "UPDATED", None, None, actor, result_link=load_link(vendor))


def _reconcile_update(conn, row, client, actor) -> str:
    snap = dec_json(row["snapshot_enc"]) if row["snapshot_enc"] else None
    if snap is None:
        return "NO_SNAPSHOT"
    try:
        vendor = client.get_vendor(snap["targetId"])
    except ApiError as exc:
        if exc.reply.status == 404:
            with tx(conn):
                set_state(conn, row["id"], "NEEDS_REVIEW", "TARGET_MISSING", "HTTP_404", actor)
            return "TARGET_MISSING"
        raise
    if all(mappings.same(p, want, mappings.vendor_prop(vendor, p)) for p, want in snap["desired"].items()):
        _verify_updated(conn, row["id"], client, snap, actor)
        return "OBSERVED_APPLIED"
    with tx(conn):
        set_state(conn, row["id"], "NEEDS_REVIEW", "UPDATE_NOT_OBSERVED", None, actor)
    return "NOT_OBSERVED"


# ---------------------------------------------------------------- queries

def _vendor_name(row):
    if not row["payload_enc"]:
        return None
    return (_decrypt_payload(row) or {}).get("name")


def summarize(row, admin: bool, unresolved_days: int, emails: dict | None = None) -> dict:
    item = {"id": row["id"], "createdAt": row["created_at"], "updatedAt": row["updated_at"], "state": row["state"],
            "drataId": row["result_drata_id"], "link": row["result_link"], "operation": row["operation"],
            "vendorName": _vendor_name(row), "message": MESSAGES[row["state"]]}
    if admin:
        item.update({"reason": row["state_reason"], "requesterEmail": (emails or {}).get(row["requester_id"]),
                     "attemptCount": row["attempt_count"], "formVersion": row["form_version"]})
    if row["state"] not in TERMINAL and not row["payload_purged_at"]:
        age = (utcnow() - parse_iso(row["created_at"])).days
        item["expiresInDays"] = max(0, unresolved_days - age)
        item["retentionWarning"] = age >= unresolved_days - 30
    return item


def history(user: dict, state: str | None = None, limit: int = 200) -> list[dict]:
    conn = get_db()
    days = settings_row(conn)["retention_unresolved_days"]
    clauses, args = [], []
    if user["role"] != "ADMIN":
        clauses.append("requester_id = ?")
        args.append(user["id"])
    if state:
        clauses.append("state = ?")
        args.append(state)
    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
    rows = conn.execute(f"SELECT * FROM submissions {where} ORDER BY created_at DESC LIMIT ?", (*args, limit)).fetchall()
    emails = {r["id"]: r["email"] for r in conn.execute("SELECT id, email FROM users")} if user["role"] == "ADMIN" else None
    return [summarize(r, user["role"] == "ADMIN", days, emails) for r in rows]


def detail(user: dict, sid: str) -> dict:
    conn = get_db()
    row = authorized_row(conn, sid, user)
    admin = user["role"] == "ADMIN"
    days = settings_row(conn)["retention_unresolved_days"]
    emails = {r["id"]: r["email"] for r in conn.execute("SELECT id, email FROM users")} if admin else None
    out = summarize(row, admin, days, emails)
    if not admin:
        return out
    out["attempts"] = [dict(a) for a in conn.execute(
        "SELECT attempt_number, method, path_template, started_at, finished_at, http_status, outcome, dispatched"
        " FROM attempts WHERE submission_id = ? ORDER BY attempt_number", (sid,))]
    out["candidates"] = dec_json(row["candidates_enc"]) or []
    out["lastError"] = row["last_error"]
    out["duplicateReason"] = row["duplicate_reason"]
    out["originSubmissionId"] = row["origin_submission_id"]
    out["targetDrataId"] = row["target_drata_id"]
    out["payloadPurged"] = bool(row["payload_purged_at"])
    schema = forms.get_version(conn, row["form_version"])
    answers = dec_json(row["answers_enc"]) if row["answers_enc"] else None
    out["answers"] = None if answers is None else [
        {"id": f["id"], "label": f["label"], "destination": f["destination"], "value": answers.get(f["id"])} for f in schema["fields"]]
    out["payload"] = _decrypt_payload(row)
    out["updateEligible"] = _update_eligible(row) and bool(settings_row(conn)["updates_enabled"])
    out["events"] = [dict(e) for e in conn.execute(
        "SELECT actor, action, at, meta FROM audit_events WHERE subject = ? ORDER BY id DESC LIMIT 50", (sid,))]
    return out


def export(actor: str, sid: str) -> dict:
    conn = get_db()
    row = row_or_404(conn, sid)
    data = detail({"id": actor, "role": "ADMIN"}, sid)
    with tx(conn):
        audit(conn, actor, "SUBMISSION_EXPORT", sid)
    data["exportedAt"] = iso()
    data["idempotencyKey"] = row["idempotency_key"]
    return data


# ---------------------------------------------------------------- lifecycle

def recover_startup(conn) -> int:
    rows = conn.execute("SELECT id FROM submissions WHERE state = 'IN_PROGRESS'").fetchall()
    for r in rows:
        dispatched = conn.execute("SELECT 1 FROM attempts WHERE submission_id = ? AND dispatched = 1 LIMIT 1", (r["id"],)).fetchone()
        with tx(conn):
            set_state(conn, r["id"], "UNKNOWN" if dispatched else "RETRYABLE", "RESTART_RECOVERY", None, "SYSTEM")
            audit(conn, "SYSTEM", "STARTUP_RECOVERY", r["id"])
    return len(rows)


def restore_flag(conn) -> int:
    recover_startup(conn)
    rows = conn.execute("SELECT id FROM submissions WHERE state IN ('RETRYABLE', 'BLOCKED')").fetchall()
    with tx(conn):
        for r in rows:
            set_state(conn, r["id"], "NEEDS_REVIEW", "RESTORE_RECONCILE", None, "SYSTEM")
        conn.execute("DELETE FROM sessions")
        conn.execute("UPDATE settings SET writes_enabled = 0, connection_state = 'RESTORED_UNVERIFIED', updated_at = ? WHERE id = 1", (iso(),))
        audit(conn, "SYSTEM", "RESTORE_CHECK", None, {"flagged": len(rows)})
    return len(rows)


def run_cleanup(conn, actor: str = "SYSTEM") -> dict:
    s, now = settings_row(conn), utcnow()
    ago = lambda days: iso(now - timedelta(days=days))
    purge = ", ".join(f"{c} = NULL" for c in ENC_COLUMNS)
    with tx(conn):
        expired = conn.execute(
            "SELECT id FROM submissions WHERE state NOT IN ('CREATED', 'UPDATED', 'CANCELLED', 'IN_PROGRESS') AND created_at < ?",
            (ago(s["retention_unresolved_days"]),)).fetchall()
        for r in expired:
            set_state(conn, r["id"], "CANCELLED", "RETENTION_EXPIRED", None, actor)
            conn.execute(f"UPDATE submissions SET {purge}, action_nonce = NULL, payload_purged_at = ? WHERE id = ?", (iso(), r["id"]))
        purged = conn.execute(
            f"UPDATE submissions SET {purge}, payload_purged_at = ? WHERE payload_purged_at IS NULL"
            " AND state IN ('CREATED', 'UPDATED', 'CANCELLED') AND terminal_at < ?",
            (iso(), ago(s["retention_terminal_days"]))).rowcount
        old = [r["id"] for r in conn.execute(
            "SELECT id FROM submissions WHERE state IN ('CREATED', 'UPDATED', 'CANCELLED') AND terminal_at < ?",
            (ago(s["retention_ledger_days"]),))]
        for sid in old:
            conn.execute("UPDATE submissions SET origin_submission_id = NULL WHERE origin_submission_id = ?", (sid,))
            conn.execute("DELETE FROM submissions WHERE id = ?", (sid,))
        conn.execute("DELETE FROM audit_events WHERE at < ?", (ago(s["retention_ledger_days"]),))
        conn.execute("DELETE FROM sessions WHERE expires_at < ?", (iso(),))
        conn.execute("DELETE FROM login_limits WHERE window_start < ? AND (cooldown_until IS NULL OR cooldown_until < ?)",
                     (ago(1), iso()))
        conn.execute("UPDATE settings SET last_cleanup_at = ? WHERE id = 1", (iso(),))
        audit(conn, actor, "CLEANUP", None, {"expired": len(expired), "purged": purged, "ledgerDeleted": len(old)})
    return {"expired": len(expired), "purged": purged, "ledgerDeleted": len(old)}


def maybe_cleanup(conn) -> None:
    last = settings_row(conn)["last_cleanup_at"]
    if last is None or utcnow() - parse_iso(last) > timedelta(days=1):
        run_cleanup(conn, actor="SYSTEM")
