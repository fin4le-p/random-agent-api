# api/views.py
import secrets
import urllib.parse
from datetime import timedelta
import traceback

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework import status

from .auth import InternalAPIKeyPermission
from .serializers import CreateAuthUrlRequest, ExchangeCodeRequest
from .models import OAuthState, AccountLink, RiotToken
from .crypto import encrypt, decrypt
from .riot import (
    exchange_code_for_token,
    fetch_userinfo,
    refresh_access_token,
    calc_expires_at,
    account_by_riot_id,
    account_me,  # ★追加
)
from .val_match import matchlist_by_puuid, match_by_id


def build_authorize_url(state: str) -> str:
    params = {
        "client_id": settings.RIOT_CLIENT_ID,
        "redirect_uri": settings.RIOT_REDIRECT_URI,
        "response_type": "code",
        "scope": "openid offline_access",
        "state": state,
    }
    return f"{settings.RIOT_AUTH_BASE.rstrip('/')}/authorize?" + urllib.parse.urlencode(params)


def _map_name(map_id: str) -> str:
    if not map_id:
        return "Unknown"
    s = map_id.strip("/").split("/")[-1]
    return s or map_id


class InternalCreateAuthUrl(APIView):
    permission_classes = [InternalAPIKeyPermission]

    def post(self, request):
        ser = CreateAuthUrlRequest(data=request.data)
        ser.is_valid(raise_exception=True)

        discord_user_id = int(ser.validated_data["discord_user_id"])
        discord_guild_id = ser.validated_data.get("discord_guild_id")
        region = ser.validated_data.get("region") or "ap"

        state_val = secrets.token_urlsafe(32)
        OAuthState.objects.create(
            state=state_val,
            discord_user_id=discord_user_id,
            discord_guild_id=discord_guild_id,
            region=region,
            expires_at=timezone.now() + timedelta(minutes=10),
        )

        url = build_authorize_url(state_val)
        return Response({"authorize_url": url, "state": state_val, "region": region})


class InternalExchangeCode(APIView):
    """
    Link（callback）時点で gameName/tagLine/puuid まで確定させて保存する
    """
    permission_classes = [InternalAPIKeyPermission]

    @transaction.atomic
    def post(self, request):
        ser = ExchangeCodeRequest(data=request.data)
        ser.is_valid(raise_exception=True)
        code = ser.validated_data["code"]
        state_val = ser.validated_data["state"]

        st = OAuthState.objects.filter(state=state_val).first()
        if not st or st.is_expired():
            return Response({"error": "invalid_or_expired_state"}, status=status.HTTP_400_BAD_REQUEST)

        try:
            token_json = exchange_code_for_token(code)

            access_token = token_json["access_token"]
            refresh_token = token_json.get("refresh_token")
            if not refresh_token:
                return Response(
                    {"error": "missing_refresh_token", "token_keys": list(token_json.keys())},
                    status=status.HTTP_400_BAD_REQUEST,
                )

            expires_at = calc_expires_at(int(token_json.get("expires_in", 3600)))
            scope = token_json.get("scope", "")
            token_type = token_json.get("token_type", "Bearer")

            # ★1) sub は userinfo（openid）で取れるなら取る（無くても通す）
            riot_subject = ""
            try:
                userinfo = fetch_userinfo(access_token)
                riot_subject = userinfo.get("sub", "") or ""
            except Exception:
                riot_subject = ""

            # ★2) ここが本命：公式 accounts/me で puuid / gameName / tagLine を確定
            me = account_me(access_token)
            game_name = me.get("gameName", "") or ""
            tag_line = me.get("tagLine", "") or ""
            puuid = me.get("puuid", "") or ""

            # accounts/me が一部欠けるケースの保険（公式のみ）
            # gameName/tagLine が取れてるのに puuid が空なら account-v1 by-riot-id で補完
            if (not puuid) and game_name and tag_line:
                try:
                    acct = account_by_riot_id(game_name, tag_line)
                    puuid = acct.get("puuid", "") or ""
                except Exception:
                    puuid = ""

            link, _created = AccountLink.objects.update_or_create(
                discord_user_id=st.discord_user_id,
                defaults={
                    "discord_guild_id": st.discord_guild_id,
                    "riot_subject": riot_subject,
                    "riot_game_name": game_name,
                    "riot_tag_line": tag_line,
                    "riot_puuid": puuid,
                    "region": st.region or "ap",
                },
            )

            RiotToken.objects.update_or_create(
                link=link,
                defaults={
                    "access_token": access_token,
                    "refresh_token_enc": encrypt(refresh_token),
                    "expires_at": expires_at,
                    "scope": scope,
                    "token_type": token_type,
                },
            )

            st.delete()

            return Response(
                {
                    "ok": True,
                    "discord_user_id": link.discord_user_id,
                    "riot_subject": link.riot_subject,
                    "riot_game_name": link.riot_game_name,
                    "riot_tag_line": link.riot_tag_line,
                    "riot_puuid": link.riot_puuid,
                }
            )

        except Exception as e:
            tb = traceback.format_exc()
            print("[InternalExchangeCode] exception:", repr(e))
            print(tb)
            return Response(
                {"error": "exchange_exception", "detail": repr(e)},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )


