import copy
import json
import re
import unicodedata
from decimal import Decimal, InvalidOperation
from urllib.parse import urlsplit

INPUT_TYPES = {"text", "textarea", "url", "email_list", "boolean", "select", "multiselect", "money"}
CUSTOM_TYPES = {"TEXT", "LONG_TEXT", "URL", "NUMBER"}
CUSTOM_INPUTS = {"TEXT": {"text"}, "LONG_TEXT": {"text", "textarea"}, "URL": {"url"}, "NUMBER": {"text"}}
CUSTOM_MAX = {"TEXT": 190, "LONG_TEXT": 10000, "URL": 768}
MAX_FIELDS = 30
MAX_OPTIONS = 50
FIELD_ID_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")

ENUMS = {
    "category": "ENGINEERING PRODUCT MARKETING CS SALES FINANCE HR ADMINISTRATIVE SECURITY LEGAL INFORMATION_TECHNOLOGY NONE".split(),
    "type": "VENDOR SUPPLIER CONTRACTOR PARTNER OTHER NONE".split(),
    "operationalImpact": "NONE LOW NORMAL IMPORTANT CRITICAL".split(),
    "environmentAccess": "NO READ_ONLY READ_WRITE".split(),
    "dataAccessedOrProcessedList": (
        "GENERAL PUBLIC CONTROLLED_UNCLASSIFIED FINANCIAL PROPRIETARY EMPLOYEE_PERSONNEL "
        "PERSONAL_IDENTIFIABLE_INFORMATION PROTECTED_HEALTH_INFORMATION OTHER_PERSONAL_OR_SENSITIVE CARDHOLDER_DATA"
    ).split(),
}

MARKER_TEMPLATE = "[VendorIntakeBridge:{installation}:{submission}]"
MARKER_RESERVE = 2 + len(MARKER_TEMPLATE.format(installation="0" * 36, submission="0" * 36))
MARKER_RE = re.compile(r"\[VendorIntakeBridge:([0-9a-f-]{36}):([0-9a-f-]{36})\]")

_URL = {"inputs": {"url"}, "max": 768}
_LONG = {"inputs": {"text", "textarea"}, "max": 30000}
NATIVE = {
    "name": {"inputs": {"text"}, "max": 191, "prop": "name", "must_require": True},
    "url": {**_URL, "prop": "url"},
    "privacyUrl": {**_URL, "prop": "privacyUrl"},
    "termsUrl": {**_URL, "prop": "termsUrl"},
    "trustCenterUrl": {**_URL, "prop": "trustCenterUrl"},
    "servicesProvided": {**_LONG, "prop": "servicesProvided"},
    "dataStored": {**_LONG, "prop": "dataStored"},
    "location": {**_LONG, "prop": "location"},
    "vendor_contact_name": {"inputs": {"text"}, "max": 191, "prop": "contactAtVendor"},
    # Create takes contactEmail; update and reads use contactsEmail.
    "vendor_contact_emails": {"inputs": {"email_list"}, "max": 191, "prop": "contactEmail", "update_prop": "contactsEmail"},
    "hasPii": {"inputs": {"boolean"}, "prop": "hasPii", "must_require": True},
    "isSubProcessor": {"inputs": {"boolean"}, "prop": "isSubProcessor", "must_require": True},
    "critical": {"inputs": {"boolean"}, "prop": "critical"},
    "category": {"inputs": {"select"}, "prop": "category", "enum": "category"},
    "type": {"inputs": {"select"}, "prop": "type", "enum": "type"},
    "cost": {"inputs": {"money"}, "prop": "cost"},
    "notes": {"inputs": {"text", "textarea"}, "max": 30000 - MARKER_RESERVE, "prop": "notes", "create_only": True},
    "operationalImpact": {"inputs": {"select"}, "prop": "operationalImpact", "enum": "operationalImpact"},
    "environmentAccess": {"inputs": {"select"}, "prop": "environmentAccess", "enum": "environmentAccess"},
    "dataAccessedOrProcessedList": {"inputs": {"multiselect"}, "prop": "dataAccessedOrProcessedList", "enum": "dataAccessedOrProcessedList"},
}
UPDATABLE = {k for k, v in NATIVE.items() if not v.get("create_only")}

