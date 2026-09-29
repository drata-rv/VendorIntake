from flask import Blueprint, current_app, g, jsonify, make_response, redirect, render_template, request, url_for

from . import auth, connection, forms, mappings, submissions
from .crypto import dec_json
from .db import audit, connect, get_db, iso, schema_current, settings_row, tx
from .errors import ApiFail

bp = Blueprint("routes", __name__)
NONCE_ACTIONS = {"RETRY", "CANCEL", "LINK_EXISTING", "CONFIRM_NEW", "RECREATE"}
STATES = ["IN_PROGRESS", "CREATED", "UPDATED", "NEEDS_REVIEW", "NEEDS_CORRECTION", "BLOCKED", "RETRYABLE", "UNKNOWN", "CANCELLED"]


def json_body() -> dict:
    body = request.get_json(silent=True)
    if not isinstance(body, dict):
        raise ApiFail(400, "INVALID_JSON", "Request body must be a JSON object.")
    return body


def out(payload, status: int = 200):
    resp = jsonify(payload)
    resp.status_code = status
    return resp


def uid() -> str:
    return g.user["id"]


@bp.before_request
def _admin_housekeeping():
    if g.get("user") and g.user["role"] == "ADMIN" and request.method == "GET" and request.endpoint != "static":
        submissions.maybe_cleanup(get_db())


@bp.get("/healthz")
def healthz():
    return out({"status": "ok"})


@bp.get("/readyz")
def readyz():
    try:
        conn = connect(current_app.config["DATABASE_PATH"])
        try:
            ready = schema_current(conn) and settings_row(conn) is not None and "fernet" in current_app.extensions
        finally:
            conn.close()
    except Exception:
        ready = False
    return out({"status": "ready" if ready else "not_ready"}, 200 if ready else 503)


@bp.get("/login")
def login_get():
    if g.user:
        return redirect(url_for("routes.index"))
    resp = make_response()
    nonce = auth.prelogin_issue(resp)
    resp.set_data(render_template("login.html", csrf_token=nonce, error=None))
    return resp


@bp.post("/login")
def login_post():
    if not auth.prelogin_valid():
        raise ApiFail(403, "CSRF_INVALID", "Login form expired. Reload and try again.")
    user = auth.authenticate(request.form.get("email"), request.form.get("password"), request.remote_addr)
    if user is None:
        resp = make_response()
        nonce = auth.prelogin_issue(resp)
        resp.set_data(render_template("login.html", csrf_token=nonce, error="Email or password is incorrect."))
        resp.status_code = 401
        return resp
    resp = redirect(url_for("routes.index"))
    auth.start_session(user["id"], resp)
    resp.delete_cookie(auth.PRELOGIN_COOKIE, path="/")
    return resp


@bp.post("/logout")
def logout():
    if g.user:
        conn = get_db()
        with tx(conn):
            conn.execute("DELETE FROM sessions WHERE token_hash = ?", (g.session["token_hash"],))
            audit(conn, uid(), "LOGOUT", uid())
    resp = redirect(url_for("routes.login_get"))
    resp.delete_cookie(auth.SESSION_COOKIE, path="/")
    return resp


@bp.get("/account/password")
@auth.login_required
def password_page():
    return render_template("password.html", forced=g.user["must_change_password"])


@bp.post("/api/account/password")
@auth.login_required
def change_password():
    body = json_body()
    if not auth.verify_current_password(uid(), body.get("currentPassword")):
        raise ApiFail(403, "CURRENT_PASSWORD_INVALID", "Current password is incorrect.", {"currentPassword": "Incorrect."})
    resp = out({"ok": True})
    auth.change_password(uid(), body.get("newPassword"), resp)
    return resp


@bp.post("/api/account/reauth")
@auth.login_required
def reauth():
    if not auth.verify_current_password(uid(), json_body().get("password")):
        raise ApiFail(403, "CURRENT_PASSWORD_INVALID", "Password is incorrect.", {"password": "Incorrect."})
    token, csrf = auth.rotate_session(uid(), g.session["token_hash"])
    resp = out({"ok": True, "csrfToken": csrf})
    auth.set_session_cookie(resp, token)
    return resp


@bp.get("/")
@auth.login_required
def index():
    if g.user["role"] == "ADMIN" and settings_row(get_db())["connection_state"] != "ACTIVE":
        return redirect(url_for("routes.connection_page"))
    return redirect(url_for("routes.intake"))


