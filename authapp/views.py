"""
authapp/views.py

Auth views for the website. Credentials are issued as HttpOnly cookies; the
cookie path is protected by CSRF (see authentication.py). Heavy work runs in
Celery tasks.
"""

import hmac
import json
import logging

import jwt
import requests
from django.apps import apps
from django.conf import settings
from django.contrib.auth import get_user_model, logout
from django.contrib.auth.models import update_last_login
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError as DjangoValidationError
from django.middleware.csrf import get_token
from django.shortcuts import redirect
from django.utils import timezone
from django.utils.crypto import get_random_string
from django.utils.decorators import method_decorator
from django_ratelimit.decorators import ratelimit
from rest_framework import generics, permissions, status
from rest_framework.exceptions import AuthenticationFailed, ValidationError
from rest_framework.response import Response
from rest_framework.views import APIView
from rest_framework_simplejwt.exceptions import TokenError
from rest_framework_simplejwt.serializers import TokenRefreshSerializer
from rest_framework_simplejwt.tokens import RefreshToken

from .authentication import (
    access_cookie_name,
    clear_auth_cookies,
    enforce_csrf,
    refresh_cookie_name,
    set_auth_cookies,
)
from .serializers import (
    ChangePasswordRequestSerializer,
    ChangePasswordSerializer,
    CustomUserSerializer,
    GoogleSignInSerializer,
    PasswordResetConfirmSerializer,
    PasswordResetSerializer,
    ResendVerificationEmailSerializer,
)
from .tasks import (
    send_change_password_code_task,
    send_login_alert_task,
    send_password_reset_code_task,
    send_verification_email_task,
)

User = get_user_model()
logger = logging.getLogger(__name__)

SIGNUP_FIELDS = ("email", "first_name", "last_name", "password")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _email_key(group, request):
    """Rate-limit key: the email in the JSON body or query string (falls back to the IP)."""
    email = ""
    try:
        body = json.loads(request.body or b"{}")
        if isinstance(body, dict):
            email = str(body.get("email", "")).strip().lower()
    except (ValueError, AttributeError):
        pass
    if not email:
        email = request.GET.get("email", "").strip().lower()
    return email or request.META.get("REMOTE_ADDR", "unknown")


def rate_limited(ip: str, email: str | None = None, methods=("POST",)):
    """
    Class decorator applying django-ratelimit to `dispatch` (block=False, so
    views check `request.limited`). Each view gets its own counter group.
    """

    def wrap(cls):
        m = list(methods)
        if email:
            cls = method_decorator(
                ratelimit(group=f"{cls.__name__}:email", key=_email_key, rate=email, method=m, block=False),
                name="dispatch",
            )(cls)
        cls = method_decorator(
            ratelimit(group=f"{cls.__name__}:ip", key="ip", rate=ip, method=m, block=False),
            name="dispatch",
        )(cls)
        return cls

    return wrap


def _blocked(request):
    if getattr(request, "limited", False):
        return Response(
            {"error": "Too many requests. Please try again later."},
            status=status.HTTP_429_TOO_MANY_REQUESTS,
        )
    return None


class PublicMixin:
    """
    Public endpoints skip authentication entirely, so a logged-in browser
    doesn't hit a CSRF failure when it opens a login or reset page.
    """

    permission_classes = [permissions.AllowAny]
    authentication_classes = []


def _base_url(request) -> str:
    configured = getattr(settings, "BACKEND_BASE_URL", None)
    if configured:
        return configured.rstrip("/")
    return f"{request.scheme}://{request.get_host()}"


def _frontend_login_url() -> str:
    return getattr(settings, "FRONTEND_LOGIN_URL", "https://plvcmonline.uk/login")


def _tokens_in_body() -> bool:
    # True during the frontend migration; set to False once all three apps use cookies.
    return getattr(settings, "AUTH_RETURN_TOKENS_IN_BODY", True)


