from django.db import models
from django.utils import timezone


class AccountLink(models.Model):
    discord_user_id = models.BigIntegerField(unique=True)
    discord_guild_id = models.BigIntegerField(null=True, blank=True)

    riot_subject = models.CharField(max_length=512, default="", blank=True)  # ←ここはあなたが migration 作ったやつに合わせて調整
    riot_puuid = models.CharField(max_length=78, default="", blank=True)     # ←追加（PUUIDは最大78程度）
    riot_game_name = models.CharField(max_length=64, default="", blank=True)
    riot_tag_line = models.CharField(max_length=16, default="", blank=True)

    region = models.CharField(max_length=8, default="ap")
    created_at = models.DateTimeField(default=timezone.now)
    updated_at = models.DateTimeField(auto_now=True)

class RiotToken(models.Model):
    link = models.OneToOneField(AccountLink, on_delete=models.CASCADE, related_name="token")
    access_token = models.TextField(default="", blank=True)
    refresh_token_enc = models.TextField(default="", blank=True)
    expires_at = models.DateTimeField(null=True, blank=True)
    scope = models.TextField(default="", blank=True)
    token_type = models.CharField(max_length=16, default="Bearer")
    created_at = models.DateTimeField(default=timezone.now)
    updated_at = models.DateTimeField(auto_now=True)

    def is_expired(self):
        if not self.expires_at:
            return True
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