@bp.get("/intake")
@auth.login_required
def intake():
    conn = get_db()
    active = forms.active(conn)
    schema = forms.get_version(conn, active["version"]) if active["version"] and active["enabled"] else None
    prefill = None
    origin = request.args.get("correct")
    if schema and origin:
        row = conn.execute(
            "SELECT answers_enc FROM submissions WHERE id = ? AND requester_id = ? AND state = 'NEEDS_CORRECTION'"
            " AND form_version = ? AND answers_enc IS NOT NULL", (origin, uid(), active["version"])).fetchone()
        if row:
            prefill = {"originSubmissionId": origin, "answers": dec_json(row["answers_enc"])}
    return render_template("intake.html", form=schema, form_version=active["version"], prefill=prefill)


@bp.get("/history")
@auth.login_required
def history_page():
    state = request.args.get("state") if request.args.get("state") in STATES else None
    return render_template("history.html", rows=submissions.history(g.user, state), states=STATES, filter_state=state)


@bp.get("/result/<sid>")
@auth.login_required
def result_page(sid):
    return render_template("result.html", sub=submissions.detail(g.user, sid))


@bp.post("/api/submissions")
@auth.login_required
def create_submission():
    status, body = submissions.submit(g.user, json_body(), request.headers.get("Idempotency-Key"))
    g.log_fields["submissionId"] = body.get("submissionId")
    return out(body, status)


@bp.get("/api/submissions/<sid>")
@auth.login_required
def get_submission(sid):
    return out(submissions.detail(g.user, sid))


@bp.get("/admin/connection")
@auth.admin_required
def connection_page():
    row = settings_row(get_db())
    return render_template("connection.html", conn=connection.view(row), drata_base_url=current_app.config["DRATA_BASE_URL"],
                           link_hosts=list(current_app.config["DRATA_LINK_HOSTS"]))


@bp.get("/api/admin/connection")
@auth.admin_required
def connection_get():
    return out(connection.view(settings_row(get_db())))


@bp.post("/api/admin/connection/test")
@auth.admin_required
def connection_test():
    return out(connection.test_credential(json_body()))


@bp.post("/api/admin/connection")
@auth.admin_required
def connection_save():
    return out(connection.save_connection(uid(), json_body()))


@bp.post("/api/admin/connection/disconnect")
@auth.admin_required
def connection_disconnect():
    return out(connection.disconnect(uid()))


@bp.post("/api/admin/retention")
@auth.admin_required
def retention():
    return out(connection.update_retention(uid(), json_body()))


@bp.get("/admin/form")
@auth.admin_required
def form_page():
    conn = get_db()
    row = settings_row(conn)
    try:
        defs = forms.definitions_for_editor(row)
    except ApiFail:
        defs = None
    version = forms.latest_version(conn)
    catalog = [{"name": n, "inputs": sorted(s["inputs"]), "enum": s.get("enum"), "createOnly": bool(s.get("create_only"))}
               for n, s in mappings.NATIVE.items()]
    return render_template("form_editor.html", schema=forms.get_version(conn, version), version=version,
                           active=forms.active(conn), native=catalog, enums=mappings.ENUMS, custom_defs=defs,
                           custom_enabled=bool(row["custom_fields_enabled"]), input_types=sorted(mappings.INPUT_TYPES),
                           custom_types=sorted(mappings.CUSTOM_TYPES))


@bp.post("/api/admin/form")
@auth.admin_required
def form_save():
    return out(forms.save_version(get_db(), uid(), json_body().get("schema")), 201)


@bp.post("/api/admin/form/preview")
@auth.admin_required
def form_preview():
    body = json_body()
    schema = body.get("schema")
    if not forms.shape_ok(schema):
        raise ApiFail(422, "FORM_INVALID", "Form schema malformed.")
    conn = get_db()
    row = settings_row(conn)
    schema = mappings.normalize_form(schema)
    errors = mappings.validate_form(schema, None, bool(row["custom_fields_enabled"]))
    result = None
    if not errors:
        result = mappings.preview(schema, body.get("answers") or {}, row["installation_uuid"])
    return out({"banner": "Preview only — nothing sent to Drata.", "schemaErrors": errors, "preview": result})


@bp.post("/api/admin/form/publish")
@auth.admin_required
def form_publish():
    version = json_body().get("version")
    if not isinstance(version, int) or isinstance(version, bool):
        raise ApiFail(422, "INVALID_INPUT", "version required.")
    return out(forms.publish(get_db(), uid(), version))


@bp.post("/api/admin/form/disable")
@auth.admin_required
def form_disable():
    return out(forms.disable(get_db(), uid()))


