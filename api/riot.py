import requests
from datetime import timedelta
from django.conf import settings
from django.utils import timezone


def exchange_code_for_token(code: str) -> dict:
    """
    authorization_code -> token
    """
    url = f"{settings.RIOT_AUTH_BASE}/token"
    resp = requests.post(
        url,
        data={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": settings.RIOT_REDIRECT_URI,
        },
        auth=(settings.RIOT_CLIENT_ID, settings.RIOT_CLIENT_SECRET),
        timeout=15,
    )
    resp.raise_for_status()
    return resp.json()


def fetch_userinfo(access_token: str) -> dict:
    """
    access_token -> userinfo
    """
    url = f"{settings.RIOT_AUTH_BASE}/userinfo"
    resp = requests.get(
        url,
        headers={"Authorization": f"Bearer {access_token}"},
        timeout=15,
    )
    resp.raise_for_status()
    return resp.json()


def refresh_access_token(refresh_token: str) -> dict:
    """
    refresh_token -> new token
    """
    url = f"{settings.RIOT_AUTH_BASE}/token"
    resp = requests.post(
        url,
        data={
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
            "redirect_uri": settings.RIOT_REDIRECT_URI,
        },
        auth=(settings.RIOT_CLIENT_ID, settings.RIOT_CLIENT_SECRET),
        timeout=15,
    )
    resp.raise_for_status()
    return resp.json()


def calc_expires_at(expires_in: int) -> timezone.datetime:
    # バッファを少し引く（期限ギリギリ事故防止）
    sec = max(60, int(expires_in) - 60)
    return timezone.now() + timedelta(seconds=sec)
