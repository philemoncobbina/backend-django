"""Helpers and views shared by the website, admin and student apps."""

import json
import logging

from django.conf import settings
from django.contrib.auth import logout
from django.core.exceptions import ObjectDoesNotExist
from django.http.request import RawPostDataException
from django.middleware.csrf import get_token
from django.utils.decorators import method_decorator
from django_ratelimit.decorators import ratelimit
from rest_framework import permissions, status
from rest_framework.exceptions import AuthenticationFailed, ValidationError
from rest_framework.response import Response
from rest_framework.views import APIView
from rest_framework_simplejwt.exceptions import TokenError
from rest_framework_simplejwt.serializers import TokenRefreshSerializer
from rest_framework_simplejwt.tokens import RefreshToken

from .authentication import (
    clear_auth_cookies,
    enforce_csrf,
    refresh_cookie_name,
    set_auth_cookies,
)

logger = logging.getLogger(__name__)


def _identifier_key(group, request):
    """Rate-limit key: the email or index number in the body or query string (else the IP)."""
    value = ""
    try:
        body = json.loads(request.body or b"{}")
        if isinstance(body, dict):
            for field in ("email", "index_number"):
                v = body.get(field)
                if isinstance(v, str) and v.strip():
                    value = f"{field}:{v.strip().lower()}"
                    break
    except (ValueError, AttributeError, RawPostDataException):
        pass
    if not value:
        v = request.GET.get("email", "").strip().lower()
        value = f"email:{v}" if v else ""
    return value or request.META.get("REMOTE_ADDR", "unknown")


def rate_limited(ip: str, email: str | None = None, methods=("POST",)):
    """Class decorator for django-ratelimit (block=False; views check `blocked(request)`)."""

    def wrap(cls):
        m = list(methods)
        if email:
            cls = method_decorator(
                ratelimit(group=f"{cls.__name__}:id", key=_identifier_key, rate=email, method=m, block=False),
                name="dispatch",
            )(cls)
        cls = method_decorator(
            ratelimit(group=f"{cls.__name__}:ip", key="ip", rate=ip, method=m, block=False),
            name="dispatch",
        )(cls)
        return cls

    return wrap


def blocked(request):
    if getattr(request, "limited", False):
        return Response(
            {"error": "Too many requests. Please try again later."},
            status=status.HTTP_429_TOO_MANY_REQUESTS,
        )
    return None


class PublicMixin:
    """No authentication at all, so a stale cookie can't break login, refresh or logout."""

    permission_classes = [permissions.AllowAny]
    authentication_classes = []


def base_url(request) -> str:
    configured = getattr(settings, "BACKEND_BASE_URL", None)
    if configured:
        return configured.rstrip("/")
    return f"{request.scheme}://{request.get_host()}"


def tokens_in_body() -> bool:
    # True during the frontend migration; set False once all three apps use cookies.
    return getattr(settings, "AUTH_RETURN_TOKENS_IN_BODY", True)


def _blacklist_refresh(raw: str) -> None:
    try:
        token = RefreshToken(raw)
        if hasattr(token, "blacklist"):
            token.blacklist()
    except TokenError:
        pass


class CSRFTokenView(PublicMixin, APIView):
    """Frontends call this on load and send the value back as X-CSRFToken."""

    def get(self, request):
        return Response({"csrfToken": get_token(request)}, headers={"Cache-Control": "no-store"})


@rate_limited(ip="120/h")
class CookieTokenRefreshView(PublicMixin, APIView):
    def post(self, request):
        if (r := blocked(request)):
            return r

        raw = request.data.get("refresh") or request.data.get("refresh_token")
        if not raw:
            raw = request.COOKIES.get(refresh_cookie_name(request))
            if raw:
                enforce_csrf(request)  # cookie-driven request, so it needs the CSRF token

        if not raw:
            return Response({"error": "Refresh token missing."}, status=status.HTTP_401_UNAUTHORIZED)

        serializer = TokenRefreshSerializer(data={"refresh": raw})
        try:
            serializer.is_valid(raise_exception=True)
        except (TokenError, AuthenticationFailed, ValidationError, ObjectDoesNotExist):
            response = Response(
                {"error": "Invalid or expired refresh token."}, status=status.HTTP_401_UNAUTHORIZED
            )
            return clear_auth_cookies(response, request)

        data = serializer.validated_data
        access, new_refresh = data["access"], data.get("refresh")

        body = {"detail": "Token refreshed."}
        if tokens_in_body():
            body["access"] = access
            if new_refresh:
                body["refresh"] = new_refresh

        return set_auth_cookies(Response(body), access, new_refresh, request=request)


class LogoutView(PublicMixin, APIView):
    """
    Always clears the cookies and blacklists the refresh token, even if the session has
    expired. CSRF is deliberately not enforced: a forged logout is a nuisance, a user who
    can't log out is worse.
    """

    def post(self, request):
        raw = (
            request.data.get("refresh")
            or request.data.get("refresh_token")
            or request.COOKIES.get(refresh_cookie_name(request))
        )
        if raw:
            _blacklist_refresh(raw)
        try:
            logout(request)
        except Exception:  # noqa: BLE001
            pass
        response = Response(
            {"success": True, "detail": "Logout successful", "message": "Logged out successfully."}
        )
        return clear_auth_cookies(response, request)