from django.db import models
from django.utils import timezone


class AccountLink(models.Model):
    """
    Discordユーザー ↔ Riotアカウント の紐付け
    """
    discord_user_id = models.BigIntegerField(db_index=True, unique=True)
    discord_guild_id = models.BigIntegerField(null=True, blank=True, db_index=True)

    # userinfoの sub（RSOのユーザー識別子）を保存（最低限）
    riot_subject = models.CharField(max_length=512, unique=True, db_index=True)

    # 表示用（取れたら入れる）
    riot_game_name = models.CharField(max_length=64, blank=True, default="")
    riot_tag_line = models.CharField(max_length=16, blank=True, default="")

    region = models.CharField(max_length=8, default="ap")

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)


class RiotToken(models.Model):
    """
    access/refresh token（refreshは暗号化して保存推奨）
    """
    link = models.OneToOneField(AccountLink, on_delete=models.CASCADE, related_name="token")

    access_token = models.TextField()
    refresh_token_enc = models.TextField()  # 暗号化済みrefresh token

    scope = models.TextField(blank=True, default="")
    token_type = models.CharField(max_length=32, blank=True, default="Bearer")
    expires_at = models.DateTimeField()

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def is_expired(self) -> bool:
        return timezone.now() >= self.expires_at


class OAuthState(models.Model):
    """
    stateの検証用（改ざん・なりすまし防止）
    """
    state = models.CharField(max_length=128, unique=True, db_index=True)
    discord_user_id = models.BigIntegerField(db_index=True)
    discord_guild_id = models.BigIntegerField(null=True, blank=True, db_index=True)

    expires_at = models.DateTimeField()

    created_at = models.DateTimeField(auto_now_add=True)

    def is_expired(self) -> bool:
        return timezone.now() >= self.expires_at