def _codes_match(stored, supplied) -> bool:
    if not stored or not supplied:
        return False
    return hmac.compare_digest(str(stored), str(supplied))


def _decode_expired_verification_token(token: str) -> dict | None:
    try:
        return jwt.decode(
            token, settings.SECRET_KEY, algorithms=["HS256"], options={"verify_exp": False}
        )
    except jwt.InvalidTokenError:
        return None


def _revoke_user_tokens(user) -> None:
    """Blacklist every outstanding refresh token for the user (needs the blacklist app)."""
    if not apps.is_installed("rest_framework_simplejwt.token_blacklist"):
        return
    from rest_framework_simplejwt.token_blacklist.models import BlacklistedToken, OutstandingToken

    pending = OutstandingToken.objects.filter(user=user, blacklistedtoken__isnull=True)
    BlacklistedToken.objects.bulk_create(
        [BlacklistedToken(token=t) for t in pending], ignore_conflicts=True
    )


def _blacklist_refresh(raw: str) -> None:
    try:
        token = RefreshToken(raw)
        if hasattr(token, "blacklist"):
            token.blacklist()
    except TokenError:
        pass


def _password_errors(password, user=None):
    try:
        validate_password(password, user)
    except DjangoValidationError as exc:
        return {"new_password": list(exc.messages)}
    return None


def _auth_response(request, user, extra=None, status_code=status.HTTP_200_OK):
    """Issue tokens: HttpOnly cookies always, JSON body only while migrating."""
    refresh = RefreshToken.for_user(user)
    access = str(refresh.access_token)
    body = dict(extra or {})
    if _tokens_in_body():
        body["access_token"] = access
        body["refresh_token"] = str(refresh)
    response = Response(body, status=status_code)
    # CHANGED: pass request so the cookie is written with the same per-portal name that
    # CookieJWTAuthentication reads (this was the cause of the 401 after login).
    set_auth_cookies(response, access, str(refresh), request=request)
    return response


# ---------------------------------------------------------------------------
# CSRF token + refresh + logout
# ---------------------------------------------------------------------------

class CSRFTokenView(PublicMixin, APIView):
    """Frontends call this on app load and send the value back as X-CSRFToken."""

    def get(self, request):
        return Response(
            {"csrfToken": get_token(request)}, headers={"Cache-Control": "no-store"}
        )


@rate_limited(ip="120/h")
class CookieTokenRefreshView(PublicMixin, APIView):
    def post(self, request):
        if (r := _blocked(request)):
            return r

        raw = request.data.get("refresh") or request.data.get("refresh_token")
        if not raw:
            # CHANGED: pass request so the refresh cookie name matches the portal.
            raw = request.COOKIES.get(refresh_cookie_name(request))
            if raw:
                enforce_csrf(request)  # cookie-driven request, so it needs the CSRF token

        if not raw:
            return Response({"error": "Refresh token missing."}, status=status.HTTP_401_UNAUTHORIZED)

        serializer = TokenRefreshSerializer(data={"refresh": raw})
        try:
            serializer.is_valid(raise_exception=True)
        except (TokenError, AuthenticationFailed, ValidationError):
            response = Response(
                {"error": "Invalid or expired refresh token."}, status=status.HTTP_401_UNAUTHORIZED
            )
            # CHANGED: pass request so the right (prefixed) cookies are cleared.
            return clear_auth_cookies(response, request)

        data = serializer.validated_data
        access, new_refresh = data["access"], data.get("refresh")

        body = {"detail": "Token refreshed."}
        if _tokens_in_body():
            body["access"] = access
            if new_refresh:
                body["refresh"] = new_refresh

        response = Response(body)
        # CHANGED: pass request so the refreshed cookies keep the portal-prefixed names.
        return set_auth_cookies(response, access, new_refresh, request=request)


