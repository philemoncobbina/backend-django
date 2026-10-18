from urllib.parse import urlsplit

from django.conf import settings
from django.middleware.csrf import CsrfViewMiddleware
from rest_framework import exceptions
from rest_framework_simplejwt.authentication import JWTAuthentication
from rest_framework_simplejwt.settings import api_settings


def portal_for_request(request):
    """
    Which frontend sent this request? Looked up from the Origin header (or the Referer's
    origin) in settings.AUTH_PORTAL_BY_ORIGIN. Unlisted origins use the default cookie names.
    """
    if request is None:
        return None
    mapping = getattr(settings, "AUTH_PORTAL_BY_ORIGIN", None) or {}
    if not mapping:
        return None
    origin = request.META.get("HTTP_ORIGIN", "")
    if not origin:
        parts = urlsplit(request.META.get("HTTP_REFERER", ""))
        origin = f"{parts.scheme}://{parts.netloc}" if parts.scheme and parts.netloc else ""
    return mapping.get(origin)


def _cookie_name(base: str, request) -> str:
    portal = portal_for_request(request)
    return f"{portal}_{base}" if portal else base


def access_cookie_name(request=None) -> str:
    return _cookie_name(getattr(settings, "AUTH_COOKIE_ACCESS", "access_token"), request)


def refresh_cookie_name(request=None) -> str:
    return _cookie_name(getattr(settings, "AUTH_COOKIE_REFRESH", "refresh_token"), request)


def _cookie_kwargs(lifetime) -> dict:
    return {
        "max_age": int(lifetime.total_seconds()),
        "httponly": True,
        "secure": getattr(settings, "AUTH_COOKIE_SECURE", not settings.DEBUG),
        "samesite": getattr(settings, "AUTH_COOKIE_SAMESITE", "Lax"),
        "domain": getattr(settings, "AUTH_COOKIE_DOMAIN", None),  # leave None: host-only cookie
        "path": getattr(settings, "AUTH_COOKIE_PATH", "/"),
    }


def set_auth_cookies(response, access=None, refresh=None, request=None):
    if access:
        response.set_cookie(
            access_cookie_name(request), access,
            **_cookie_kwargs(api_settings.ACCESS_TOKEN_LIFETIME),
        )
    if refresh:
        response.set_cookie(
            refresh_cookie_name(request), refresh,
            **_cookie_kwargs(api_settings.REFRESH_TOKEN_LIFETIME),
        )
    return response


def clear_auth_cookies(response, request=None):
    kwargs = {
        "path": getattr(settings, "AUTH_COOKIE_PATH", "/"),
        "domain": getattr(settings, "AUTH_COOKIE_DOMAIN", None),
        "samesite": getattr(settings, "AUTH_COOKIE_SAMESITE", "Lax"),
    }
    response.delete_cookie(access_cookie_name(request), **kwargs)
    response.delete_cookie(refresh_cookie_name(request), **kwargs)
    return response


class CSRFCheck(CsrfViewMiddleware):
    def _reject(self, request, reason):
        return reason  # return the reason instead of an HTTP response


def enforce_csrf(request):
    """Same check DRF's SessionAuthentication performs. Safe methods are skipped."""
    check = CSRFCheck(lambda r: None)
    check.process_request(request)
    reason = check.process_view(request, None, (), {})
    if reason:
        raise exceptions.PermissionDenied(f"CSRF Failed: {reason}")


class CookieJWTAuthentication(JWTAuthentication):
    """
    1. Authorization: Bearer <token> -> no CSRF check (browsers never add this header themselves).
    2. HttpOnly access cookie        -> CSRF check on unsafe methods.
    A bad or expired cookie is treated as anonymous so public endpoints keep working.
    """

    def authenticate(self, request):
        header = self.get_header(request)

        if header is not None:
            raw = self.get_raw_token(header)
            if raw is None:
                return None
            validated = self.get_validated_token(raw)
            return self.get_user(validated), validated

        raw = request.COOKIES.get(access_cookie_name(request))
        if not raw:
            return None

        try:
            validated = self.get_validated_token(raw)
            user = self.get_user(validated)
        except exceptions.AuthenticationFailed:
            return None

        enforce_csrf(request)
        return user, validated