_EMAIL_RE = re.compile(
    r"^[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]{1,64}@[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?"
    r"(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?)+$"
)
_MONEY_RE = re.compile(r"^\d{1,9}(\.\d{1,2})?$")
_NUMBER_RE = re.compile(r"^-?\d{1,10}(\.\d{1,6})?$")
_CTRL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f]")


ACRONYMS = {"CS", "HR"}


def humanize(value: str) -> str:
    return value if value in ACRONYMS else value.replace("_", " ").capitalize()


def starter_form() -> dict:
    def f(fid, label, ftype, dest, required=False, help_text="", **extra):
        return {"id": fid, "label": label, "type": ftype, "required": required, "helpText": help_text,
                "destination": dest, **extra}

    def native(name):
        return {"kind": "native", "field": name}

    def retained(reason):
        return {"kind": "retained_only", "reason": reason}

    return {"currency": "USD", "fields": [
        f("vendor_name", "Vendor name", "text", native("name"), True, "Use legal or commonly recognized vendor name.", maxLength=191),
        f("vendor_website", "Vendor website", "url", native("url"), True, "Used to warn about possible duplicates."),
        f("services_provided", "Services provided", "textarea", native("servicesProvided"), True),
        f("data_stored", "Data stored", "textarea", native("dataStored")),
        f("stores_pii", "Stores PII?", "boolean", native("hasPii"), True),
        f("is_subprocessor", "Subprocessor?", "boolean", native("isSubProcessor"), True),
        f("contact_name", "Vendor contact name", "text", native("vendor_contact_name")),
        f("contact_email", "Vendor contact email", "email_list", native("vendor_contact_emails"), False, "Up to 5 addresses, comma or semicolon separated."),
        f("privacy_url", "Privacy policy URL", "url", native("privacyUrl")),
        f("category", "Vendor category", "select", native("category")),
        f("vendor_type", "Vendor type", "select", native("type")),
        f("business_justification", "Business justification", "textarea", retained("Business context stays with the intake record."), False, maxLength=5000),
        f("replaces_existing", "Replaces existing vendor?", "text", retained("Procurement context stays with the intake record."), False, maxLength=191),
        f("procurement_reference", "Existing procurement request reference", "text", retained("Cross-reference to the customer's request system."), False, maxLength=191),
        f("intake_notes", "Additional intake notes", "textarea", native("notes")),
    ]}


def normalize_form(schema: dict) -> dict:
    out = copy.deepcopy(schema)
    out.setdefault("currency", "USD")
    for fld in out.get("fields", []):
        fld.setdefault("required", False)
        fld.setdefault("helpText", "")
        fld.setdefault("options", [])
        dest = fld.get("destination") or {}
        spec = NATIVE.get(dest.get("field")) if dest.get("kind") == "native" else None
        if spec and "maxLength" not in fld and "max" in spec:
            fld["maxLength"] = spec["max"]
        if spec and spec.get("enum") and not fld["options"]:
            fld["options"] = [{"value": v, "label": humanize(v)} for v in ENUMS[spec["enum"]]]
    return out


