# api/val_match.py
import urllib.request
import json


def _get(url: str, api_key: str, timeout: int = 10) -> dict:
    req = urllib.request.Request(url, headers={"X-Riot-Token": api_key}, method="GET")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def matchlist_by_puuid(region: str, api_key: str, puuid: str) -> dict:
    url = f"https://{region}.api.riotgames.com/val/match/v1/matchlists/by-puuid/{puuid}"
    return _get(url, api_key)


def match_by_id(region: str, api_key: str, match_id: str) -> dict:
    url = f"https://{region}.api.riotgames.com/val/match/v1/matches/{match_id}"
    return _get(url, api_key)
