import secrets
import traceback
import urllib.parse
from datetime import timedelta

from django.conf import settings
from django.db import transaction
from django.utils import timezone as dj_timezone

from rest_framework import status
from rest_framework.response import Response
from rest_framework.views import APIView

from ..auth import InternalAPIKeyPermission
from ..crypto import decrypt, encrypt
from ..models import AccountLink, OAuthState, RiotToken
from ..integrations.riot import (
    account_by_riot_id,
    account_me,
    calc_expires_at,
    exchange_code_for_token,
    fetch_userinfo,
    refresh_access_token,
)
from ..serializers import CreateAuthUrlRequest, ExchangeCodeRequest


def build_authorize_url(state: str) -> str:
    params = {
        "client_id": settings.RIOT_CLIENT_ID,
        "redirect_uri": settings.RIOT_REDIRECT_URI,
        "response_type": "code",
        "scope": "openid offline_access",
        "state": state,
    }
    return f"{settings.RIOT_AUTH_BASE.rstrip('/')}/authorize?" + urllib.parse.urlencode(params)


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
            expires_at=dj_timezone.now() + timedelta(minutes=10),
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
            return Response({"ok": False, "error": "missing_discord_user_id"}, status=400)

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