def validate_form(schema: dict, custom_defs: dict | None = None, custom_enabled: bool = False) -> list[dict]:
    errors: list[dict] = []

    def err(fid, msg):
        errors.append({"fieldId": fid, "message": msg})

    fields = schema.get("fields")
    if not isinstance(fields, list) or not fields:
        return [{"fieldId": None, "message": "Form needs at least one field."}]
    if len(fields) > MAX_FIELDS:
        err(None, f"Maximum {MAX_FIELDS} fields.")
    if not re.fullmatch(r"[A-Z]{3}", str(schema.get("currency", ""))):
        err(None, "Currency must be a 3-letter ISO code.")

    seen_ids, seen_native, seen_custom = set(), set(), set()
    for fld in fields:
        try:
            _check_field(fld, custom_defs, custom_enabled, seen_ids, seen_native, seen_custom, err)
        except (TypeError, AttributeError, KeyError, ValueError):
            fid = fld.get("id") if isinstance(fld, dict) and isinstance(fld.get("id"), str) else None
            err(fid, "Malformed field definition.")

    if "name" not in seen_native:
        err(None, "A field must map to the native destination name.")
    if custom_defs is not None:
        _check_required_custom(fields, custom_defs, seen_custom, err)
    return errors


def _check_field(fld, custom_defs, custom_enabled, seen_ids, seen_native, seen_custom, err):
    fid = fld.get("id")
    if not isinstance(fid, str) or not FIELD_ID_RE.fullmatch(fid):
        err(fid, "Field id must match [a-z][a-z0-9_]{0,63}.")
        return
    if fid in seen_ids:
        err(fid, "Duplicate field id.")
    seen_ids.add(fid)
    ftype = fld.get("type")
    if ftype not in INPUT_TYPES:
        err(fid, "Unsupported input type.")
        return
    if not str(fld.get("label", "")).strip() or len(fld["label"]) > 191:
        err(fid, "Label required, max 191 characters.")
    if len(str(fld.get("helpText", ""))) > 500:
        err(fid, "Help text max 500 characters.")
    if not isinstance(fld.get("required"), bool):
        err(fid, "required must be boolean.")
    _check_options(fld, err)
    _check_destination(fld, custom_defs, custom_enabled, seen_native, seen_custom, err)


def _check_options(fld, err):
    fid, ftype = fld["id"], fld["type"]
    opts = fld.get("options", [])
    if ftype not in ("select", "multiselect"):
        if opts:
            err(fid, "Options apply to select and multiselect only.")
        return
    if not opts or len(opts) > MAX_OPTIONS:
        err(fid, f"Provide 1-{MAX_OPTIONS} options.")
        return
    values = [o.get("value") if isinstance(o, dict) else None for o in opts]
    if any(not isinstance(v, str) or not v.strip() or len(v) > 191 for v in values) or len(set(values)) != len(values):
        err(fid, "Options need distinct non-empty string values.")
    if any(not str(o.get("label", "")).strip() for o in opts if isinstance(o, dict)):
        err(fid, "Options need labels.")


def _check_destination(fld, defs, custom_enabled, seen_native, seen_custom, err):
    fid, ftype = fld["id"], fld["type"]
    dest = fld.get("destination")
    if not isinstance(dest, dict):
        err(fid, "Destination required.")
        return
    kind = dest.get("kind")
    if kind == "native":
        name = dest.get("field")
        spec = NATIVE.get(name)
        if spec is None:
            err(fid, "Destination is not an allowlisted native field.")
            return
        if name in seen_native:
            err(fid, "Duplicate native destination.")
        seen_native.add(name)
        if ftype not in spec["inputs"]:
            err(fid, f"Destination {name} requires input type {'/'.join(sorted(spec['inputs']))}.")
        if spec.get("must_require") and not fld.get("required"):
            err(fid, f"{name} must be required.")
        if fld.get("maxLength") is not None and ftype in ("text", "textarea"):
            if not isinstance(fld["maxLength"], int) or not 1 <= fld["maxLength"] <= spec.get("max", 30000):
                err(fid, f"maxLength must be 1-{spec.get('max', 30000)}.")
        if spec.get("enum"):
            allowed = set(ENUMS[spec["enum"]])
            if any(o.get("value") not in allowed for o in fld.get("options", []) if isinstance(o, dict)):
                err(fid, "Option outside the destination enum.")
    elif kind == "custom":
        _check_custom(fld, dest, defs, custom_enabled, seen_custom, err)
    elif kind == "retained_only":
        reason = dest.get("reason")
        if not isinstance(reason, str) or not reason.strip() or len(reason) > 500:
            err(fid, "Retained-only fields need a reason (max 500 characters).")
        if ftype in ("text", "textarea") and not isinstance(fld.get("maxLength"), int):
            err(fid, "Retained-only text fields need maxLength.")
    else:
        err(fid, "Unknown destination kind.")


