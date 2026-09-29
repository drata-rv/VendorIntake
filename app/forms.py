import json

from . import mappings
from .connection import client_for, summarize_definitions
from .db import audit, iso, settings_row, tx
from .drata import ApiError, DrataError
from .errors import ApiFail


def seed_starter(conn) -> None:
    schema = mappings.normalize_form(mappings.starter_form())
    conn.execute("INSERT INTO form_versions (schema_json, created_by, created_at) VALUES (?, NULL, ?)",
                 (json.dumps(schema, sort_keys=True), iso()))
    conn.execute("INSERT INTO active_form (id, version, enabled) VALUES (1, NULL, 0)")


def get_version(conn, version: int) -> dict | None:
    row = conn.execute("SELECT schema_json FROM form_versions WHERE version = ?", (version,)).fetchone()
    return json.loads(row["schema_json"]) if row else None


def latest_version(conn) -> int:
    return conn.execute("SELECT MAX(version) AS v FROM form_versions").fetchone()["v"]


def active(conn) -> dict:
    row = conn.execute("SELECT version, enabled, definitions_json FROM active_form WHERE id = 1").fetchone()
    return {"version": row["version"], "enabled": bool(row["enabled"]),
            "definitions": json.loads(row["definitions_json"]) if row["definitions_json"] else None}


def _typed(obj: dict, keys, kind) -> bool:
    return all(obj.get(k) is None or (isinstance(obj[k], kind) and not isinstance(obj[k], bool)) for k in keys)


def shape_ok(schema) -> bool:
    if not (isinstance(schema, dict) and isinstance(schema.get("fields"), list)
            and len(schema["fields"]) <= mappings.MAX_FIELDS and _typed(schema, ("currency",), str)):
        return False
    for f in schema["fields"]:
        if not (isinstance(f, dict) and isinstance(f.get("destination"), dict)):
            return False
        dest = f["destination"]
        opts = f.get("options")
        if not (_typed(f, ("id", "type", "label", "helpText"), str) and _typed(f, ("maxLength",), int)
                and _typed(dest, ("kind", "field", "customType", "reason"), str) and _typed(dest, ("customFieldId",), int)
                and (opts is None or (isinstance(opts, list) and all(isinstance(o, dict) for o in opts)))):
            return False
    return True


def save_version(conn, actor: str, schema) -> dict:
    if not shape_ok(schema):
        raise ApiFail(422, "FORM_INVALID", "Form schema malformed.", {"_form": "Malformed schema."})
    normalized = mappings.normalize_form(schema)
    row = settings_row(conn)
    errors = mappings.validate_form(normalized, None, bool(row["custom_fields_enabled"]))
    with tx(conn):
        cur = conn.execute("INSERT INTO form_versions (schema_json, created_by, created_at) VALUES (?, ?, ?)",
                           (json.dumps(normalized, sort_keys=True), actor, iso()))
        audit(conn, actor, "FORM_SAVED", str(cur.lastrowid), {"validationErrors": len(errors)})
    return {"version": cur.lastrowid, "schema": normalized, "errors": errors}


def fetch_definitions(row) -> dict:
    client = client_for(row)
    try:
        return client.custom_field_definitions()
    except (ApiError, DrataError) as exc:
        raise ApiFail(502, "DEFINITIONS_UNAVAILABLE", "Custom field definitions could not be read from Drata.") from exc
    finally:
        client.close()


# Snapshot compared before each custom-field write to detect tenant drift.
def definition_fingerprint(defs: dict, mapped_ids) -> dict:
    keys = ("type", "isRequired", "readOnly")
    fp = {"mapped": {str(i): _pick(defs.get(i), keys) for i in sorted(mapped_ids)}, "required": sorted(
        i for i, d in defs.items() if "VENDOR" in (d.get("entityTypes") or []) and d.get("isRequired"))}
    return fp


def _pick(d, keys):
    if d is None:
        return None
    return {**{k: d.get(k) for k in keys}, "vendor": "VENDOR" in (d.get("entityTypes") or [])}


def custom_ids(schema: dict) -> list[int]:
    return [f["destination"]["customFieldId"] for f in schema["fields"] if f["destination"]["kind"] == "custom"]


def publish(conn, actor: str, version: int) -> dict:
    schema = get_version(conn, version)
    if schema is None:
        raise ApiFail(404, "NOT_FOUND", "Form version not found.")
    row = settings_row(conn)
    if row["connection_state"] != "ACTIVE" or not row["writes_enabled"]:
        raise ApiFail(409, "CONNECTION_UNAVAILABLE", "Activate the Drata connection before publishing.")
    defs = fetch_definitions(row) if row["custom_fields_enabled"] else None
    errors = mappings.validate_form(schema, defs, bool(row["custom_fields_enabled"]))
    if errors:
        raise ApiFail(422, "FORM_INVALID", "Form has mapping errors.", {e["fieldId"] or "_form": e["message"] for e in errors})
    snapshot = definition_fingerprint(defs, custom_ids(schema)) if defs is not None else None
    with tx(conn):
        conn.execute("UPDATE active_form SET version = ?, enabled = 1, definitions_json = ? WHERE id = 1",
                     (version, json.dumps(snapshot, sort_keys=True) if snapshot else None))
        audit(conn, actor, "FORM_PUBLISHED", str(version))
    return active(conn)


def disable(conn, actor: str) -> dict:
    with tx(conn):
        conn.execute("UPDATE active_form SET enabled = 0 WHERE id = 1")
        audit(conn, actor, "FORM_DISABLED")
    return active(conn)


def definitions_for_editor(row) -> list[dict] | None:
    if not row["custom_fields_enabled"] or row["connection_state"] != "ACTIVE":
        return None
    return summarize_definitions(fetch_definitions(row))
