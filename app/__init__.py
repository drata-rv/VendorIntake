import json
import logging
import os
import sys
import threading
import time
import traceback
import uuid
from urllib.parse import urlsplit

from flask import Flask, current_app, g, jsonify, make_response, render_template, request
from werkzeug.exceptions import HTTPException
from werkzeug.middleware.proxy_fix import ProxyFix

from . import auth, db
from .crypto import DecryptError, load_fernet
from .drata import DEFAULT_BASE_URL, DrataClient, Gate
from .errors import ApiFail

LOOPBACK = {"localhost", "127.0.0.1", "::1"}
NO_SESSION_ENDPOINTS = {"static", "routes.healthz", "routes.readyz"}
CSP = ("default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self'; connect-src 'self'; "
       "frame-ancestors 'none'; base-uri 'none'; form-action 'self'")


class JsonFormatter(logging.Formatter):
    def format(self, record):
        data = {"ts": db.iso(), "level": record.levelname, "msg": record.getMessage()}
        data.update(getattr(record, "fields", {}))
        return json.dumps(data, sort_keys=True)


def load_config(env, overrides=None) -> dict:
    cfg = {
        "APP_BASE_URL": env.get("APP_BASE_URL", ""),
        "DATABASE_PATH": env.get("DATABASE_PATH", "/data/bridge.sqlite3"),
        "ENCRYPTION_KEY_FILE": env.get("ENCRYPTION_KEY_FILE", "/run/secrets/bridge.key"),
        "DRATA_BASE_URL": env.get("DRATA_BASE_URL", DEFAULT_BASE_URL),
        "TRUST_PROXY_HOPS": int(env.get("TRUST_PROXY_HOPS", "1")),
        "MAX_CONTENT_LENGTH": int(env.get("MAX_CONTENT_LENGTH", "131072")),
        "DRATA_LINK_HOSTS": tuple(h.strip().lower() for h in env.get("DRATA_LINK_HOSTS", "").split(",") if h.strip()),
        "DRATA_MIN_INTERVAL": float(env.get("DRATA_MIN_INTERVAL", "1.0")),
    }
    cfg.update(overrides or {})
    base = urlsplit(cfg["APP_BASE_URL"])
    if base.scheme not in ("https", "http") or not base.hostname or (base.scheme == "http" and base.hostname not in LOOPBACK):
        raise RuntimeError("APP_BASE_URL must be an https URL (http allowed for loopback only).")
    if base.path not in ("", "/") or base.query or base.fragment:
        raise RuntimeError("APP_BASE_URL must not include a path.")
    if urlsplit(cfg["DRATA_BASE_URL"]).scheme != "https":
        raise RuntimeError("DRATA_BASE_URL must be https.")
    cfg["APP_ORIGIN"] = f"{base.scheme}://{base.netloc}"
    return cfg


def create_app(overrides: dict | None = None) -> Flask:
    app = Flask(__name__)
    app.config.update(load_config(os.environ, overrides))
    app.extensions["fernet"] = load_fernet(app.config["ENCRYPTION_KEY_FILE"])
    app.extensions["write_lock"] = threading.Lock()
    app.extensions["gate"] = Gate(app.config["DRATA_MIN_INTERVAL"])
    app.extensions["drata_factory"] = lambda api_key, budget=None: DrataClient(
        app.config["DRATA_BASE_URL"], api_key, budget, app.extensions["gate"])
    if app.config["TRUST_PROXY_HOPS"] > 0:
        n = app.config["TRUST_PROXY_HOPS"]
        app.wsgi_app = ProxyFix(app.wsgi_app, x_for=n, x_proto=n, x_host=n)
    _configure_logging(app)
    app.teardown_appcontext(db.close_db)
    app.before_request(_before)
    app.after_request(_after)
    _register_errors(app)

    from .routes import bp
    from .cli import register_cli
    app.register_blueprint(bp)
    register_cli(app)

    app.context_processor(_template_context)
    if not os.environ.get("FLASK_RUN_FROM_CLI") and not app.config.get("SKIP_STARTUP"):
        _startup(app)
    return app