class LogoutView(PublicMixin, APIView):
    """
    Always clears the cookies and blacklists the refresh token. CSRF is deliberately
    not enforced here: a forged logout is a nuisance, but a user who can't log out
    (lost CSRF token) is worse.
    """

    def post(self, request):
        raw = (
            request.data.get("refresh")
            or request.data.get("refresh_token")
            # CHANGED: pass request so the portal's refresh cookie is found.
            or request.COOKIES.get(refresh_cookie_name(request))
        )
        if raw:
            _blacklist_refresh(raw)
        try:
            logout(request)  # clears any leftover Django session
        except Exception:  # noqa: BLE001
            pass
        # CHANGED: pass request so the portal's cookies are the ones deleted.
        return clear_auth_cookies(Response({"detail": "Logout successful"}), request)


# ---------------------------------------------------------------------------
# Account
# ---------------------------------------------------------------------------

class UserDetailView(generics.RetrieveAPIView):
    permission_classes = [permissions.IsAuthenticated]
    serializer_class = CustomUserSerializer

    def get_object(self):
        return self.request.user


@rate_limited(ip="5/h")
class SignUpView(PublicMixin, generics.CreateAPIView):
    serializer_class = CustomUserSerializer

    def create(self, request, *args, **kwargs):
        if (r := _blocked(request)):
            return r

        # Whitelist fields: the serializer also exposes class_name, index_number, is_blocked...
        data = {k: request.data.get(k) for k in SIGNUP_FIELDS if k in request.data}
        email = (data.get("email") or "").strip()
        password = data.get("password")

        if email and User.objects.filter(email__iexact=email).exists():
            logger.info("Signup rejected: email already exists")
            return Response(
                {"error": "Email has already been used."}, status=status.HTTP_400_BAD_REQUEST
            )

        if not password:
            return Response(
                {"password": ["This field is required."]}, status=status.HTTP_400_BAD_REQUEST
            )
        errors = _password_errors(password)
        if errors:
            return Response({"password": errors["new_password"]}, status=status.HTTP_400_BAD_REQUEST)

        data["is_active"] = False  # inactive until the email is verified

        serializer = self.get_serializer(data=data)
        serializer.is_valid(raise_exception=True)
        self.perform_create(serializer)
        user = serializer.instance

        send_verification_email_task.delay(user.id, _base_url(request))
        logger.info("Verification email task enqueued for user id=%s", user.id)

        body = dict(serializer.data)
        body["message"] = (
            "User registration successful. Please check your email for the verification link."
        )
        return Response(body, status=status.HTTP_201_CREATED)