def _check_custom(fld, dest, defs, custom_enabled, seen_custom, err):
    fid = fld["id"]
    cid, ctype = dest.get("customFieldId"), dest.get("customType")
    if not custom_enabled:
        err(fid, "Custom fields are not enabled on the connection.")
    if not isinstance(cid, int) or isinstance(cid, bool) or cid <= 0:
        err(fid, "customFieldId must be a positive integer.")
        return
    if cid in seen_custom:
        err(fid, "Duplicate custom destination.")
    seen_custom.add(cid)
    if ctype not in CUSTOM_TYPES:
        err(fid, "Custom type unsupported. OPTIONS, OPTIONS_NUMERIC and CURRENCY mappings are disabled.")
        return
    if fld["type"] not in CUSTOM_INPUTS[ctype]:
        err(fid, f"Custom type {ctype} requires input type {'/'.join(sorted(CUSTOM_INPUTS[ctype]))}.")
    if defs is None:
        return
    d = defs.get(cid)
    if d is None or "VENDOR" not in d.get("entityTypes", []):
        err(fid, "Custom field not found on VENDOR in this tenant.")
    elif d.get("type") != ctype:
        err(fid, f"Tenant reports type {d.get('type')}, form says {ctype}.")
    elif d.get("readOnly"):
        err(fid, "Custom field is read-only.")
    elif d.get("isRequired") and not fld.get("required"):
        err(fid, "Tenant requires this custom field; mark it required.")


def _check_required_custom(fields, defs, mapped, err):
    for cid, d in defs.items():
        if "VENDOR" not in d.get("entityTypes", []) or not d.get("isRequired") or d.get("readOnly"):
            continue
        if d.get("type") not in CUSTOM_TYPES:
            err(None, f"Required custom field {cid} has unsupported type {d.get('type')}.")
        elif cid not in mapped:
            err(None, f"Required custom field {cid} is not mapped.")


def form_uses_custom(schema: dict) -> bool:
    return any(f["destination"]["kind"] == "custom" for f in schema["fields"])


def _text(value, fld, err, limit, multiline):
    if not isinstance(value, str):
        return err("Text expected.")
    text = unicodedata.normalize("NFC", value).strip()
    if not multiline:
        text = re.sub(r"[\r\n\t]+", " ", text)
    if _CTRL_RE.search(text):
        return err("Control characters are not allowed.")
    if len(text) > limit:
        return err(f"Maximum {limit} characters.")
    return text


def _url(value, limit, err):
    if not isinstance(value, str):
        return err("URL expected.")
    text = unicodedata.normalize("NFC", value).strip()
    if len(text) > limit:
        return err(f"Maximum {limit} characters.")
    if not text or re.search(r"[\x00-\x20\x7f-\x9f]", text):
        return err("URL must not contain spaces or control characters.")
    try:
        parts = urlsplit(text)
        parts.port
    except ValueError:
        return err("Invalid URL.")
    if parts.scheme not in ("http", "https") or not parts.hostname:
        return err("URL must be absolute http(s).")
    if parts.username or parts.password:
        return err("URL must not contain credentials.")
    return text


def _emails(value, err):
    if not isinstance(value, str):
        return err("Email list expected.")
    out = []
    for raw in re.split(r"[;,]", unicodedata.normalize("NFC", value)):
        addr = raw.strip()
        if not addr:
            continue
        if not _EMAIL_RE.match(addr):
            return err(f"Invalid email address: {addr[:80]}")
        local, domain = addr.rsplit("@", 1)
        addr = f"{local}@{domain.lower()}"
        if addr not in out:
            out.append(addr)
    joined = ";".join(out)
    if not 1 <= len(out) <= 5:
        return err("Provide 1-5 email addresses.")
    if len(joined) > 191:
        return err("Email list exceeds 191 characters.")
    return joined


