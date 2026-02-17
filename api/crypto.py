from cryptography.fernet import Fernet
from django.conf import settings

def _fernet() -> Fernet:
    if not settings.TOKEN_ENC_KEY:
        raise RuntimeError("TOKEN_ENC_KEY is not set")
    return Fernet(settings.TOKEN_ENC_KEY.encode() if isinstance(settings.TOKEN_ENC_KEY, str) else settings.TOKEN_ENC_KEY)

def encrypt(text: str) -> str:
    return _fernet().encrypt(text.encode()).decode()

def decrypt(token: str) -> str:
    return _fernet().decrypt(token.encode()).decode()
