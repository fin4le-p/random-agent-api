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
from rest_framework.permissions import AllowAny

from .auth import InternalAPIKeyPermission
from .serializers import CreateAuthUrlRequest, ExchangeCodeRequest
from .models import OAuthState, AccountLink, RiotToken
from .crypto import encrypt, decrypt
from .riot import exchange_code_for_token, fetch_userinfo, refresh_access_token, calc_expires_at


def build_authorize_url(state: str) -> str:
    params = {
        "client_id": settings.RIOT_CLIENT_ID,
        "redirect_uri": settings.RIOT_REDIRECT_URI,
        "response_type": "code",
        "scope": "openid offline_access",
        "state": state,
    }
    return f"{settings.RIOT_AUTH_BASE}/authorize?" + urllib.parse.urlencode(params)


class InternalCreateAuthUrl(APIView):
    permission_classes = [InternalAPIKeyPermission]

    def post(self, request):
        ser = CreateAuthUrlRequest(data=request.data)
        ser.is_valid(raise_exception=True)

        discord_user_id = int(ser.validated_data["discord_user_id"])
        discord_guild_id = ser.validated_data.get("discord_guild_id")
        region = ser.validated_data.get("region", "ap")

        state = secrets.token_urlsafe(32)
        OAuthState.objects.create(
            state=state,
            discord_user_id=discord_user_id,
            discord_guild_id=discord_guild_id,
            expires_at=timezone.now() + timedelta(minutes=10),
        )

        url = build_authorize_url(state)
        return Response({"authorize_url": url, "state": state, "region": region})


class InternalExchangeCode(APIView):
    """
    Nextの callback から code/state を受け取り、token交換して保存
    """
    permission_classes = [InternalAPIKeyPermission]

    @transaction.atomic
    def post(self, request):
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
                return Response({"error": "missing_refresh_token", "token_keys": list(token_json.keys())},
                                status=status.HTTP_400_BAD_REQUEST)

            expires_at = calc_expires_at(int(token_json.get("expires_in", 3600)))
            scope = token_json.get("scope", "")
            token_type = token_json.get("token_type", "Bearer")

            # 2) userinfo
            userinfo = fetch_userinfo(access_token)
            riot_subject = userinfo.get("sub", "")
            if not riot_subject:
                return Response({"error": "missing_userinfo_sub", "userinfo_keys": list(userinfo.keys())},
                                status=status.HTTP_400_BAD_REQUEST)

            game_name = userinfo.get("game_name", "") or userinfo.get("acct", {}).get("game_name", "") or ""
            tag_line = userinfo.get("tag_line", "") or userinfo.get("acct", {}).get("tag_line", "") or ""

            # 3) upsert link/token
            link, _created = AccountLink.objects.update_or_create(
                discord_user_id=st.discord_user_id,
                defaults={
                    "discord_guild_id": st.discord_guild_id,
                    "riot_subject": riot_subject,
                    "riot_game_name": game_name,
                    "riot_tag_line": tag_line,
                    "region": getattr(st, "region", "ap") if hasattr(st, "region") else "ap",
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

            # 4) 成功したら state を消費（最後に delete）
            st.delete()

            return Response({
                "ok": True,
                "discord_user_id": link.discord_user_id,
                "riot_subject": link.riot_subject,
                "riot_game_name": link.riot_game_name,
                "riot_tag_line": link.riot_tag_line,
            })

        except Exception as e:
            # ここが “今あなたの環境で起きてる 500 の正体” を必ず出す
            tb = traceback.format_exc()
            print("[InternalExchangeCode] exception:", repr(e))
            print(tb)

            # Next 側でも読めるように JSON で返す（機密を出しすぎない）
            return Response(
                {"error": "exchange_exception", "detail": repr(e)},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )


class InternalEnsureFreshToken(APIView):
    permission_classes = [InternalAPIKeyPermission]

    @transaction.atomic
    def post(self, request):
        discord_user_id = int(request.data.get("discord_user_id", 0))
        if not discord_user_id:
            return Response({"error": "missing_discord_user_id"}, status=400)

        link = AccountLink.objects.filter(discord_user_id=discord_user_id).first()
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
        tok.save(update_fields=["access_token","refresh_token_enc","expires_at","scope","token_type","updated_at"])

        return Response({"ok": True, "refreshed": True})

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

        has_token = hasattr(link, "token")
        return Response({
            "linked": True,
            "has_token": bool(has_token),
            "riot_game_name": link.riot_game_name,
            "riot_tag_line": link.riot_tag_line,
            "riot_subject": link.riot_subject,
            "expires_at": link.token.expires_at if has_token else None,
        }, status=200)


class InternalMe(APIView):
    """
    Botが叩く：期限切れならrefresh→userinfoを取って返す（疎通確認用）
    """
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

        # 期限切れならrefresh
        refreshed = False
        if tok.is_expired():
            refresh_token = decrypt(tok.refresh_token_enc)
            new_tok = refresh_access_token(refresh_token)

            tok.access_token = new_tok["access_token"]
            new_refresh = new_tok.get("refresh_token")
            if new_refresh:
                tok.refresh_token_enc = encrypt(new_refresh)
            tok.expires_at = calc_expires_at(int(new_tok.get("expires_in", 3600)))
            tok.scope = new_tok.get("scope", tok.scope)
            tok.token_type = new_tok.get("token_type", tok.token_type)
            tok.save(update_fields=["access_token","refresh_token_enc","expires_at","scope","token_type","updated_at"])
            refreshed = True

        # userinfo取得（access_tokenの疎通確認）
        userinfo = fetch_userinfo(tok.access_token)

        # 返す（できればgame_name/tag_lineを返す）
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

        return Response({
            "ok": True,
            "refreshed": refreshed,
            "discord_user_id": link.discord_user_id,
            "riot_subject": userinfo.get("sub", "") or link.riot_subject,
            "game_name": game_name,
            "tag_line": tag_line,
            "expires_at": tok.expires_at,
        }, status=200)