def _decimal(value, pattern, err, message):
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        return err(message)
    text = str(value).strip()
    if not pattern.match(text):
        return err(message)
    return text


def validate_answers(schema: dict, answers) -> tuple[dict, dict]:
    clean, errors = {}, {}
    if not isinstance(answers, dict):
        return {}, {"_form": "answers must be an object."}
    try:
        json.dumps(answers, ensure_ascii=False).encode("utf-8")
    except (UnicodeEncodeError, TypeError, ValueError, RecursionError):
        return {}, {"_form": "answers contain unsupported characters."}
    known = {f["id"]: f for f in schema["fields"]}
    for unknown in set(answers) - set(known):
        errors["_unknown"] = f"Unknown field id: {str(unknown)[:64]}"
    for fid, fld in known.items():
        problem: list[str] = []

        def err(msg, _p=problem):
            _p.append(msg)

        value = _one(fld, answers.get(fid), err)
        if problem:
            errors[fid] = problem[0]
        elif value is not None:
            clean[fid] = value
        elif fld["required"]:
            errors[fid] = "This field is required."
    return clean, errors


def _blank(value) -> bool:
    return value is None or (isinstance(value, str) and not value.strip()) or (isinstance(value, list) and not value)


def _one(fld, value, err):
    if _blank(value):
        return None
    ftype, dest = fld["type"], fld["destination"]
    limit = fld.get("maxLength") or 30000
    if dest["kind"] == "custom":
        limit = min(limit, CUSTOM_MAX.get(dest["customType"], limit))
    if ftype in ("text", "textarea"):
        if dest["kind"] == "custom" and dest["customType"] == "NUMBER":
            text = _decimal(value, _NUMBER_RE, err, "Number with at most 6 decimals expected.")
            if text is not None and abs(Decimal(text)) > 1_000_000_000:
                return err("Number exceeds 1,000,000,000.")
            return text
        return _text(value, fld, err, limit, ftype == "textarea")
    if ftype == "url":
        return _url(value, min(limit, 768), err)
    if ftype == "email_list":
        return _emails(value, err)
    if ftype == "boolean":
        return value if isinstance(value, bool) else err("Yes or No expected.")
    if ftype == "money":
        text = _decimal(value, _MONEY_RE, err, "Amount with at most 2 decimals expected.")
        return text
    allowed = {o["value"] for o in fld["options"]}
    if ftype == "select":
        return value if isinstance(value, str) and value in allowed else err("Choose one of the listed options.")
    if not isinstance(value, list) or any(not isinstance(v, str) or v not in allowed for v in value):
        return err("Choose from the listed options.")
    return sorted(set(value), key=[o["value"] for o in fld["options"]].index)


# Decimal only. Drata cost is integer cents as a string.
def cost_cents(amount: str) -> str:
    return str((Decimal(amount) * 100).to_integral_exact())


# At most 15 significant digits below 1e9, so float repr round-trips.
def custom_number(text: str):
    d = Decimal(text)
    return int(d) if d == d.to_integral_value() else float(d)


def marker(installation: str, submission: str) -> str:
    return MARKER_TEMPLATE.format(installation=installation, submission=submission)


def build_create_payload(schema: dict, clean: dict, mark: str) -> dict:
    payload: dict = {"status": "PROSPECTIVE"}
    customs = []
    notes = None
    for fld in schema["fields"]:
        if fld["id"] not in clean:
            continue
        value, dest = clean[fld["id"]], fld["destination"]
        if dest["kind"] == "native":
            spec = NATIVE[dest["field"]]
            if dest["field"] == "cost":
                value = cost_cents(value)
            if dest["field"] == "notes":
                notes = value
            else:
                payload[spec["prop"]] = value
        elif dest["kind"] == "custom":
            if dest["customType"] == "NUMBER":
                value = custom_number(value)
            customs.append({"id": dest["customFieldId"], "value": value})
    payload["notes"] = f"{notes}\n\n{mark}" if notes else mark
    if customs:
        payload["customFields"] = customs
    return payload


