# api/serializers.py
from rest_framework import serializers


class CreateAuthUrlRequest(serializers.Serializer):
    discord_user_id = serializers.IntegerField()
    discord_guild_id = serializers.IntegerField(required=False, allow_null=True)
    region = serializers.CharField(required=False, allow_blank=True)


class ExchangeCodeRequest(serializers.Serializer):
    code = serializers.CharField()
    state = serializers.CharField()
