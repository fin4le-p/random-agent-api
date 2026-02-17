from rest_framework.permissions import BasePermission
from django.conf import settings

class InternalAPIKeyPermission(BasePermission):
    def has_permission(self, request, view):
        key = request.headers.get("X-Internal-API-Key", "")
        return bool(settings.INTERNAL_API_KEY) and key == settings.INTERNAL_API_KEY