def _configure_logging(app):
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    app.logger.handlers[:] = [handler]
    app.logger.setLevel(logging.INFO)
    app.logger.propagate = False


def _startup(app):
    from . import submissions
    path = app.config["DATABASE_PATH"]
    if not os.path.exists(path):
        raise RuntimeError("Database missing. Run: flask --app app init-db")
    conn = db.connect(path)
    try:
        if not db.schema_current(conn):
            raise RuntimeError("Schema out of date. Run: flask --app app migrate")
        row = db.settings_row(conn)
        if row is None:
            raise RuntimeError("Settings row missing. Run: flask --app app init-db")
        if row["api_key_enc"]:
            try:
                from .crypto import decrypt
                decrypt(row["api_key_enc"], app.extensions["fernet"])
            except DecryptError as exc:
                raise RuntimeError("Encryption key cannot decrypt stored settings.") from exc
        submissions.recover_startup(conn)
        submissions.run_cleanup(conn, actor="SYSTEM")
    finally:
        conn.close()


def _before():
    g.request_id = str(uuid.uuid4())
    g.started = time.monotonic()
    g.log_fields = {}
    g.user = g.session = None
    if request.endpoint in NO_SESSION_ENDPOINTS:
        return
    auth.load_session()
    auth.check_request_origin()
    if request.method in auth.UNSAFE and request.path.startswith("/api/") and request.mimetype != "application/json":
        raise ApiFail(415, "UNSUPPORTED_MEDIA_TYPE", "Content-Type must be application/json.")


def _after(response):
    response.headers["X-Request-ID"] = g.get("request_id", "")
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "no-referrer"
    if request.endpoint != "static":
        response.headers["Content-Security-Policy"] = CSP
        response.headers["Cache-Control"] = "no-store"
    fields = {"requestId": g.get("request_id"), "route": request.url_rule.rule if request.url_rule else "-",
              "method": request.method, "status": response.status_code,
              "ms": round((time.monotonic() - g.get("started", time.monotonic())) * 1000)}
    fields.update(g.get("log_fields", {}))
    if request.endpoint != "static":
        current_app.logger.info("request", extra={"fields": fields})
    return response


def _fail_response(status, code, message, body=None, headers=None):
    if auth.wants_json():
        payload = body or {"error": {"code": code, "message": message, "fieldErrors": {}, "submissionId": None}}
        resp = jsonify(payload)
        resp.status_code = status
    else:
        resp = app_error_page(status, code, message)
    for key, value in (headers or {}).items():
        resp.headers[key] = value
    return resp


def app_error_page(status, code, message):
    return make_response(render_template("error.html", status=status, code=code, message=message), status)


def _register_errors(app):
    @app.errorhandler(ApiFail)
    def api_fail(exc):
        g.log_fields["code"] = exc.code
        if exc.submission_id:
            g.log_fields["submissionId"] = exc.submission_id
        return _fail_response(exc.status, exc.code, exc.message, exc.body(), exc.headers)

    @app.errorhandler(HTTPException)
    def http_error(exc):
        code = {413: "PAYLOAD_TOO_LARGE", 404: "NOT_FOUND", 405: "METHOD_NOT_ALLOWED"}.get(exc.code, "HTTP_ERROR")
        g.log_fields["code"] = code
        return _fail_response(exc.code, code, exc.name)

    @app.errorhandler(Exception)
    def unhandled(exc):
        frame = traceback.extract_tb(exc.__traceback__)[-1] if exc.__traceback__ else None
        where = f"{os.path.basename(frame.filename)}:{frame.lineno}:{frame.name}" if frame else "-"
        g.log_fields.update({"code": "INTERNAL_ERROR", "errorType": exc.__class__.__name__, "where": where})
        return _fail_response(500, "INTERNAL_ERROR", "Unexpected error. The request id is in the response header.")


def _template_context():
    ctx = {"current_user": g.get("user"), "csrf_token": (g.get("user") or {}).get("csrf", ""),
           "tenant": None, "nav": request.endpoint}
    if g.get("user") is not None:
        row = db.settings_row(db.get_db())
        if row and row["account_id"]:
            ctx["tenant"] = {"name": row["account_name"], "domain": row["account_domain"]}
    return ctx