@bp.get("/submissions/<sid>")
@auth.admin_required
def submission_page(sid):
    return render_template("submission_detail.html", d=submissions.detail(g.user, sid))


@bp.post("/api/admin/submissions/<sid>/action-nonce")
@auth.admin_required
def action_nonce(sid):
    body = json_body()
    action = body.get("action")
    if action not in NONCE_ACTIONS:
        raise ApiFail(422, "INVALID_INPUT", "Unsupported action.")
    extra = body.get("targetDrataId") if action == "LINK_EXISTING" else None
    return out(submissions.issue_nonce(get_db(), sid, action, extra))


@bp.post("/api/admin/submissions/<sid>/retry")
@auth.admin_required
def submission_retry(sid):
    status, body = submissions.retry(uid(), sid, json_body().get("nonce"))
    return out(body, status)


@bp.post("/api/admin/submissions/<sid>/reconcile")
@auth.admin_required
def submission_reconcile(sid):
    status, body = submissions.reconcile(uid(), sid)
    return out(body, status)


@bp.post("/api/admin/submissions/<sid>/resolve")
@auth.admin_required
def submission_resolve(sid):
    status, body = submissions.resolve(uid(), sid, json_body())
    return out(body, status)


@bp.post("/api/admin/submissions/<sid>/update-preview")
@auth.admin_required
def submission_update_preview(sid):
    status, body = submissions.update_preview(uid(), sid, json_body())
    return out(body, status)


@bp.post("/api/admin/submissions/<sid>/update-confirm")
@auth.admin_required
def submission_update_confirm(sid):
    status, body = submissions.update_confirm(uid(), sid, json_body().get("nonce"))
    return out(body, status)


@bp.post("/api/admin/submissions/<sid>/export")
@auth.admin_required
def submission_export(sid):
    resp = out(submissions.export(uid(), sid))
    resp.headers["Content-Disposition"] = f'attachment; filename="submission-{sid}.json"'
    return resp


@bp.get("/admin/users")
@auth.admin_required
def users_page():
    rows = get_db().execute(
        "SELECT id, email, role, active, must_change_password, created_at, last_login_at FROM users ORDER BY email").fetchall()
    return render_template("users.html", users=[dict(r) for r in rows])


@bp.post("/api/admin/users")
@auth.admin_required
def users_create():
    body = json_body()
    role = body.get("role", "REQUESTER")
    if role not in ("ADMIN", "REQUESTER"):
        raise ApiFail(422, "INVALID_INPUT", "role must be ADMIN or REQUESTER.")
    password = auth.temp_password()
    user_id = auth.create_user(get_db(), body.get("email"), role, password, True, uid())
    return out({"id": user_id, "email": auth.normalize_email(body.get("email")), "role": role, "temporaryPassword": password}, 201)


@bp.post("/api/admin/users/<user_id>")
@auth.admin_required
def users_update(user_id):
    body = json_body()
    conn = get_db()
    target = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
    if target is None:
        raise ApiFail(404, "NOT_FOUND", "User not found.")
    active = body.get("active", bool(target["active"]))
    role = body.get("role", target["role"])
    if role not in ("ADMIN", "REQUESTER") or not isinstance(active, bool):
        raise ApiFail(422, "INVALID_INPUT", "active must be boolean and role ADMIN or REQUESTER.")
    lost_admin = target["role"] == "ADMIN" and target["active"] and (not active or role != "ADMIN")
    if lost_admin and (user_id == uid() or conn.execute(
            "SELECT COUNT(*) AS n FROM users WHERE role = 'ADMIN' AND active = 1 AND id != ?", (user_id,)).fetchone()["n"] == 0):
        raise ApiFail(409, "LAST_ADMIN", "Cannot remove the last active administrator or your own access.")
    temp = auth.temp_password() if body.get("resetPassword") is True else None
    with tx(conn):
        conn.execute("UPDATE users SET active = ?, role = ?, updated_at = ? WHERE id = ?", (int(active), role, iso(), user_id))
        if temp:
            conn.execute("UPDATE users SET password_hash = ?, must_change_password = 1 WHERE id = ?", (auth.hash_password(temp), user_id))
        if not active or role != target["role"] or temp:
            auth.invalidate_sessions(conn, user_id)
        audit(conn, uid(), "USER_UPDATED", user_id, {"active": active, "role": role, "passwordReset": bool(temp)})
    return out({"id": user_id, "active": active, "role": role, "temporaryPassword": temp})
