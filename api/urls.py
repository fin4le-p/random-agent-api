# api/urls.py
from django.urls import path
from .views import (
    InternalCreateAuthUrl,
    InternalExchangeCode,
    InternalEnsureFreshToken,
    InternalLinkStatus,
    InternalMe,
    InternalValorantRecentMatches,
    InternalValorantMatchHighlight,
)

urlpatterns = [
    path("internal/rso/create-auth-url", InternalCreateAuthUrl.as_view()),
    path("internal/rso/exchange", InternalExchangeCode.as_view()),
    path("internal/rso/ensure-fresh-token", InternalEnsureFreshToken.as_view()),
    path("internal/rso/status", InternalLinkStatus.as_view()),
    path("internal/rso/me", InternalMe.as_view()),
    path("internal/val/recent-matches", InternalValorantRecentMatches.as_view()),
    path("internal/val/match-highlight", InternalValorantMatchHighlight.as_view()),
]
