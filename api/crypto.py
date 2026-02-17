# api/crypto.py
import base64
from django.conf import settings
from cryptography.fernet import Fernet


def _fernet() -> Fernet:
    key = getattr(settings, "TOKEN_ENC_KEY", "") or ""
    if not key:
        raise RuntimeError("TOKEN_ENC_KEY is not set")

    # 32-byte urlsafe base64 key required by Fernet
    try:
        raw = base64.urlsafe_b64decode(key.encode())
    except Exception:
        raw = b""
    if len(raw) != 32:
        raise RuntimeError("TOKEN_ENC_KEY must be a urlsafe-base64 encoded 32-byte key")

    return Fernet(key.encode())


def encrypt(text: str) -> str:
    return _fernet().encrypt(text.encode()).decode()


def decrypt(token: str) -> str:
    return _fernet().decrypt(token.encode()).decode()


def generate_token_enc_key() -> str:
    return Fernet.generate_key().decode()
