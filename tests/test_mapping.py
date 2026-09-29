import json

from app import mappings

INSTALLATION = "0a1b2c3d-0000-4000-8000-000000000001"
SUBMISSION = "0a1b2c3d-0000-4000-8000-000000000002"
MARK = mappings.marker(INSTALLATION, SUBMISSION)
BASE = {"vendor_name": "Acme", "vendor_website": "https://acme.example", "services_provided": "Hosting",
        "stores_pii": False, "is_subprocessor": False}
RETAINED = {"kind": "retained_only", "reason": "kept locally"}
SIX_EMAILS = ";".join(f"u{i}@x.io" for i in range(6))


def with_fields(*extra):
    form = mappings.starter_form()
    form["fields"] += list(extra)
    return mappings.normalize_form(form)


def field(fid, dest, ftype="text", required=False, **kw):
    return {"id": fid, "label": fid, "type": ftype, "required": required, "destination": dest, **kw}


def native(name):
    return {"kind": "native", "field": name}


def custom(cid, ctype):
    return {"kind": "custom", "customFieldId": cid, "customType": ctype}


STARTER = with_fields()
COST = with_fields(field("annual_cost", native("cost"), "money"))
CUSTOM = with_fields(field("risk_score", custom(42, "NUMBER")), field("team_note", custom(43, "TEXT")))


def build(body, schema=STARTER):
    clean, errors = mappings.validate_answers(schema, body)
    assert not errors, errors
    return mappings.build_create_payload(schema, clean, MARK), clean


def assert_rejected(schema, cases):
    for fid, value in cases:
        clean, errors = mappings.validate_answers(schema, {**BASE, fid: value})
        assert fid in errors and fid not in clean, (fid, str(value)[:30], errors)


def test_email_property_is_contactEmail_on_create_and_contactsEmail_on_update():
    payload, clean = build({**BASE, "contact_email": "a@x.io"})
    assert payload["contactEmail"] == "a@x.io" and "contactsEmail" not in payload
    assert mappings.build_update_payload(STARTER, clean, ["vendor_contact_emails"]) == {"contactsEmail": "a@x.io"}


def test_explicit_false_kept_and_unanswered_boolean_rejected():
    payload, _ = build(BASE)
    assert payload["hasPii"] is False and payload["isSubProcessor"] is False
    assert "isSubProcessorActive" not in payload
    assert_rejected(STARTER, [("stores_pii", v) for v in (None, "", "no", 0, "false")])


def test_blank_optional_fields_are_omitted_not_nulled():
    for blank in (None, "", "   ", []):
        optional = {f["id"]: blank for f in STARTER["fields"] if not f["required"]}
        payload, _ = build({**BASE, **optional})
        assert set(payload) == {"status", "name", "url", "servicesProvided", "hasPii", "isSubProcessor", "notes"}, blank
        assert payload["notes"] == MARK


def test_cost_is_exact_integer_cents_and_bad_amounts_rejected():
    for raw, cents in (("12000.50", "1200050"), ("0", "0"), ("7", "700"), ("1.5", "150"), ("999999999.99", "99999999999")):
        payload, clean = build({**BASE, "annual_cost": raw}, COST)
        assert payload["cost"] == cents
        assert mappings.build_update_payload(COST, clean, ["cost"]) == {"cost": cents}
    assert_rejected(COST, [("annual_cost", v) for v in ("1.234", "-1", "1e3", "1000000000", "12,5", "abc", True)])


def test_out_of_range_values_are_rejected_never_truncated():
    assert_rejected(STARTER, [
        ("vendor_name", "n" * 192),
        ("vendor_website", "https://x.io/" + "a" * 756),
        ("privacy_url", "https://x.io/" + "a" * 756),
        ("services_provided", "s" * 30001),
        ("contact_name", "c" * 192),
        ("contact_email", ";".join(f"{'l' * 50}{i}@example.com" for i in range(4))),
        ("category", "BOGUS"),
        ("vendor_type", "vendor"),
        ("intake_notes", "n" * (30000 - mappings.MARKER_RESERVE + 1)),
    ])
    exact = {"vendor_name": "n" * 191, "vendor_website": "https://x.io/" + "a" * 755, "category": "SECURITY"}
    assert build({**BASE, **exact})[1] == {**BASE, **exact}


def test_status_always_prospective_and_never_in_update_payload():
    payload, clean = build({**BASE, "intake_notes": "pilot", "contact_email": "a@x.io"})
    assert payload["status"] == "PROSPECTIVE"
    selected = mappings.updatable_fields(STARTER, clean) + ["status", "notes", "confirmed"]
    update = mappings.build_update_payload(STARTER, clean, selected)
    assert update and not {"status", "notes", "confirmed", "customFields"} & set(update)
    assert mappings.build_update_payload(STARTER, clean, ["name"]) == {"name": "Acme"}


