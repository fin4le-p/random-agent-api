import os
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
)


# -------------------------
# helpers
# -------------------------

def build_authorize_url(state: str) -> str:
    params = {
        "client_id": settings.RIOT_CLIENT_ID,
        "redirect_uri": settings.RIOT_REDIRECT_URI,
        "response_type": "code",
        "scope": "openid offline_access",
        "state": state,
    }
    return f"{settings.RIOT_AUTH_BASE}/authorize?" + urllib.parse.urlencode(params)


def _has_token_enc_key() -> bool:
    # TOKEN_ENC_KEY は settings 経由でも env 直でもいいが、あなたの crypto.py が env を見るなら env を優先
    return bool(getattr(settings, "TOKEN_ENC_KEY", None) or os.getenv("TOKEN_ENC_KEY"))


def _get_link_token(link: AccountLink):
    """
    link.token が OneToOne の想定。
    ただしモデル次第で例外出る可能性があるので安全に取る。
    """
    try:
        return getattr(link, "token", None)
    except Exception:
        return None


def _update_token_from_refresh(tok: RiotToken) -> bool:
    """
    期限切れなら refresh して tok を更新して保存する。
    更新が発生したら True。
    """
    if not tok.is_expired():
        return False

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
    return True


# -------------------------
# views
# -------------------------

class InternalCreateAuthUrl(APIView):
    permission_classes = [InternalAPIKeyPermission]

    def post(self, request):
        ser = CreateAuthUrlRequest(data=request.data)
        ser.is_valid(raise_exception=True)

        discord_user_id = int(ser.validated_data["discord_user_id"])
        discord_guild_id = ser.validated_data.get("discord_guild_id")
        region = ser.validated_data.get("region", "ap")

        state = secrets.token_urlsafe(32)

        # OAuthState に region カラムが無いケースでも落ちないように
        create_kwargs = dict(
            state=state,
            discord_user_id=discord_user_id,
            discord_guild_id=discord_guild_id,
            expires_at=timezone.now() + timedelta(minutes=10),
        )
        if hasattr(OAuthState, "region"):
            create_kwargs["region"] = region

        OAuthState.objects.create(**create_kwargs)

        url = build_authorize_url(state)
        return Response({"authorize_url": url, "state": state, "region": region})


