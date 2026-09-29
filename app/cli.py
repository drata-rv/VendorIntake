import os
import sqlite3
import sys
from pathlib import Path

import click
from cryptography.fernet import Fernet, InvalidToken

from . import auth, db, forms, submissions
from .crypto import decrypt, encrypt, load_fernet

REENCRYPT = {"submissions": ("answers_enc", "payload_enc", "snapshot_enc", "candidates_enc"), "settings": ("api_key_enc",)}


def _conn(app):
    return db.connect(app.config["DATABASE_PATH"])


def register_cli(app):
    @app.cli.command("init-db")
    def init_db():
        path = Path(app.config["DATABASE_PATH"])
        path.parent.mkdir(parents=True, exist_ok=True)
        conn = db.connect(str(path))
        try:
            first = not db.applied_versions(conn)
            db.migrate(conn)
            if first:
                conn.execute("INSERT INTO settings (id, installation_uuid, updated_at) VALUES (1, ?, ?)", (db.new_id(), db.iso()))
                forms.seed_starter(conn)
        finally:
            conn.close()
        os.chmod(path, 0o600)
        click.echo("initialized" if first else "already initialized")

    @app.cli.command("migrate")
    def migrate():
        conn = _conn(app)
        try:
            applied = db.migrate(conn)
        finally:
            conn.close()
        click.echo("applied: " + (", ".join(applied) or "none"))

    @app.cli.command("check-schema")
    def check_schema():
        conn = _conn(app)
        try:
            ok = db.schema_current(conn)
        finally:
            conn.close()
        if not ok:
            click.echo("schema out of date; run: flask --app app migrate", err=True)
            sys.exit(1)
        click.echo("schema current")

    @app.cli.command("create-admin")
    @click.option("--email", prompt=True)
    @click.password_option(confirmation_prompt=True)
    def create_admin(email, password):
        conn = _conn(app)
        try:
            auth.create_user(conn, email, "ADMIN", password, False, "SYSTEM")
        finally:
            conn.close()
        click.echo("administrator created")

    @app.cli.command("reset-password")
    @click.option("--email", prompt=True)
    @click.password_option(confirmation_prompt=True)
    def reset_password(email, password):
        auth.validate_password(password)
        conn = _conn(app)
        try:
            user = conn.execute("SELECT id FROM users WHERE email = ?", (auth.normalize_email(email),)).fetchone()
            if user is None:
                raise click.ClickException("no such user")
            with db.tx(conn):
                conn.execute("UPDATE users SET password_hash = ?, must_change_password = 0, updated_at = ? WHERE id = ?",
                             (auth.hash_password(password), db.iso(), user["id"]))
                auth.invalidate_sessions(conn, user["id"])
                conn.execute("DELETE FROM login_limits")
                db.audit(conn, "SYSTEM", "PASSWORD_CHANGED", user["id"])
        finally:
            conn.close()
        click.echo("password reset")

    @app.cli.command("backup")
    @click.option("--output", required=True, type=click.Path(dir_okay=False))
    def backup(output):
        if os.path.exists(output):
            raise click.ClickException("output exists")
        os.close(os.open(output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600))
        src = _conn(app)
        dest = sqlite3.connect(output)
        try:
            src.backup(dest)
        finally:
            dest.close()
            src.close()
        click.echo(f"backup written: {output}")

    @app.cli.command("restore-check")
    def restore_check():
        conn = _conn(app)
        try:
            flagged = submissions.restore_flag(conn)
        finally:
            conn.close()
        click.echo(f"flagged for reconciliation: {flagged}; writes disabled; sessions cleared")

    @app.cli.command("cleanup")
    def cleanup():
        conn = _conn(app)
        try:
            result = submissions.run_cleanup(conn, actor="SYSTEM")
        finally:
            conn.close()
        click.echo(str(result))

    @app.cli.command("rotate-key")
    @click.option("--new-key-file", required=True, type=click.Path(exists=True, dir_okay=False))
    def rotate_key(new_key_file):
        old = app.extensions["fernet"]
        new = load_fernet(new_key_file)
        if _same_key(old, new):
            raise click.ClickException("new key equals current key")
        conn = _conn(app)
        count = 0
        try:
            # One transaction: partial re-encryption would leave rows under two keys.
            with db.tx(conn):
                for table, columns in REENCRYPT.items():
                    for row in conn.execute(f"SELECT id, {', '.join(columns)} FROM {table}").fetchall():
                        for column in columns:
                            if row[column] is not None:
                                conn.execute(f"UPDATE {table} SET {column} = ? WHERE id = ?",
                                             (encrypt(decrypt(row[column], old), new), row["id"]))
                                count += 1
                db.audit(conn, "SYSTEM", "KEY_ROTATED", None, {"values": count})
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        finally:
            conn.close()
        click.echo(f"re-encrypted {count} values; install the new key file and restart")


def _same_key(old: Fernet, new: Fernet) -> bool:
    try:
        new.decrypt(old.encrypt(b"probe"))
    except InvalidToken:
        return False
    return True
