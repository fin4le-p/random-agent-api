# api/val_match.py
import urllib.request
import json

def _get(url: str, api_key: str, timeout: int = 10) -> dict:
    headers = {
        "X-Riot-Token": api_key,
        # ★ Cloudflare 1010対策（urllibの素のUAを避ける）
        "User-Agent": "Mozilla/5.0 (compatible; nakano6/1.0)",
        "Accept": "application/json",
    }
    req = urllib.request.Request(url, headers=headers, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "replace")
        raise RuntimeError(f"HTTPError {e.code} {e.reason} url={url} body={body}") from e

def matchlist_by_puuid(region: str, api_key: str, puuid: str) -> dict:
    url = f"https://{region}.api.riotgames.com/val/match/v1/matchlists/by-puuid/{puuid}"
    return _get(url, api_key)


def match_by_id(region: str, api_key: str, match_id: str) -> dict:
    url = f"https://{region}.api.riotgames.com/val/match/v1/matches/{match_id}"
    return _get(url, api_key)
