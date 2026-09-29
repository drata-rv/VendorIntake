import json
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

from flask import current_app, g

MIGRATIONS_DIR = Path(__file__).resolve().parent.parent / "migrations"


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime | None = None) -> str:
    return (dt or utcnow()).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def parse_iso(value: str) -> datetime:
    return datetime.strptime(value, "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=timezone.utc)


def in_seconds(seconds: float) -> str:
    return iso(utcnow() + timedelta(seconds=seconds))


def new_id() -> str:
    return str(uuid.uuid4())


def connect(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(path, timeout=5, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA busy_timeout = 5000")
    conn.execute("PRAGMA synchronous = FULL")
    return conn


def get_db() -> sqlite3.Connection:
    if "db" not in g:
        g.db = connect(current_app.config["DATABASE_PATH"])
    return g.db


def close_db(_exc=None) -> None:
    conn = g.pop("db", None)
    if conn is not None:
        conn.close()


@contextmanager
def tx(conn: sqlite3.Connection):
    # Nested use joins the outer transaction.
    if conn.in_transaction:
        yield conn
        return
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    else:
        conn.execute("COMMIT")


def audit(conn, actor: str, action: str, subject: str | None = None, meta: dict | None = None) -> None:
    conn.execute(
        "INSERT INTO audit_events (actor, action, subject, at, meta) VALUES (?, ?, ?, ?, ?)",
        (actor, action, subject, iso(), json.dumps(meta or {}, sort_keys=True)),
    )


def migration_files() -> list[Path]:
    return sorted(MIGRATIONS_DIR.glob("*.sql"))


def latest_version() -> str:
    return migration_files()[-1].stem


def applied_versions(conn) -> set[str]:
    try:
        return {r["version"] for r in conn.execute("SELECT version FROM schema_migrations")}
    except sqlite3.OperationalError:
        return set()


def migrate(conn) -> list[str]:
    done = applied_versions(conn)
    applied = []
    for path in migration_files():
        if path.stem in done:
            continue
        conn.executescript("BEGIN IMMEDIATE;\n" + path.read_text() + "\nCOMMIT;")
        conn.execute("INSERT INTO schema_migrations (version, applied_at) VALUES (?, ?)", (path.stem, iso()))
        applied.append(path.stem)
    return applied


def schema_current(conn) -> bool:
    return latest_version() in applied_versions(conn)


def settings_row(conn) -> sqlite3.Row:
    return conn.execute("SELECT * FROM settings WHERE id = 1").fetchone()
