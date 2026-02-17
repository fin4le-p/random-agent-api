# api/auth.py
from rest_framework.permissions import BasePermission
from django.conf import settings


class InternalAPIKeyPermission(BasePermission):
    def has_permission(self, request, view):
        expected = getattr(settings, "INTERNAL_API_KEY", "")
        if not expected:
            return False
        got = request.headers.get("X-Internal-API-Key", "")
        return got == expected