class InternalEnsureFreshToken(APIView):
    permission_classes = [InternalAPIKeyPermission]

    @transaction.atomic
    def post(self, request):
        discord_user_id = int(request.data.get("discord_user_id", 0))
        if not discord_user_id:
            return Response({"error": "missing_discord_user_id"}, status=400)

        link = AccountLink.objects.filter(discord_user_id=discord_user_id).select_related("token").first()
        if not link or not hasattr(link, "token"):
            return Response({"error": "not_linked"}, status=404)

        tok = link.token
        if not tok.is_expired():
            return Response({"ok": True, "refreshed": False})

        refresh_token = decrypt(tok.refresh_token_enc)
        new_tok = refresh_access_token(refresh_token)

        tok.access_token = new_tok["access_token"]
        new_refresh = new_tok.get("refresh_token")
        if new_refresh:
            tok.refresh_token_enc = encrypt(new_refresh)
        tok.expires_at = calc_expires_at(int(new_tok.get("expires_in", 3600)))
        tok.scope = new_tok.get("scope", tok.scope)
        tok.token_type = new_tok.get("token_type", tok.token_type)
        tok.save(update_fields=["access_token", "refresh_token_enc", "expires_at", "scope", "token_type", "updated_at"])

        return Response({"ok": True, "refreshed": True})


class InternalLinkStatus(APIView):
    permission_classes = [InternalAPIKeyPermission]

    def post(self, request):
        discord_user_id = int(request.data.get("discord_user_id", 0))
        if not discord_user_id:
            return Response({"error": "missing_discord_user_id"}, status=400)

        link = AccountLink.objects.filter(discord_user_id=discord_user_id).select_related("token").first()
        if not link:
            return Response({"linked": False}, status=200)

        has_token = hasattr(link, "token")
        return Response(
            {
                "linked": True,
                "has_token": bool(has_token),
                "riot_game_name": link.riot_game_name,
                "riot_tag_line": link.riot_tag_line,
                "riot_subject": link.riot_subject,
                "riot_puuid": link.riot_puuid,
                "expires_at": link.token.expires_at if has_token else None,
            },
            status=200,
        )


class InternalMe(APIView):
    """
    連携確認ではなく「直近1試合が取れる」ことを確認するためのエンドポイント
    - refresh 等はここではやらない（連携機能は無視）
    """
    permission_classes = [InternalAPIKeyPermission]

    def post(self, request):
        discord_user_id = int(request.data.get("discord_user_id", 0))
        if not discord_user_id:
            return Response({"error": "missing_discord_user_id"}, status=400)

        link = AccountLink.objects.filter(discord_user_id=discord_user_id).first()
        if not link:
            return Response({"error": "not_linked"}, status=404)

        if not link.riot_puuid:
            return Response({"error": "missing_puuid"}, status=404)

        api_key = getattr(settings, "RIOT_API_KEY", "")
        if not api_key:
            return Response({"error": "server_misconfigured_riot_api_key"}, status=500)

        region = getattr(settings, "VAL_MATCH_REGION", None) or (link.region or "ap")

        try:
            ml = matchlist_by_puuid(region, api_key, link.riot_puuid)
            history = (ml.get("history", []) or [])
            if not history:
                return Response({"error": "no_match_history"}, status=404)

            match_id = history[0].get("matchId")
            if not match_id:
                return Response({"error": "invalid_match_history"}, status=502)

            m = match_by_id(region, api_key, match_id)
            info = m.get("matchInfo", {}) or {}
            players = m.get("players", []) or []
            teams = m.get("teams", []) or []

            me = next((p for p in players if p.get("puuid") == link.riot_puuid), None)
            if not me:
                return Response({"error": "player_not_found_in_match"}, status=502)

            team_id = me.get("teamId")
            stats0 = (me.get("stats", {}) or {})
            kills = int(stats0.get("kills", 0))
            deaths = int(stats0.get("deaths", 0))
            assists = int(stats0.get("assists", 0))
            score = int(stats0.get("score", 0))
            rounds = int(stats0.get("roundsPlayed", 0)) or 1
            acs = round(score / rounds, 1)

            my_team = next((t for t in teams if t.get("teamId") == team_id), None)
            won = bool(my_team.get("won")) if my_team else None

            return Response(
                {
                    "ok": True,
                    "riotId": f"{link.riot_game_name}#{link.riot_tag_line}".strip("#"),
                    "puuid": link.riot_puuid,
                    "region": region,
                    "match": {
                        "matchId": match_id,
                        "map": _map_name(info.get("mapId", "")),
                        "mode": info.get("gameMode", "") or info.get("queueId", ""),
                        "isCompleted": bool(info.get("isCompleted", True)),
                        "won": won,
                        "k": kills,
                        "d": deaths,
                        "a": assists,
                        "acs": acs,
                        "teamId": team_id,
                        "gameStartMillis": info.get("gameStartMillis"),
                    },
                }
            )

        except Exception as e:
            return Response({"error": "match_fetch_exception", "detail": repr(e)}, status=502)


