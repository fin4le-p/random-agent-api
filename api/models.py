# api/models.py
from datetime import timedelta

from django.db import models
from django.utils import timezone


class OAuthState(models.Model):
    state = models.CharField(max_length=256, unique=True)
    discord_user_id = models.BigIntegerField()
    discord_guild_id = models.BigIntegerField(null=True, blank=True)
    region = models.CharField(max_length=16, default="ap")

    expires_at = models.DateTimeField()
    created_at = models.DateTimeField(auto_now_add=True)

    def is_expired(self) -> bool:
        return timezone.now() >= self.expires_at


class AccountLink(models.Model):
    discord_user_id = models.BigIntegerField(unique=True)
    discord_guild_id = models.BigIntegerField(null=True, blank=True)

    riot_subject = models.CharField(max_length=512, blank=True, default="")
    riot_game_name = models.CharField(max_length=64, blank=True, default="")
    riot_tag_line = models.CharField(max_length=32, blank=True, default="")
    riot_puuid = models.CharField(max_length=128, blank=True, default="")

    region = models.CharField(max_length=16, default="ap")

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)


class RiotToken(models.Model):
    link = models.OneToOneField(AccountLink, on_delete=models.CASCADE, related_name="token")

    access_token = models.TextField(blank=True, default="")
    refresh_token_enc = models.TextField(blank=True, default="")

    expires_at = models.DateTimeField()
    scope = models.TextField(blank=True, default="")
    token_type = models.CharField(max_length=32, blank=True, default="Bearer")

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def is_expired(self, leeway_seconds: int = 30) -> bool:
        return timezone.now() >= (self.expires_at - timedelta(seconds=leeway_seconds))
