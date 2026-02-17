from rest_framework import serializers

class CreateAuthUrlRequest(serializers.Serializer):
    discord_user_id = serializers.IntegerField()
    discord_guild_id = serializers.IntegerField(required=False, allow_null=True)
    region = serializers.CharField(required=False, default="ap")

class ExchangeCodeRequest(serializers.Serializer):
    code = serializers.CharField()
    state = serializers.CharField()

class RecentMatchesRequest(serializers.Serializer):
    discord_user_id = serializers.IntegerField()