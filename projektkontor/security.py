from __future__ import annotations

import base64
import hashlib
import hmac
import os
import secrets
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken


def load_or_create_secret(path: Path) -> bytes:
    env_secret = os.getenv("PK_SECRET_KEY")
    if env_secret:
        raw = env_secret.encode("utf-8")
        return hashlib.sha256(raw).digest()
    if path.exists():
        return base64.urlsafe_b64decode(path.read_bytes())
    secret = secrets.token_bytes(32)
    path.write_bytes(base64.urlsafe_b64encode(secret))
    try:
        path.chmod(0o600)
    except OSError:
        pass
    return secret


SCRYPT_N = 2**14
SCRYPT_R = 8
SCRYPT_P = 5


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=SCRYPT_N, r=SCRYPT_R, p=SCRYPT_P)
    return "$".join((
        "scrypt", str(SCRYPT_N), str(SCRYPT_R), str(SCRYPT_P),
        base64.urlsafe_b64encode(salt).decode(), base64.urlsafe_b64encode(digest).decode(),
    ))


def verify_password(password: str, encoded: str) -> bool:
    try:
        parts = encoded.split("$")
        if len(parts) == 3:
            algorithm, salt64, digest64 = parts
            n, r, p = 2**14, 8, 1
        elif len(parts) == 6:
            algorithm, n_text, r_text, p_text, salt64, digest64 = parts
            n, r, p = int(n_text), int(r_text), int(p_text)
        else:
            return False
        if algorithm != "scrypt":
            return False
        if n < 2**12 or n > 2**18 or r < 1 or r > 16 or p < 1 or p > 10:
            return False
        salt = base64.urlsafe_b64decode(salt64)
        expected = base64.urlsafe_b64decode(digest64)
        actual = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=n, r=r, p=p)
        return hmac.compare_digest(actual, expected)
    except (ValueError, TypeError, OverflowError):
        return False


def password_needs_rehash(encoded: str) -> bool:
    try:
        parts = encoded.split("$")
        return len(parts) != 6 or tuple(map(int, parts[1:4])) != (SCRYPT_N, SCRYPT_R, SCRYPT_P)
    except (ValueError, TypeError):
        return True


class CodeVault:
    def __init__(self, secret: bytes):
        key = base64.urlsafe_b64encode(hashlib.sha256(secret + b"access-codes").digest())
        self._fernet = Fernet(key)

    def encrypt(self, code: str) -> str:
        return self._fernet.encrypt(code.encode("utf-8")).decode("ascii")

    def decrypt(self, token: str) -> str:
        try:
            return self._fernet.decrypt(token.encode("ascii")).decode("utf-8")
        except (InvalidToken, ValueError) as exc:
            raise ValueError("Zugangscode konnte nicht entschlüsselt werden") from exc


def generate_access_code(length: int = 8) -> str:
    alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
    return "".join(secrets.choice(alphabet) for _ in range(length))


def new_session_token() -> str:
    return secrets.token_urlsafe(32)


def hash_session_token(token: str, secret: bytes) -> str:
    return hmac.new(secret, token.encode("utf-8"), hashlib.sha256).hexdigest()
