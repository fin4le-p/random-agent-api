# api/riot.py
import urllib.parse
import urllib.request
import urllib.error
import json
from datetime import timedelta

from django.conf import settings
from django.utils import timezone


def _json_request(url: str, method: str = "GET", headers: dict | None = None, data: dict | None = None, timeout: int = 10):
    h = headers or {}
    body = None
    if data is not None:
        body = json.dumps(data).encode("utf-8")
        h["Content-Type"] = "application/json"

    req = urllib.request.Request(url, data=body, headers=h, method=method)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        raw = r.read()
        return json.loads(raw.decode("utf-8"))


def calc_expires_at(expires_in_seconds: int) -> timezone.datetime:
    return timezone.now() + timedelta(seconds=int(expires_in_seconds))


def exchange_code_for_token(code: str) -> dict:
    token_url = getattr(settings, "RIOT_TOKEN_URL", "").rstrip("/")
    client_id = getattr(settings, "RIOT_CLIENT_ID", "")
    client_secret = getattr(settings, "RIOT_CLIENT_SECRET", "")
    redirect_uri = getattr(settings, "RIOT_REDIRECT_URI", "")

    if not (token_url and client_id and client_secret and redirect_uri):
        raise RuntimeError("RIOT token settings missing (RIOT_TOKEN_URL/CLIENT_ID/CLIENT_SECRET/REDIRECT_URI)")

    payload = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": redirect_uri,
    }

    data = urllib.parse.urlencode(payload).encode("utf-8")
    req = urllib.request.Request(token_url, data=data, method="POST")
    basic = (f"{client_id}:{client_secret}").encode("utf-8")
    import base64
    req.add_header("Authorization", "Basic " + base64.b64encode(basic).decode("ascii"))
    req.add_header("Content-Type", "application/x-www-form-urlencoded")

    with urllib.request.urlopen(req, timeout=10) as r:
        raw = r.read().decode("utf-8")
        return json.loads(raw)


def refresh_access_token(refresh_token: str) -> dict:
    token_url = getattr(settings, "RIOT_TOKEN_URL", "").rstrip("/")
    client_id = getattr(settings, "RIOT_CLIENT_ID", "")
    client_secret = getattr(settings, "RIOT_CLIENT_SECRET", "")

    if not (token_url and client_id and client_secret):
        raise RuntimeError("RIOT token settings missing (RIOT_TOKEN_URL/CLIENT_ID/CLIENT_SECRET)")

    payload = {
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
    }

    data = urllib.parse.urlencode(payload).encode("utf-8")
    req = urllib.request.Request(token_url, data=data, method="POST")
    basic = (f"{client_id}:{client_secret}").encode("utf-8")
    import base64
    req.add_header("Authorization", "Basic " + base64.b64encode(basic).decode("ascii"))
    req.add_header("Content-Type", "application/x-www-form-urlencoded")

    with urllib.request.urlopen(req, timeout=10) as r:
        raw = r.read().decode("utf-8")
        return json.loads(raw)


def fetch_userinfo(access_token: str) -> dict:
    url = getattr(settings, "RIOT_USERINFO_URL", "").rstrip("/")
    if not url:
        raise RuntimeError("RIOT_USERINFO_URL is not set")
    return _json_request(
        url,
        method="GET",
        headers={"Authorization": f"Bearer {access_token}"},
        timeout=10,
    )


def account_by_riot_id(game_name: str, tag_line: str) -> dict:
    api_key = getattr(settings, "RIOT_API_KEY", "")
    account_region = getattr(settings, "RIOT_ACCOUNT_REGION", "asia")  # americas/europe/asia
    if not api_key:
        raise RuntimeError("RIOT_API_KEY is not set (needed for account-v1 puuid lookup)")

    game_name_enc = urllib.parse.quote(game_name, safe="")
    tag_line_enc = urllib.parse.quote(tag_line, safe="")
    url = f"https://{account_region}.api.riotgames.com/riot/account/v1/accounts/by-riot-id/{game_name_enc}/{tag_line_enc}"
    return _json_request(url, method="GET", headers={"X-Riot-Token": api_key}, timeout=10)
