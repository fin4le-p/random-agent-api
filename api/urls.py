from django.urls import path
from .views import InternalCreateAuthUrl, InternalExchangeCode, InternalEnsureFreshToken

urlpatterns = [
    path("internal/rso/create-auth-url", InternalCreateAuthUrl.as_view()),
    path("internal/rso/exchange", InternalExchangeCode.as_view()),
    path("internal/rso/ensure-fresh-token", InternalEnsureFreshToken.as_view()),
]
