import json
import urllib.request
import urllib.error

def fetch_account_me(access_token: str, cluster: str = "asia") -> dict:
    """
    cluster: americas / europe / asia (推奨: サーバに近いもの)
    GET https://{cluster}.api.riotgames.com/riot/account/v1/accounts/me
    Authorization: Bearer {accessToken}
    """
    url = f"https://{cluster}.api.riotgames.com/riot/account/v1/accounts/me"
    req = urllib.request.Request(
        url,
        headers={"Authorization": f"Bearer {access_token}"},
        method="GET",
    )
    try:
        with urllib.request.urlopen(req, timeout=8) as r:
            return json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "ignore") if hasattr(e, "read") else ""
        raise RuntimeError(f"account_me_failed: {e.code} {body}")