class InternalValorantRecentMatches(APIView):
    permission_classes = [InternalAPIKeyPermission]

    def post(self, request):
        discord_user_id = int(request.data.get("discord_user_id", 0))
        count = int(request.data.get("count", 5))
        count = max(1, min(count, 10))

        if not discord_user_id:
            return Response({"error": "missing_discord_user_id"}, status=400)

        link = AccountLink.objects.filter(discord_user_id=discord_user_id).first()
        if not link:
            return Response({"error": "not_linked"}, status=404)

        # PUUID が無ければ account-v1 で埋める（公式のみ）
        if not link.riot_puuid:
            if not (link.riot_game_name and link.riot_tag_line):
                return Response({"error": "not_linked_or_missing_riot_id"}, status=404)
            try:
                acct = account_by_riot_id(link.riot_game_name, link.riot_tag_line)
                puuid = acct.get("puuid", "") or ""
                if not puuid:
                    return Response({"error": "puuid_lookup_failed"}, status=502)
                link.riot_puuid = puuid
                link.save(update_fields=["riot_puuid", "updated_at"])
            except Exception as e:
                return Response({"error": "puuid_lookup_exception", "detail": repr(e)}, status=502)

        api_key = getattr(settings, "RIOT_API_KEY", "")
        if not api_key:
            return Response({"error": "server_misconfigured_riot_api_key"}, status=500)

        region = getattr(settings, "VAL_MATCH_REGION", None) or (link.region or "ap")

        ml = matchlist_by_puuid(region, api_key, link.riot_puuid)
        history = (ml.get("history", []) or [])[:count]

        items = []
        for h in history:
            match_id = h.get("matchId")
            if not match_id:
                continue

            m = match_by_id(region, api_key, match_id)
            info = m.get("matchInfo", {}) or {}
            players = m.get("players", []) or []
            teams = m.get("teams", []) or []

            me = next((p for p in players if p.get("puuid") == link.riot_puuid), None)
            if not me:
                continue

            team_id = me.get("teamId")
            stats0 = (me.get("stats", {}) or {})
            kills = int(stats0.get("kills", 0))
            deaths = int(stats0.get("deaths", 0))
            assists = int(stats0.get("assists", 0))
            score = int(stats0.get("score", 0))
            rounds = int(stats0.get("roundsPlayed", 0)) or 1
            acs = round(score / rounds, 1)

            my_team = next((t for t in teams if t.get("teamId") == team_id), None)
            won = bool(my_team.get("won")) if my_team else None

            items.append(
                {
                    "matchId": match_id,
                    "map": _map_name(info.get("mapId", "")),
                    "mode": info.get("gameMode", "") or info.get("queueId", ""),
                    "isCompleted": bool(info.get("isCompleted", True)),
                    "won": won,
                    "k": kills,
                    "d": deaths,
                    "a": assists,
                    "acs": acs,
                    "teamId": team_id,
                    "gameStartMillis": info.get("gameStartMillis"),
                }
            )

        return Response(
            {
                "ok": True,
                "riotId": f"{link.riot_game_name}#{link.riot_tag_line}".strip("#"),
                "puuid": link.riot_puuid,
                "region": region,
                "matches": items,
            }
        )