class InternalExchangeCode(APIView):
    """
    Next の callback から code/state を受け取り、token交換して保存
    """
    permission_classes = [InternalAPIKeyPermission]

    @transaction.atomic
    def post(self, request):
        # 暗号鍵が無いと refresh_token の保存で必ず落ちるので、早めにエラー化
        if not _has_token_enc_key():
            return Response(
                {"error": "server_misconfigured", "detail": "TOKEN_ENC_KEY is not set"},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )

        ser = ExchangeCodeRequest(data=request.data)
        ser.is_valid(raise_exception=True)
        code = ser.validated_data["code"]
        state = ser.validated_data["state"]

        st = OAuthState.objects.filter(state=state).first()
        if not st or st.is_expired():
            return Response({"error": "invalid_or_expired_state"}, status=status.HTTP_400_BAD_REQUEST)

        try:
            # 1) code -> token
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

            # 2) userinfo
            userinfo = fetch_userinfo(access_token)
            riot_subject = userinfo.get("sub", "")
            if not riot_subject:
                return Response(
                    {"error": "missing_userinfo_sub", "userinfo_keys": list(userinfo.keys())},
                    status=status.HTTP_400_BAD_REQUEST,
                )

            game_name = userinfo.get("game_name", "") or userinfo.get("acct", {}).get("game_name", "") or ""
            tag_line = userinfo.get("tag_line", "") or userinfo.get("acct", {}).get("tag_line", "") or ""

            # 3) upsert link/token
            region = getattr(st, "region", "ap") if hasattr(st, "region") else "ap"

            link, _created = AccountLink.objects.update_or_create(
                discord_user_id=st.discord_user_id,
                defaults={
                    "discord_guild_id": st.discord_guild_id,
                    "riot_subject": riot_subject,
                    "riot_game_name": game_name,
                    "riot_tag_line": tag_line,
                    "region": region,
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

            # 4) 成功したら state を消費
            st.delete()

            return Response(
                {
                    "ok": True,
                    "discord_user_id": link.discord_user_id,
                    "riot_subject": link.riot_subject,
                    "riot_game_name": link.riot_game_name,
                    "riot_tag_line": link.riot_tag_line,
                },
                status=200,
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
        if not _has_token_enc_key():
            return Response(
                {"error": "server_misconfigured", "detail": "TOKEN_ENC_KEY is not set"},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )

        discord_user_id = int(request.data.get("discord_user_id", 0))
        if not discord_user_id:
            return Response({"error": "missing_discord_user_id"}, status=400)

        link = AccountLink.objects.filter(discord_user_id=discord_user_id).first()
        if not link:
            return Response({"error": "not_linked"}, status=404)

        tok = _get_link_token(link)
        if not tok:
            return Response({"error": "missing_token"}, status=404)

        refreshed = _update_token_from_refresh(tok)
        return Response({"ok": True, "refreshed": refreshed}, status=200)


class InternalLinkStatus(APIView):
    """
    discord_user_id の連携状況を返す
    """
    permission_classes = [InternalAPIKeyPermission]

    def post(self, request):
        discord_user_id = int(request.data.get("discord_user_id", 0))
        if not discord_user_id:
            return Response({"error": "missing_discord_user_id"}, status=400)

        link = AccountLink.objects.filter(discord_user_id=discord_user_id).first()
        if not link:
            return Response({"linked": False}, status=200)

        tok = _get_link_token(link)
        return Response(
            {
                "linked": True,
                "has_token": bool(tok),
                "riot_game_name": link.riot_game_name,
                "riot_tag_line": link.riot_tag_line,
                "riot_subject": link.riot_subject,
                "expires_at": tok.expires_at if tok else None,
            },
            status=200,
        )


class InternalMe(APIView):
    """
    Botが叩く：期限切れならrefresh→userinfoを取って返す（疎通確認用）
    """
    permission_classes = [InternalAPIKeyPermission]

    @transaction.atomic
    def post(self, request):
        if not _has_token_enc_key():
            return Response(
                {"error": "server_misconfigured", "detail": "TOKEN_ENC_KEY is not set"},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )

        discord_user_id = int(request.data.get("discord_user_id", 0))
        if not discord_user_id:
            return Response({"error": "missing_discord_user_id"}, status=400)

        # select_related("token") がモデル次第で失敗する可能性があるので安全に
        try:
            link = AccountLink.objects.filter(discord_user_id=discord_user_id).select_related("token").first()
        except Exception:
            link = AccountLink.objects.filter(discord_user_id=discord_user_id).first()

        if not link:
            return Response({"error": "not_linked"}, status=404)

        tok = _get_link_token(link)
        if not tok:
            return Response({"error": "missing_token"}, status=404)

        refreshed = _update_token_from_refresh(tok)

        userinfo = fetch_userinfo(tok.access_token)

        game_name = (
            userinfo.get("game_name", "")
            or userinfo.get("acct", {}).get("game_name", "")
            or link.riot_game_name
            or ""
        )
        tag_line = (
            userinfo.get("tag_line", "")
            or userinfo.get("acct", {}).get("tag_line", "")
            or link.riot_tag_line
            or ""
        )

        return Response(
            {
                "ok": True,
                "refreshed": refreshed,
                "discord_user_id": link.discord_user_id,
                "riot_subject": userinfo.get("sub", "") or link.riot_subject,
                "game_name": game_name,
                "tag_line": tag_line,
                "expires_at": tok.expires_at,
            },
            status=200,
        )
