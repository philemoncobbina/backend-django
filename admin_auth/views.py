"""
Admin app views: auth + user management.

Credentials are issued as HttpOnly cookies (named per portal, see authapp/authentication.py)
and the cookie path is protected by CSRF. Email sending runs in Celery.
"""

import json
import logging

import jwt
from django.apps import apps
from django.conf import settings
from django.contrib.auth import get_user_model, logout
from django.contrib.auth.hashers import make_password
from django.contrib.auth.models import update_last_login
from django.db import transaction
from django.middleware.csrf import get_token
from django.shortcuts import redirect
from django.utils import timezone
from django.utils.decorators import method_decorator
from django_ratelimit.decorators import ratelimit
from rest_framework import generics, permissions, status
from rest_framework.exceptions import AuthenticationFailed, ValidationError
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView
from rest_framework_simplejwt.exceptions import TokenError
from rest_framework_simplejwt.serializers import TokenRefreshSerializer
from rest_framework_simplejwt.tokens import RefreshToken

from authapp.authentication import (
    clear_auth_cookies,
    enforce_csrf,
    refresh_cookie_name,
    set_auth_cookies,
)
from authapp.models import CustomUser

from .models import UserChangeLog
from .permissions import IsPrincipalOrSuperuser
from .serializers import (
    AdminUserSerializer,
    UserChangeLogSerializer,
    create_user_change_log,
)
from .tasks import send_admin_verification_email_task

User = get_user_model()
logger = logging.getLogger(__name__)

ADMIN_ROLES = ("principal", "staff")
PRIVILEGE_FIELDS = ("is_superuser", "is_staff", "groups", "user_permissions")


# ---------------------------------------------------------------------------
# Helpers (same as the website app; move to a shared module when convenient)
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
    """No authentication at all, so a stale cookie can't break login, refresh or logout."""

    permission_classes = [permissions.AllowAny]
    authentication_classes = []


def _base_url(request) -> str:
    configured = getattr(settings, "BACKEND_BASE_URL", None)
    if configured:
        return configured.rstrip("/")
    return f"{request.scheme}://{request.get_host()}"


def _tokens_in_body() -> bool:
    # True during the frontend migration; set False once the admin frontend uses cookies.
    return getattr(settings, "AUTH_RETURN_TOKENS_IN_BODY", True)


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


# ---------------------------------------------------------------------------
# CSRF token, refresh, logout
# ---------------------------------------------------------------------------

class CSRFTokenView(PublicMixin, APIView):
    """The admin frontend calls this on load and sends the value back as X-CSRFToken."""

    def get(self, request):
        return Response({"csrfToken": get_token(request)}, headers={"Cache-Control": "no-store"})


@rate_limited(ip="120/h")
class CookieTokenRefreshView(PublicMixin, APIView):
    def post(self, request):
        if (r := _blocked(request)):
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
        except (TokenError, AuthenticationFailed, ValidationError):
            response = Response(
                {"error": "Invalid or expired refresh token."}, status=status.HTTP_401_UNAUTHORIZED
            )
            return clear_auth_cookies(response, request)

        data = serializer.validated_data
        access, new_refresh = data["access"], data.get("refresh")

        body = {"detail": "Token refreshed."}
        if _tokens_in_body():
            body["access"] = access
            if new_refresh:
                body["refresh"] = new_refresh

        return set_auth_cookies(Response(body), access, new_refresh, request=request)