@rate_limited(ip="60/h", methods=("GET",))
class VerifyEmailView(PublicMixin, APIView):
    """The URL carries only the signed token; user_id comes from the token payload."""

    def get(self, request, token):
        if (r := _blocked(request)):
            return r

        try:
            payload = jwt.decode(token, settings.SECRET_KEY, algorithms=["HS256"])
            user_id = payload.get("user_id")

            if payload.get("token_type") != "email_verification" or not user_id:
                return Response(
                    {"error": "Invalid verification token."}, status=status.HTTP_400_BAD_REQUEST
                )

            user = User.objects.filter(id=user_id).first()
            if not user:
                return Response({"error": "Account not found."}, status=status.HTTP_404_NOT_FOUND)

            if not user.is_active:
                user.is_active = True
                user.save(update_fields=["is_active"])
                logger.info("Email verified for user id=%s", user_id)

            return redirect(_frontend_login_url())

        except jwt.ExpiredSignatureError:
            expired = _decode_expired_verification_token(token)
            if not expired:
                return Response(
                    {"error": "Activation link has expired and could not be renewed."},
                    status=status.HTTP_400_BAD_REQUEST,
                )

            user_id = expired.get("user_id")
            if expired.get("token_type") != "email_verification" or not user_id:
                return Response(
                    {"error": "Activation link has expired and is invalid."},
                    status=status.HTTP_400_BAD_REQUEST,
                )

            user = User.objects.filter(id=user_id).first()
            if not user:
                return Response(
                    {"error": "Activation link has expired and the user account was not found."},
                    status=status.HTTP_404_NOT_FOUND,
                )
            if user.is_active:
                return redirect(_frontend_login_url())

            send_verification_email_task.delay(user.id, _base_url(request))
            return Response(
                {
                    "error": "Activation link has expired.",
                    "message": "A new verification link has been sent to your email.",
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        except jwt.InvalidTokenError:
            return Response(
                {"error": "Invalid activation link."}, status=status.HTTP_400_BAD_REQUEST
            )


@rate_limited(ip="60/h", email="3/h")
class ResendVerificationEmailView(PublicMixin, APIView):
    serializer_class = ResendVerificationEmailSerializer

    def post(self, request, *args, **kwargs):
        if (r := _blocked(request)):
            return r

        serializer = self.serializer_class(data=request.data)
        if not serializer.is_valid():
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

        email = serializer.validated_data["email"]
        user = User.objects.filter(email=email).first()

        if not user:
            return Response(
                {"error": "No account found with this email address."},
                status=status.HTTP_404_NOT_FOUND,
            )
        if user.is_active:
            return Response(
                {"message": "This account has already been verified. Please login."},
                status=status.HTTP_200_OK,
            )

        send_verification_email_task.delay(user.id, _base_url(request))
        return Response(
            {"message": "A new verification link has been sent to your email."},
            status=status.HTTP_200_OK,
        )


# ---------------------------------------------------------------------------
# Login
# ---------------------------------------------------------------------------

@rate_limited(ip="60/10m", email="10/15m")
class LoginView(PublicMixin, APIView):
    def post(self, request):
        if (r := _blocked(request)):
            return r

        email = (request.data.get("email") or "").strip()
        password = request.data.get("password") or ""
        bad_credentials = Response(
            {"error": "Incorrect username or password."}, status=status.HTTP_401_UNAUTHORIZED
        )

        user = User.objects.filter(email=email).first()
        if not user:
            make_password_burn = User().set_password  # equalise timing with the real path
            make_password_burn(password)
            return bad_credentials

        if user.is_google_account:
            return Response(
                {"error": "Your account was created with Google. Please login with your Google account"},
                status=status.HTTP_403_FORBIDDEN,
            )

        # Verify the password BEFORE revealing anything about the account's state or role.
        if not user.check_password(password):
            return bad_credentials

        if user.is_blocked:
            return Response(
                {"error": "Your account has been blocked."}, status=status.HTTP_403_FORBIDDEN
            )
        if not user.is_active:
            return Response(
                {"error": "Account not verified."}, status=status.HTTP_401_UNAUTHORIZED
            )
        if user.role in ["principal", "staff"]:
            return Response(
                {"error": "You do not have access to this system. Please use the staff portal."},
                status=status.HTTP_403_FORBIDDEN,
            )
        if user.role == "student":
            return Response(
                {"error": "Please login through the student portal."},
                status=status.HTTP_403_FORBIDDEN,
            )

        update_last_login(None, user)
        send_login_alert_task.delay(user.id, _base_url(request))

        return _auth_response(request, user, extra={"user": CustomUserSerializer(user).data})


# ---------------------------------------------------------------------------
# Google sign-in
# ---------------------------------------------------------------------------

@rate_limited(ip="60/h")
class GoogleSignInView(PublicMixin, APIView):
    def post(self, request):
        if (r := _blocked(request)):
            return r

        serializer = GoogleSignInSerializer(data=request.data)
        if not serializer.is_valid():
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

        access_token = serializer.validated_data["access_token"]
        invalid = Response(
            {"success": False, "error": "Invalid or expired token"},
            status=status.HTTP_400_BAD_REQUEST,
        )

        try:
            info = self._get_token_info(access_token)
            if not info:
                return invalid

            # The token must have been issued to OUR Google client, not to any other app.
            if not self._audience_ok(info):
                logger.warning("Google token rejected: audience mismatch")
                return invalid

            if str(info.get("email_verified", "")).lower() != "true":
                return Response(
                    {"success": False, "error": "Google email is not verified"},
                    status=status.HTTP_400_BAD_REQUEST,
                )

            email = info.get("email")
            if not email:
                return Response(
                    {"success": False, "error": "Email not found in token"},
                    status=status.HTTP_400_BAD_REQUEST,
                )

            user, created = User.objects.get_or_create(
                email=email,
                defaults={
                    "is_google_account": True,
                    "is_active": True,
                    "is_blocked": False,
                    "date_joined": timezone.now(),
                },
            )
            if created:
                logger.info("New Google user created id=%s", user.id)

            # All account checks happen BEFORE we change anything on the record.
            if not user.is_google_account:
                return Response(
                    {
                        "success": False,
                        "error": "Account was not created with Gmail. Please login with your email and password",
                    },
                    status=status.HTTP_403_FORBIDDEN,
                )
            if user.is_blocked:
                return Response(
                    {"success": False, "error": "User account is blocked. Please contact support."},
                    status=status.HTTP_403_FORBIDDEN,
                )
            if not user.is_active:
                return Response(
                    {
                        "success": False,
                        "error": "User account is inactive. Please verify your email or contact support.",
                    },
                    status=status.HTTP_403_FORBIDDEN,
                )

            profile = self._get_user_profile(access_token)
            first_name = (
                profile.get("given_name")
                or profile.get("first_name")
                or (profile.get("names") or [{}])[0].get("givenName", "")
                or ""
            )
            last_name = (
                profile.get("family_name")
                or profile.get("last_name")
                or (profile.get("names") or [{}])[0].get("familyName", "")
                or ""
            )
            if first_name:
                user.first_name = first_name
            if last_name:
                user.last_name = last_name
            user.last_login = timezone.now()
            user.save(update_fields=["first_name", "last_name", "last_login"])

            return self._respond(request, user, email, created)

        except Exception:  # noqa: BLE001
            logger.exception("Unexpected error during Google sign-in")
            return Response(
                {"success": False, "error": "Google sign-in failed. Please try again."},
                status=status.HTTP_400_BAD_REQUEST,
            )

    @staticmethod
    def _respond(request, user, email, created):
        extra = {
            "success": True,
            "email": email,
            "created": created,
            "first_name": user.first_name,
            "last_name": user.last_name,
        }
        refresh = RefreshToken.for_user(user)
        access = str(refresh.access_token)
        if _tokens_in_body():
            extra["access"] = access
            extra["refresh"] = str(refresh)
        response = Response(extra)
        # CHANGED: pass request so the cookie name matches what the authenticator reads.
        return set_auth_cookies(response, access, str(refresh), request=request)

    @staticmethod
    def _audience_ok(info: dict) -> bool:
        allowed = set(getattr(settings, "GOOGLE_OAUTH_CLIENT_IDS", []) or [])
        if not allowed:
            logger.error("GOOGLE_OAUTH_CLIENT_IDS is not set; refusing Google sign-in.")
            return False
        return info.get("aud") in allowed or info.get("azp") in allowed

    @staticmethod
    def _get_token_info(access_token: str) -> dict | None:
        try:
            response = requests.get(
                "https://www.googleapis.com/oauth2/v3/tokeninfo",
                params={"access_token": access_token},
                timeout=5,
            )
            return response.json() if response.status_code == 200 else None
        except Exception as exc:  # noqa: BLE001
            logger.error("Error fetching Google token info: %s", exc)
            return None

    @staticmethod
    def _get_user_profile(access_token: str) -> dict:
        for url in (
            "https://www.googleapis.com/oauth2/v3/userinfo",
            "https://people.googleapis.com/v1/people/me?personFields=names,emailAddresses",
        ):
            try:
                response = requests.get(
                    url, headers={"Authorization": f"Bearer {access_token}"}, timeout=5
                )
                if response.status_code == 200:
                    return response.json()
            except Exception as exc:  # noqa: BLE001
                logger.error("Google profile endpoint error: %s", exc)
        return {}


# ---------------------------------------------------------------------------
# Password reset (one base class; three portals)
# ---------------------------------------------------------------------------

class _PasswordResetBase(PublicMixin, APIView):
    serializer_class = PasswordResetSerializer
    portal = ""
    get_error = ""
    wrong_portal_error = ""

    def role_ok(self, role) -> bool:
        raise NotImplementedError

    def get(self, request, *args, **kwargs):
        if (r := _blocked(request)):
            return r

        email = request.query_params.get("email")
        if not email:
            return Response(
                {"error": "Email parameter is missing."}, status=status.HTTP_400_BAD_REQUEST
            )

        user = User.objects.filter(email=email).first()
        if user and user.is_active and self.role_ok(user.role):
            return Response({"message": "Email is registered."}, status=status.HTTP_200_OK)
        return Response({"error": self.get_error}, status=status.HTTP_400_BAD_REQUEST)

    def post(self, request, *args, **kwargs):
        if (r := _blocked(request)):
            return r

        serializer = self.serializer_class(data=request.data)
        if not serializer.is_valid():
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

        email = serializer.validated_data["email"]
        user = User.objects.filter(email=email).first()

        if not user or not user.is_active:
            return Response(
                {"error": "Email not registered or not active."}, status=status.HTTP_400_BAD_REQUEST
            )
        if not self.role_ok(user.role):
            return Response({"error": self.wrong_portal_error}, status=status.HTTP_403_FORBIDDEN)

        code = get_random_string(length=6, allowed_chars="0123456789")
        user.verification_code = code
        user.save(update_fields=["verification_code"])

        send_password_reset_code_task.delay(email, code)
        logger.info("[%s] Password reset code task enqueued for user id=%s", self.portal, user.id)
        return Response(
            {"message": "Verification code sent to your email."}, status=status.HTTP_200_OK
        )


@rate_limited(ip="120/h", email="6/h", methods=("GET", "POST"))
class StudentPasswordResetView(_PasswordResetBase):
    portal = "Student"
    get_error = "Email not registered, not active, or does not belong to a student account."
    wrong_portal_error = (
        "This endpoint is only for student accounts. Please use the correct portal."
    )

    def role_ok(self, role):
        return role == "student"


@rate_limited(ip="120/h", email="6/h", methods=("GET", "POST"))
class AdminPasswordResetView(_PasswordResetBase):
    portal = "Admin"
    get_error = "Email not registered, not active, or does not belong to an admin account."
    wrong_portal_error = (
        "This endpoint is only for admin accounts (principal or staff). Please use the correct portal."
    )

    def role_ok(self, role):
        return role in ["principal", "staff"]


@rate_limited(ip="120/h", email="6/h", methods=("GET", "POST"))
class WebsitePasswordResetView(_PasswordResetBase):
    portal = "Website"
    get_error = "Email not registered, not active, or must use a dedicated portal."
    wrong_portal_error = "Please use your designated portal to reset your password."

    def role_ok(self, role):
        return role not in ["principal", "staff", "student"]


@rate_limited(ip="120/h", email="10/h")
class PasswordResetConfirmView(PublicMixin, APIView):
    serializer_class = PasswordResetConfirmSerializer

    def post(self, request, *args, **kwargs):
        if (r := _blocked(request)):
            return r

        serializer = self.serializer_class(data=request.data)
        if not serializer.is_valid():
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

        email = serializer.validated_data["email"]
        code = serializer.validated_data["verification_code"]
        new_password = serializer.validated_data["new_password"]

        user = User.objects.filter(email=email).first()
        if not user or not _codes_match(user.verification_code, code):
            return Response(
                {"error": "Invalid verification code."}, status=status.HTTP_400_BAD_REQUEST
            )

        errors = _password_errors(new_password, user)
        if errors:
            # Keep "new_password" for field-level display, add "error" so frontends
            # that only read `error` still show a readable message.
            return Response(
                {"error": " ".join(errors["new_password"]), **errors},
                status=status.HTTP_400_BAD_REQUEST,
            )

        user.set_password(new_password)
        user.verification_code = None
        user.save(update_fields=["password", "verification_code"])
        _revoke_user_tokens(user)  # a reset ends every existing session

        logger.info("Password reset for user id=%s", user.id)
        return Response({"message": "Password reset successful."}, status=status.HTTP_200_OK)

@rate_limited(ip="120/h", email="10/h")
class VerifyResetCodeView(PublicMixin, APIView):
    def post(self, request, *args, **kwargs):
        if (r := _blocked(request)):
            return r

        email = request.data.get("email")
        code = request.data.get("verification_code")
        user = User.objects.filter(email=email).first() if email else None

        if user and _codes_match(user.verification_code, code):
            return Response({"message": "Verification code is valid."}, status=status.HTTP_200_OK)
        return Response({"error": "Invalid verification code."}, status=status.HTTP_400_BAD_REQUEST)


# ---------------------------------------------------------------------------
# Change password (authenticated)
# ---------------------------------------------------------------------------

@rate_limited(ip="30/h")
class ChangePasswordRequestView(APIView):
    permission_classes = [permissions.IsAuthenticated]
    serializer_class = ChangePasswordRequestSerializer

    def post(self, request, *args, **kwargs):
        if (r := _blocked(request)):
            return r

        serializer = self.serializer_class(data=request.data, context={"request": request})
        if not serializer.is_valid():
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

        user = request.user
        if not user.email or not user.is_active:
            return Response(
                {"error": "User account is not active or email is missing."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        code = get_random_string(length=6, allowed_chars="0123456789")
        user.verification_code = code
        user.save(update_fields=["verification_code"])

        send_change_password_code_task.delay(user.email, code)
        return Response(
            {"message": "Verification code sent to your email."}, status=status.HTTP_200_OK
        )


@rate_limited(ip="30/h")
class ChangePasswordView(APIView):
    permission_classes = [permissions.IsAuthenticated]
    serializer_class = ChangePasswordSerializer

    def post(self, request, *args, **kwargs):
        if (r := _blocked(request)):
            return r

        serializer = self.serializer_class(data=request.data, context={"request": request})
        if not serializer.is_valid():
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

        user = request.user
        code = serializer.validated_data["verification_code"]
        old_password = serializer.validated_data["old_password"]
        new_password = serializer.validated_data["new_password"]

        if not _codes_match(user.verification_code, code):
            return Response(
                {"error": "Invalid verification code."}, status=status.HTTP_400_BAD_REQUEST
            )
        if not user.check_password(old_password):
            return Response(
                {"error": "Current password is incorrect. Please enter your correct current password."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        if old_password == new_password:
            return Response(
                {"error": "New password must be different from your current password."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        errors = _password_errors(new_password, user)
        if errors:
            return Response(errors, status=status.HTTP_400_BAD_REQUEST)

        user.set_password(new_password)
        user.verification_code = None
        user.save(update_fields=["password", "verification_code"])
        _revoke_user_tokens(user)  # other devices must log in again

        logger.info("Password changed for user id=%s", user.id)
        response = Response(
            {"message": "Password changed successfully."}, status=status.HTTP_200_OK
        )
        # CHANGED: pass request so this portal's cookies are the ones cleared.
        return clear_auth_cookies(response, request)  # this device logs in again too


@rate_limited(ip="60/h")
class VerifyChangePasswordCodeView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request, *args, **kwargs):
        if (r := _blocked(request)):
            return r

        code = request.data.get("verification_code")
        if not code:
            return Response(
                {"error": "Verification code is required."}, status=status.HTTP_400_BAD_REQUEST
            )

        if _codes_match(request.user.verification_code, code):
            return Response({"message": "Verification code is valid."}, status=status.HTTP_200_OK)
        return Response(
            {"error": "Invalid verification code. Please check your email and try again."},
            status=status.HTTP_400_BAD_REQUEST,
        )