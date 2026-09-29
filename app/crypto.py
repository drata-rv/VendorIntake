import json
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken
from flask import current_app


class DecryptError(Exception):
    pass


def load_fernet(key_path: str) -> Fernet:
    raw = Path(key_path).read_bytes().strip()
    return Fernet(raw)


def _fernet() -> Fernet:
    return current_app.extensions["fernet"]


def encrypt(text: str, fernet: Fernet | None = None) -> str:
    return (fernet or _fernet()).encrypt(text.encode()).decode()


def decrypt(token: str, fernet: Fernet | None = None) -> str:
    try:
        return (fernet or _fernet()).decrypt(token.encode()).decode()
    except InvalidToken as exc:
        raise DecryptError("decryption failed") from exc


def enc_json(obj, fernet: Fernet | None = None) -> str:
    return encrypt(json.dumps(obj, sort_keys=True, separators=(",", ":")), fernet)


def dec_json(token: str | None, fernet: Fernet | None = None):
    return None if token is None else json.loads(decrypt(token, fernet))