class LogoutView(PublicMixin, APIView):
    """
    Always clears the cookies and blacklists the refresh token, even when the session has
    already expired. CSRF is deliberately not enforced: a forged logout is a nuisance, a
    user who can't log out is worse.
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
        response = Response({"success": True, "message": "Logged out successfully."})
        return clear_auth_cookies(response, request)


# ---------------------------------------------------------------------------
# Current user
# ---------------------------------------------------------------------------

class UserDetailView(generics.RetrieveAPIView):
    permission_classes = [permissions.IsAuthenticated]
    serializer_class = AdminUserSerializer

    def get_object(self):
        return self.request.user

    def get(self, request, *args, **kwargs):
        user = self.get_object()
        user_data = dict(self.get_serializer(user).data)
        user_data["role"] = user.role
        return Response(user_data, status=status.HTTP_200_OK)


class SessionCheckView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request, *args, **kwargs):
        user = request.user
        response = Response(
            {
                "authenticated": True,
                "user": {"id": user.id, "email": user.email, "role": user.role},
            },
            status=status.HTTP_200_OK,
        )
        # Was "private, max-age=300": a blocked or logged-out user still looked
        # authenticated for up to five minutes.
        response["Cache-Control"] = "private, no-store"
        return response


# ---------------------------------------------------------------------------
# User management
# ---------------------------------------------------------------------------

class AdminUserManagementView(APIView):
    permission_classes = [IsPrincipalOrSuperuser]

    def _can_modify_users(self, request):
        return request.user.is_superuser or request.user.role == "principal"

    def _guard_target(self, request, user):
        """Only a superuser may touch a superuser account."""
        if user.is_superuser and not request.user.is_superuser:
            logger.warning(
                "Principal %s attempted to modify superuser id=%s", request.user.email, user.id
            )
            return Response(
                {"error": "Only a superuser can modify a superuser account."},
                status=status.HTTP_403_FORBIDDEN,
            )
        return None

    def get(self, request, *args, **kwargs):
        users = CustomUser.objects.prefetch_related("guardians").all()
        serializer = AdminUserSerializer(users, many=True, context={"request": request})
        return Response(serializer.data, status=status.HTTP_200_OK)

    @transaction.atomic
    def patch(self, request, user_id, *args, **kwargs):
        if not self._can_modify_users(request):
            logger.warning(
                "Unauthorized PATCH attempt by %s with role %s",
                request.user.email,
                request.user.role,
            )
            return Response(
                {"error": "Permission denied. Only principals or superusers can modify users."},
                status=status.HTTP_403_FORBIDDEN,
            )

        try:
            user = CustomUser.objects.select_for_update().get(id=user_id)
        except CustomUser.DoesNotExist:
            logger.warning("User with ID %s was not found for update", user_id)
            return Response({"error": "User not found."}, status=status.HTTP_404_NOT_FOUND)

        if (denied := self._guard_target(request, user)):
            return denied

        action = request.data.get("action")

        if action == "block":
            if user.pk == request.user.pk:
                return Response(
                    {"error": "You cannot block your own account."},
                    status=status.HTTP_400_BAD_REQUEST,
                )
            if user.is_blocked and not user.is_active:
                return Response(
                    {"message": f"User {user.email} is already blocked."},
                    status=status.HTTP_200_OK,
                )

            previous_value = {"is_blocked": user.is_blocked, "is_active": user.is_active}
            user.is_blocked = True
            user.is_active = False
            user.save(update_fields=["is_blocked", "is_active"])
            _revoke_user_tokens(user)

            create_user_change_log(
                target_user=user,
                changed_by=request.user,
                action=UserChangeLog.Action.BLOCK,
                field_name="account_status",
                previous_value=previous_value,
                new_value={"is_blocked": True, "is_active": False},
                request=request,
            )
            logger.info("User %s blocked by %s", user.email, request.user.email)
            return Response(
                {"message": f"User {user.email} has been blocked."}, status=status.HTTP_200_OK
            )

        if action == "unblock":
            if not user.is_blocked and user.is_active:
                return Response(
                    {"message": f"User {user.email} is already unblocked."},
                    status=status.HTTP_200_OK,
                )

            previous_value = {"is_blocked": user.is_blocked, "is_active": user.is_active}
            user.is_blocked = False
            user.is_active = True
            user.save(update_fields=["is_blocked", "is_active"])

            create_user_change_log(
                target_user=user,
                changed_by=request.user,
                action=UserChangeLog.Action.UNBLOCK,
                field_name="account_status",
                previous_value=previous_value,
                new_value={"is_blocked": False, "is_active": True},
                request=request,
            )
            logger.info("User %s unblocked by %s", user.email, request.user.email)
            return Response(
                {"message": f"User {user.email} has been unblocked."}, status=status.HTTP_200_OK
            )

        if action == "activate":
            if user.is_active:
                return Response(
                    {"message": f"User {user.email} is already active."}, status=status.HTTP_200_OK
                )

            previous_value = user.is_active
            user.is_active = True
            user.save(update_fields=["is_active"])

            create_user_change_log(
                target_user=user,
                changed_by=request.user,
                action=UserChangeLog.Action.ACTIVATE,
                field_name="is_active",
                previous_value=previous_value,
                new_value=True,
                request=request,
            )
            logger.info("User %s activated by %s", user.email, request.user.email)
            return Response(
                {"message": f"User {user.email} has been activated."}, status=status.HTTP_200_OK
            )

        if action == "deactivate":
            if not user.is_active:
                return Response(
                    {"message": f"User {user.email} is already inactive."},
                    status=status.HTTP_200_OK,
                )
            if user.pk == request.user.pk:
                return Response(
                    {
                        "error": "You cannot deactivate your own account through this "
                        "administrative endpoint."
                    },
                    status=status.HTTP_400_BAD_REQUEST,
                )

            previous_value = user.is_active
            user.is_active = False
            user.save(update_fields=["is_active"])
            _revoke_user_tokens(user)

            create_user_change_log(
                target_user=user,
                changed_by=request.user,
                action=UserChangeLog.Action.DEACTIVATE,
                field_name="is_active",
                previous_value=previous_value,
                new_value=False,
                request=request,
            )
            logger.info("User %s deactivated by %s", user.email, request.user.email)
            return Response(
                {"message": f"User {user.email} has been deactivated."}, status=status.HTTP_200_OK
            )

        if action == "edit":
            old_class = user.class_name

            mutable_data = request.data.copy()
            mutable_data.pop("action", None)
            if not request.user.is_superuser:
                for field in PRIVILEGE_FIELDS:  # a principal can't grant superuser rights
                    mutable_data.pop(field, None)

            serializer = AdminUserSerializer(
                user, data=mutable_data, partial=True, context={"request": request}
            )
            serializer.is_valid(raise_exception=True)
            updated_user = serializer.save()

            new_class = updated_user.class_name
            if new_class and new_class != old_class and updated_user.is_student:
                self._handle_class_change(updated_user, old_class, new_class)

            logger.info("User %s updated by %s", updated_user.email, request.user.email)

            response_serializer = AdminUserSerializer(updated_user, context={"request": request})
            return Response(
                {"message": "User details updated successfully.", "user": response_serializer.data},
                status=status.HTTP_200_OK,
            )

        logger.warning(
            "Unknown user-management action '%s' submitted by %s", action, request.user.email
        )
        return Response(
            {
                "error": "Invalid action. Supported actions are block, unblock, "
                "activate, deactivate, and edit."
            },
            status=status.HTTP_400_BAD_REQUEST,
        )

    def _handle_class_change(self, user, old_class, new_class):
        """Persist the previous class when a student's class changes."""
        StudentClassHistory = apps.get_model("booklist", "StudentClassHistory")
        current_year = self._get_current_academic_year(user)

        if old_class != new_class:
            StudentClassHistory.objects.create(
                student=user,
                academic_year=current_year,
                class_name=old_class or "",
            )
            logger.info(
                "Class history recorded for %s: %s to %s", user.email, old_class, new_class
            )

    def _get_current_academic_year(self, user):
        # StudentClassHistory has no `created_at` field, so we order by "-id" (auto-increment)
        # to get the most recent record. Switch to "-created_at" if that field is ever added.
        latest_history = user.class_history.order_by("-id").first()
        if latest_history:
            return latest_history.academic_year

        current_year = timezone.now().year
        return f"{current_year}-{current_year + 1}"

    @transaction.atomic
    def delete(self, request, user_id, *args, **kwargs):
        if not self._can_modify_users(request):
            logger.warning(
                "Unauthorized DELETE attempt by %s with role %s",
                request.user.email,
                request.user.role,
            )
            return Response(
                {"error": "Permission denied. Only principals or superusers can delete users."},
                status=status.HTTP_403_FORBIDDEN,
            )

        try:
            user = CustomUser.objects.select_for_update().get(id=user_id)
        except CustomUser.DoesNotExist:
            return Response({"error": "User not found."}, status=status.HTTP_404_NOT_FOUND)

        if (denied := self._guard_target(request, user)):
            return denied

        if user.pk == request.user.pk:
            return Response(
                {
                    "error": "You cannot delete your own account through this "
                    "administrative endpoint."
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        user_email = user.email
        user_snapshot = {
            "id": user.id,
            "email": user.email,
            "username": user.username,
            "first_name": user.first_name,
            "last_name": user.last_name,
            "role": user.role,
            "index_number": user.index_number,
            "class_name": user.class_name,
            "is_active": user.is_active,
            "is_blocked": user.is_blocked,
        }

        create_user_change_log(
            target_user=user,
            target_user_email=user_email,
            changed_by=request.user,
            action=UserChangeLog.Action.DELETE,
            field_name="account",
            previous_value=user_snapshot,
            new_value=None,
            request=request,
        )

        user.delete()
        logger.info("User %s deleted by %s", user_email, request.user.email)

        return Response(
            {"message": f"User {user_email} has been deleted successfully."},
            status=status.HTTP_200_OK,
        )


class UserChangeLogListView(generics.ListAPIView):
    """
    Read-only audit log. Principals and superusers only.

    Optional query parameters: target_user_email, changed_by_email, action, field_name
    """

    permission_classes = [IsPrincipalOrSuperuser]
    serializer_class = UserChangeLogSerializer
    http_method_names = ["get", "head", "options"]

    def get_queryset(self):
        queryset = UserChangeLog.objects.select_related("target_user", "changed_by").all()

        target_user_email = self.request.query_params.get("target_user_email")
        changed_by_email = self.request.query_params.get("changed_by_email")
        action = self.request.query_params.get("action")
        field_name = self.request.query_params.get("field_name")

        if target_user_email:
            queryset = queryset.filter(target_user_email__iexact=target_user_email.strip())

        if changed_by_email:
            queryset = queryset.filter(changed_by_email__iexact=changed_by_email.strip())

        if action:
            normalised_action = action.strip().lower()
            valid_actions = {choice.value for choice in UserChangeLog.Action}
            if normalised_action in valid_actions:
                queryset = queryset.filter(action=normalised_action)
            else:
                queryset = queryset.none()

        if field_name:
            queryset = queryset.filter(field_name__icontains=field_name.strip())

        return queryset


# ---------------------------------------------------------------------------
# Admin account creation and email verification
# ---------------------------------------------------------------------------

@rate_limited(ip="30/h")
class AdminSignUpView(generics.CreateAPIView):
    permission_classes = [IsPrincipalOrSuperuser]
    serializer_class = AdminUserSerializer

    def create(self, request, *args, **kwargs):
        if (r := _blocked(request)):
            return r

        if not (request.user.is_superuser or request.user.role == "principal"):
            return Response(
                {"error": "Only principals or superusers can create users."},
                status=status.HTTP_403_FORBIDDEN,
            )

        mutable_data = request.data.copy()
        email = mutable_data.get("email")
        role = mutable_data.get("role", "staff")

        if role not in ADMIN_ROLES:
            return Response({"error": "Invalid role provided."}, status=status.HTTP_400_BAD_REQUEST)

        if not email or not isinstance(email, str) or not email.strip():
            return Response(
                {"email": ["This field is required."]}, status=status.HTTP_400_BAD_REQUEST
            )

        normalised_email = email.strip()
        if CustomUser.objects.filter(email__iexact=normalised_email).exists():
            return Response(
                {"error": "Email has already been used."}, status=status.HTTP_400_BAD_REQUEST
            )

        mutable_data["email"] = normalised_email
        if not request.user.is_superuser:
            for field in PRIVILEGE_FIELDS:
                mutable_data.pop(field, None)

        serializer = self.get_serializer(data=mutable_data, context={"request": request})
        serializer.is_valid(raise_exception=True)
        user = serializer.save()

        base_url = _base_url(request)
        transaction.on_commit(lambda: send_admin_verification_email_task.delay(user.id, base_url))
        logger.info("Admin verification email task queued for user id=%s", user.id)

        headers = self.get_success_headers(serializer.data)
        return Response(
            {
                "message": "User registration successful. Please check the email "
                "address for the verification link."
            },
            status=status.HTTP_201_CREATED,
            headers=headers,
        )


@rate_limited(ip="60/h", methods=("GET",))
class VerifyEmailView(PublicMixin, APIView):
    """Only accepts the dedicated email-verification token (see tasks.py), never a session token."""

    def get(self, request, token):
        if (r := _blocked(request)):
            return r

        invalid = Response({"error": "Invalid activation link."}, status=status.HTTP_400_BAD_REQUEST)

        try:
            payload = jwt.decode(token, settings.SECRET_KEY, algorithms=["HS256"])
        except jwt.ExpiredSignatureError:
            return Response(
                {
                    "error": "Activation link has expired.",
                    "message": "Ask a principal to activate your account or send a new invitation.",
                },
                status=status.HTTP_400_BAD_REQUEST,
            )
        except jwt.InvalidTokenError:
            logger.warning("Invalid activation link.")
            return invalid

        user_id = payload.get("user_id")
        if payload.get("token_type") != "email_verification" or not user_id:
            return invalid

        with transaction.atomic():
            user = User.objects.select_for_update().filter(id=user_id).first()
            if not user:
                return Response({"error": "Account not found."}, status=status.HTTP_404_NOT_FOUND)

            if user.role not in ADMIN_ROLES:
                return invalid  # a website or student token can't be used on this endpoint

            if user.is_blocked:
                return Response(
                    {"error": "This account is blocked. Please contact support."},
                    status=status.HTTP_403_FORBIDDEN,
                )

            if not user.is_active:
                user.is_active = True
                user.save(update_fields=["is_active"])
                create_user_change_log(
                    target_user=user,
                    changed_by=user,
                    action=UserChangeLog.Action.ACTIVATE,
                    field_name="is_active",
                    previous_value=False,
                    new_value=True,
                    request=request,
                )
                logger.info("Email verified for user id=%s", user_id)

        dashboard_url = getattr(settings, "FRONTEND_DASHBOARD_URL", None)
        if dashboard_url:
            return redirect(dashboard_url)
        return Response({"message": "Email verified. You can now log in."}, status=status.HTTP_200_OK)


# ---------------------------------------------------------------------------
# Login
# ---------------------------------------------------------------------------

@rate_limited(ip="60/10m", email="8/15m")
class LoginView(PublicMixin, APIView):
    def post(self, request):
        if (r := _blocked(request)):
            return r

        email = request.data.get("email")
        password = request.data.get("password")

        if (
            not isinstance(email, str)
            or not isinstance(password, str)
            or not email.strip()
            or not password
        ):
            return Response(
                {"error": "Email and password are required."}, status=status.HTTP_400_BAD_REQUEST
            )

        bad_credentials = Response(
            {"error": "Incorrect username or password."}, status=status.HTTP_401_UNAUTHORIZED
        )

        user = CustomUser.objects.filter(email__iexact=email.strip()).first()
        if not user:
            make_password(password)  # spend the same time as a real check
            return bad_credentials

        # Verify the password BEFORE saying anything about the account's role or state.
        if not user.check_password(password):
            return bad_credentials

        if (user.role or "").lower() not in ADMIN_ROLES:
            return Response(
                {"error": "You are not authorized to access the admin system."},
                status=status.HTTP_403_FORBIDDEN,
            )
        if user.is_blocked:
            return Response(
                {"error": "Your account has been blocked. Please contact support for assistance."},
                status=status.HTTP_403_FORBIDDEN,
            )
        if not user.is_active:
            return Response(
                {
                    "error": "Account not verified. Please check your email for the "
                    "verification link."
                },
                status=status.HTTP_401_UNAUTHORIZED,
            )

        update_last_login(None, user)

        refresh = RefreshToken.for_user(user)
        access = str(refresh.access_token)

        body = {"user": {"id": user.id, "email": user.email, "role": user.role}}
        if _tokens_in_body():
            body["access_token"] = access
            body["refresh_token"] = str(refresh)

        return set_auth_cookies(Response(body, status=status.HTTP_200_OK), access, str(refresh), request=request)