def test_marker_appended_to_notes_reserve_enforced_and_preview_uses_same_mapping():
    assert build({**BASE, "intake_notes": "pilot"})[0]["notes"] == f"pilot\n\n{MARK}"
    assert build(BASE)[0]["notes"] == MARK
    payload, _ = build({**BASE, "intake_notes": "n" * (30000 - mappings.MARKER_RESERVE)})
    assert len(payload["notes"]) == 30000 and payload["notes"].endswith(MARK)
    result = mappings.preview(STARTER, {**BASE, "intake_notes": "pilot"})
    assert result["fieldErrors"] == {} and result["payload"]["status"] == "PROSPECTIVE"
    assert result["payload"]["notes"].startswith("pilot\n\n") and mappings.MARKER_RE.search(result["payload"]["notes"])


def test_unknown_or_malformed_answers_rejected():
    _, errors = mappings.validate_answers(STARTER, {**BASE, "status": "ACTIVE", "accountId": 1})
    assert "_unknown" in errors
    assert "_form" in mappings.validate_answers(STARTER, ["not", "an", "object"])[1]


def test_email_list_split_dedupe_domain_lowercase_and_bounds():
    for raw, expected in (("Sec.Ops@Vendor.EXAMPLE", "Sec.Ops@vendor.example"),
                          ("a@x.io, b@x.io;a@x.io , ,c@x.io", "a@x.io;b@x.io;c@x.io"),
                          ("A@x.io;a@X.io", "A@x.io;a@x.io")):
        assert build({**BASE, "contact_email": raw})[0]["contactEmail"] == expected
    assert_rejected(STARTER, [("contact_email", v) for v in ("not-an-email", "a@b", "a@@x.io", "a b@x.io", ";", SIX_EMAILS)])


def test_url_rules_scheme_credentials_and_control_characters():
    for good in ("https://Vendor.Example/path?q=1#f", "http://x.io", "https://x.io:8443/"):
        assert build({**BASE, "vendor_website": good})[1]["vendor_website"] == good
    assert_rejected(STARTER, [("vendor_website", v) for v in (
        "ftp://x.io", "javascript:alert(1)", "//x.io/a", "x.io", "https://", "https://u:p@x.io", "https://user@x.io/",
        "https://x.io/a b", "https://x.io/\x00", "https://x.io/a\tb", "https://x.io:99999")])


def test_custom_number_serialized_as_plain_json_number_within_limits():
    for raw, text in (("12.5", "12.5"), ("42", "42"), ("-3.000001", "-3.000001"), ("0.10", "0.1"),
                      ("1000000000", "1000000000")):
        payload, _ = build({**BASE, "risk_score": raw, "team_note": "t" * 190}, CUSTOM)
        values = {c["id"]: c["value"] for c in payload["customFields"]}
        assert json.dumps(values[42]) == text and isinstance(values[43], str), raw
    assert_rejected(CUSTOM, [("risk_score", v) for v in (
        "1000000000.000001", "1.1234567", "12345678901", "1e3", "NaN", "Infinity")] + [("team_note", "t" * 191)])


def hostile(*extra, drop=(), relax=()):
    fields = [{**f, "required": False} if f["id"] in relax else f
              for f in mappings.starter_form()["fields"] if f["id"] not in drop]
    return mappings.normalize_form({"currency": "USD", "fields": fields + list(extra)})


def test_validate_form_rejects_bad_mappings_and_accepts_starter():
    tenant_required = {"entityTypes": ["VENDOR"], "isRequired": True}
    cases = [
        ("duplicate native", hostile(field("site2", native("url"), "url")), None, True, "Duplicate native"),
        ("missing name", hostile(drop=("vendor_name",)), None, True, "native destination name"),
        ("retained blank reason", hostile(field("why", {**RETAINED, "reason": "  "}, maxLength=10)), None, True, "reason"),
        ("retained no reason", hostile(field("why", {"kind": "retained_only"}, maxLength=10)), None, True, "reason"),
        ("retained null reason", hostile(field("why", {**RETAINED, "reason": None}, maxLength=10)), None, True, "reason"),
        ("custom OPTIONS", hostile(field("pick", custom(7, "OPTIONS"))), None, True, "unsupported"),
        ("custom CURRENCY", hostile(field("pick", custom(7, "CURRENCY"))), None, True, "unsupported"),
        ("custom disabled", hostile(field("note", custom(7, "TEXT"))), None, False, "not enabled"),
        ("protected status", hostile(field("st", native("status"))), None, True, "allowlisted"),
        ("protected confirmed", hostile(field("cf", native("confirmed"), "boolean")), None, True, "allowlisted"),
        ("pii optional", hostile(relax=("stores_pii",)), None, True, "must be required"),
        ("no destination", hostile(field("orphan", None)), None, True, "Destination required"),
        ("required tenant field unmapped", hostile(), {9: {**tenant_required, "type": "TEXT"}}, True, "not mapped"),
        ("required tenant field unsupported", hostile(), {9: {**tenant_required, "type": "OPTIONS"}}, True, "unsupported type"),
    ]
    for name, schema, defs, enabled, expected in cases:
        errors = mappings.validate_form(schema, defs, enabled)
        assert any(expected in e["message"] for e in errors), (name, errors)
    assert mappings.validate_form(STARTER, None, False) == []