def build_update_payload(schema: dict, clean: dict, selected: list[str]) -> dict:
    payload: dict = {}
    for fld in schema["fields"]:
        dest = fld["destination"]
        if dest["kind"] != "native" or dest["field"] not in selected or fld["id"] not in clean:
            continue
        name = dest["field"]
        if name not in UPDATABLE:
            continue
        spec = NATIVE[name]
        value = cost_cents(clean[fld["id"]]) if name == "cost" else clean[fld["id"]]
        payload[spec.get("update_prop", spec["prop"])] = value
    return payload


def updatable_fields(schema: dict, clean: dict) -> list[str]:
    return [f["destination"]["field"] for f in schema["fields"]
            if f["destination"]["kind"] == "native" and f["destination"]["field"] in UPDATABLE and f["id"] in clean]


def preview(schema: dict, sample: dict, installation: str = "00000000-0000-4000-8000-000000000000") -> dict:
    clean, errors = validate_answers(schema, sample)
    mark = marker(installation, "11111111-1111-4111-8111-111111111111")
    warnings = [f"{n} not mapped: Drata API defaults it to false."
                for n in ("hasPii", "isSubProcessor")
                if n not in {f["destination"].get("field") for f in schema["fields"]}]
    retained = [{"id": f["id"], "label": f["label"], "reason": f["destination"]["reason"]}
                for f in schema["fields"] if f["destination"]["kind"] == "retained_only"]
    return {"payload": None if errors else build_create_payload(schema, clean, mark),
            "fieldErrors": errors, "warnings": warnings, "retainedOnly": retained}


def prop_values(payload: dict, update: bool = False) -> dict:
    return {k: v for k, v in payload.items() if k not in ("customFields",)}


# Drata lowercases the whole contact address on write; compare case-insensitively.
def _emails_of(value):
    return sorted(e.strip().lower() for e in re.split(r"[;,]", value or "") if e.strip())


def same(prop: str, want, have) -> bool:
    if prop in ("contactEmail", "contactsEmail"):
        return _emails_of(want) == _emails_of(have)
    if prop == "dataAccessedOrProcessedList":
        return sorted(want or []) == sorted(have or [])
    if prop == "cost":
        return str(want) == str(have)
    if isinstance(want, str) and isinstance(have, str):
        return want.strip() == have.strip()
    return want == have


def vendor_prop(vendor: dict, prop: str):
    return vendor.get("contactsEmail") if prop == "contactEmail" else vendor.get(prop)


# Read-back covers only properties the bridge sent.
def mismatches(vendor: dict, expected: dict) -> list[str]:
    bad = [p for p, want in expected.items() if p != "customFields" and not same(p, want, vendor_prop(vendor, p))]
    have = {c.get("customFieldId"): c for c in vendor.get("customFields") or []}
    for item in expected.get("customFields", []):
        got = have.get(item["id"])
        if got is None or "value" not in got or not _custom_same(item["value"], got["value"]):
            bad.append(f"customFields.{item['id']}")
    return bad


def _custom_same(want, have) -> bool:
    if isinstance(want, (int, float)) and not isinstance(want, bool):
        try:
            return Decimal(str(want)) == Decimal(str(have))
        except InvalidOperation:
            return False
    return want == have


def normalized_name(name: str) -> str:
    return " ".join(unicodedata.normalize("NFC", name or "").casefold().split())


def normalized_host(url: str | None) -> str | None:
    if not url:
        return None
    text = url.strip()
    try:
        host = urlsplit(text if "//" in text else "//" + text).hostname
    except ValueError:
        return None
    return host.lower().rstrip(".") if host else None
