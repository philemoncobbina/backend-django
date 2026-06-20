"""
authapp/views.py
================
All email sending and external HTTP calls have been moved to Celery tasks
(see tasks.py).  Views are now thin: validate → mutate DB → enqueue task →
respond.  No raw threads remain.
"""

import logging
import os

import jwt
from django.conf import settings
from django.contrib.auth import (
    get_user_model, login, logout,
)
from django.contrib.auth.hashers import check_password, make_password
from django.core.exceptions import ObjectDoesNotExist
from django.shortcuts import get_object_or_404, redirect
from django.template.loader import render_to_string
from django.urls import reverse
from django.utils import timezone
from django.utils.crypto import get_random_string
from django_ratelimit.decorators import ratelimit
from rest_framework import generics, permissions, status
from rest_framework.response import Response
from rest_framework.views import APIView
from rest_framework_simplejwt.tokens import RefreshToken
import requests

from authapp.models import CustomUser
from .models import CustomUser
from .serializers import (
    ChangePasswordRequestSerializer,
    ChangePasswordSerializer,
    CustomUserSerializer,
    GoogleSignInSerializer,
    PasswordResetConfirmSerializer,
    PasswordResetSerializer,
)
from .tasks import (
    send_change_password_code_task,
    send_login_alert_task,
    send_password_reset_code_task,
    send_verification_email_task,
)

User = get_user_model()
logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def _rate_limited_response():
    """Standard 429 response."""
    return Response(
        {"error": "Too many requests. Please try again later."},
        status=status.HTTP_429_TOO_MANY_REQUESTS,
    )


def _base_url(request) -> str:
    """
    Return the scheme + host of the current request
    (e.g. "https://api.plvcmonline.uk").

    This is the only piece of the request object that tasks need; passing
    the full request to Celery is not possible because it is not serialisable.
    """
    return f"{request.scheme}://{request.get_host()}"


# ---------------------------------------------------------------------------
# Views
# ---------------------------------------------------------------------------

class UserDetailView(generics.RetrieveAPIView):
    permission_classes = [permissions.IsAuthenticated]
    serializer_class = CustomUserSerializer

    def get_object(self):
        return self.request.user


class SignUpView(generics.CreateAPIView):
    permission_classes = [permissions.AllowAny]
    serializer_class = CustomUserSerializer

    def create(self, request, *args, **kwargs):
        # Rate limiting
        decorator = ratelimit(key="ip", rate="5/h", method="POST", block=False)
        decorator(lambda r: r)(request)
        if getattr(request, "limited", False):
            return _rate_limited_response()

        mutable_data = request.data.copy()
        email = mutable_data.get("email")

        if CustomUser.objects.filter(email=email).exists():
            logger.info("Signup rejected — email already exists: %s", email)
            return Response(
                {"error": "Email has already been used."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        # Create the user as inactive until email is verified
        mutable_data["is_active"] = False
        serializer = self.get_serializer(data=mutable_data)
        serializer.is_valid(raise_exception=True)
        self.perform_create(serializer)

        try:
            user = CustomUser.objects.get(email=email)
        except ObjectDoesNotExist:
            logger.error("User not found after creation: %s", email)
            return Response(
                {"error": "User creation failed."},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )

        # Enqueue verification email — no threads, no blocking
        send_verification_email_task.delay(user.id, _base_url(request))
        logger.info("Verification email task enqueued for user %s (id=%s)", email, user.id)

        response = Response(serializer.data, status=status.HTTP_201_CREATED)
        response.data["message"] = (
            "User registration successful. Please check your email for the verification link."
        )
        return response


class VerifyEmailView(APIView):
    """
    IDOR-safe email verification.

    The user_id comes exclusively from the decoded JWT payload — the URL
    carries only the token, never a raw user_id.

    URL: verify-email/<str:token>/
    """

    def get(self, request, token):
        if getattr(request, "limited", False):
            return Response(
                {"error": "Too many requests. Please try again later."},
                status=status.HTTP_429_TOO_MANY_REQUESTS,
            )

        try:
            payload = jwt.decode(token, settings.SECRET_KEY, algorithms=["HS256"])
            user_id_from_token = payload.get("user_id")

            if not user_id_from_token:
                return Response(
                    {"error": "Invalid token."},
                    status=status.HTTP_400_BAD_REQUEST,
                )

            user = get_object_or_404(User, id=user_id_from_token)

            if user.is_active:
                return redirect("https://plvcmonline.uk/login")

            user.is_active = True
            user.save()
            logger.info("Email verified for user id=%s", user_id_from_token)
            return redirect("https://plvcmonline.uk/login")

        except jwt.ExpiredSignatureError:
            logger.error("Activation link expired.")
            return Response(
                {"error": "Activation link has expired."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        except jwt.InvalidTokenError:
            logger.error("Invalid activation link.")
            return Response(
                {"error": "Invalid activation link."},
                status=status.HTTP_400_BAD_REQUEST,
            )


class GoogleSignInView(APIView):
    """Google OAuth2 sign-in — no emails to send, no Celery needed here."""

    def post(self, request):
        if getattr(request, "limited", False):
            return _rate_limited_response()

        logger.debug("Google sign-in request received.")
        serializer = GoogleSignInSerializer(data=request.data)
        if not serializer.is_valid():
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

        access_token = serializer.validated_data["access_token"]

        try:
            id_token_info = self._get_id_token_info(access_token)
            if not id_token_info:
                return Response(
                    {"success": False, "error": "Invalid or expired token"},
                    status=status.HTTP_400_BAD_REQUEST,
                )

            email = id_token_info.get("email")
            if not email:
                return Response(
                    {"success": False, "error": "Email not found in token"},
                    status=status.HTTP_400_BAD_REQUEST,
                )

            user_profile = self._get_user_profile(access_token)

            first_name = (
                user_profile.get("given_name")
                or user_profile.get("first_name")
                or (user_profile.get("names") or [{}])[0].get("givenName", "")
                or ""
            )
            last_name = (
                user_profile.get("family_name")
                or user_profile.get("last_name")
                or (user_profile.get("names") or [{}])[0].get("familyName", "")
                or ""
            )

            user, created = User.objects.get_or_create(email=email)

            if created:
                user.is_google_account = True
                user.is_active = True
                user.is_blocked = False
                user.date_joined = timezone.now()
                user.save()
                logger.info("New Google user created: %s", email)

            user.first_name = first_name
            user.last_name = last_name
            user.last_login = timezone.now()
            user.save()

            if user.is_blocked:
                return Response(
                    {"success": False, "error": "User account is blocked. Please contact support."},
                    status=status.HTTP_403_FORBIDDEN,
                )
            if not user.is_active:
                return Response(
                    {"success": False, "error": "User account is inactive. Please verify your email or contact support."},
                    status=status.HTTP_403_FORBIDDEN,
                )
            if not user.is_google_account:
                return Response(
                    {"success": False, "error": "Account was not created with Gmail. Please login with your email and password"},
                    status=status.HTTP_403_FORBIDDEN,
                )

            refresh = RefreshToken.for_user(user)
            return Response({
                "success":    True,
                "email":      email,
                "created":    created,
                "first_name": user.first_name,
                "last_name":  user.last_name,
                "access":     str(refresh.access_token),
                "refresh":    str(refresh),
            })

        except Exception as exc:
            logger.exception("Unexpected error during Google sign-in: %s", exc)
            return Response(
                {"success": False, "error": str(exc)},
                status=status.HTTP_400_BAD_REQUEST,
            )

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _get_id_token_info(self, access_token: str) -> dict | None:
        try:
            response = requests.get(
                "https://www.googleapis.com/oauth2/v3/tokeninfo",
                params={"access_token": access_token},
                timeout=5,
            )
            return response.json() if response.status_code == 200 else None
        except Exception as exc:
            logger.error("Error fetching Google ID token info: %s", exc)
            return None

    def _get_user_profile(self, access_token: str) -> dict:
        # Try userinfo endpoint first
        try:
            response = requests.get(
                "https://www.googleapis.com/oauth2/v3/userinfo",
                headers={"Authorization": f"Bearer {access_token}"},
                timeout=5,
            )
            if response.status_code == 200:
                return response.json()
        except Exception as exc:
            logger.error("UserInfo endpoint error: %s", exc)

        # Fall back to People API
        try:
            response = requests.get(
                "https://people.googleapis.com/v1/people/me?personFields=names,emailAddresses",
                headers={"Authorization": f"Bearer {access_token}"},
                timeout=5,
            )
            if response.status_code == 200:
                return response.json()
        except Exception as exc:
            logger.error("People API error: %s", exc)

        return {}


class LoginView(APIView):
    def post(self, request):
        if getattr(request, "limited", False):
            return _rate_limited_response()

        email    = request.data.get("email")
        password = request.data.get("password")
        logger.debug("Login attempt for: %s", email)

        try:
            user = CustomUser.objects.get(email=email)
        except CustomUser.DoesNotExist:
            return Response(
                {"error": "Incorrect username or password."},
                status=status.HTTP_401_UNAUTHORIZED,
            )

        if user.is_blocked:
            return Response(
                {"error": "Your account has been blocked."},
                status=status.HTTP_403_FORBIDDEN,
            )
        if user.is_google_account:
            return Response(
                {"error": "Your account was created with Google. Please login with your Google account"},
                status=status.HTTP_403_FORBIDDEN,
            )
        if not user.is_active:
            return Response(
                {"error": "Account not verified."},
                status=status.HTTP_401_UNAUTHORIZED,
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

        if not check_password(password, user.password):
            return Response(
                {"error": "Incorrect username or password."},
                status=status.HTTP_401_UNAUTHORIZED,
            )

        login(request, user)
        refresh = RefreshToken.for_user(user)

        # Enqueue login alert — geo-lookup and email sending happen in the worker
        send_login_alert_task.delay(user.id, _base_url(request))
        logger.info("Login alert task enqueued for user id=%s", user.id)

        user_data = CustomUserSerializer(user).data
        return Response(
            {
                "access_token":  str(refresh.access_token),
                "refresh_token": str(refresh),
                "user":          user_data,
            },
            status=status.HTTP_200_OK,
        )


# ---------------------------------------------------------------------------
# Password Reset — Student Portal
# ---------------------------------------------------------------------------

class StudentPasswordResetView(APIView):
    permission_classes = [permissions.AllowAny]
    serializer_class = PasswordResetSerializer

    def get(self, request, *args, **kwargs):
        if getattr(request, "limited", False):
            return _rate_limited_response()

        email = request.query_params.get("email")
        if not email:
            return Response(
                {"error": "Email parameter is missing."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        user = User.objects.filter(email=email).first()
        if user and user.is_active and user.role == "student":
            return Response({"message": "Email is registered."}, status=status.HTTP_200_OK)
        return Response(
            {"error": "Email not registered, not active, or does not belong to a student account."},
            status=status.HTTP_400_BAD_REQUEST,
        )

    def post(self, request, *args, **kwargs):
        if getattr(request, "limited", False):
            return _rate_limited_response()

        serializer = self.serializer_class(data=request.data)
        if not serializer.is_valid():
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

        email = serializer.validated_data["email"]
        user  = User.objects.filter(email=email).first()

        if not user or not user.is_active:
            return Response(
                {"error": "Email not registered or not active."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        if user.role != "student":
            return Response(
                {"error": "This endpoint is only for student accounts. Please use the correct portal."},
                status=status.HTTP_403_FORBIDDEN,
            )

        verification_code = get_random_string(length=6, allowed_chars="0123456789")
        user.verification_code = verification_code
        user.save()

        send_password_reset_code_task.delay(email, verification_code)
        logger.info("[Student] Password reset code task enqueued for %s", email)
        return Response(
            {"message": "Verification code sent to your email."},
            status=status.HTTP_200_OK,
        )


# ---------------------------------------------------------------------------
# Password Reset — Admin Portal
# ---------------------------------------------------------------------------

class AdminPasswordResetView(APIView):
    permission_classes = [permissions.AllowAny]
    serializer_class = PasswordResetSerializer

    def get(self, request, *args, **kwargs):
        if getattr(request, "limited", False):
            return _rate_limited_response()

        email = request.query_params.get("email")
        if not email:
            return Response(
                {"error": "Email parameter is missing."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        user = User.objects.filter(email=email).first()
        if user and user.is_active and user.role in ["principal", "staff"]:
            return Response({"message": "Email is registered."}, status=status.HTTP_200_OK)
        return Response(
            {"error": "Email not registered, not active, or does not belong to an admin account."},
            status=status.HTTP_400_BAD_REQUEST,
        )

    def post(self, request, *args, **kwargs):
        if getattr(request, "limited", False):
            return _rate_limited_response()

        serializer = self.serializer_class(data=request.data)
        if not serializer.is_valid():
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

        email = serializer.validated_data["email"]
        user  = User.objects.filter(email=email).first()

        if not user or not user.is_active:
            return Response(
                {"error": "Email not registered or not active."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        if user.role not in ["principal", "staff"]:
            return Response(
                {"error": "This endpoint is only for admin accounts (principal or staff). Please use the correct portal."},
                status=status.HTTP_403_FORBIDDEN,
            )

        verification_code = get_random_string(length=6, allowed_chars="0123456789")
        user.verification_code = verification_code
        user.save()

        send_password_reset_code_task.delay(email, verification_code)
        logger.info("[Admin] Password reset code task enqueued for %s", email)
        return Response(
            {"message": "Verification code sent to your email."},
            status=status.HTTP_200_OK,
        )


# ---------------------------------------------------------------------------
# Password Reset — School Website
# ---------------------------------------------------------------------------

class WebsitePasswordResetView(APIView):
    permission_classes = [permissions.AllowAny]
    serializer_class = PasswordResetSerializer

    def get(self, request, *args, **kwargs):
        if getattr(request, "limited", False):
            return _rate_limited_response()

        email = request.query_params.get("email")
        if not email:
            return Response(
                {"error": "Email parameter is missing."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        user = User.objects.filter(email=email).first()
        if user and user.is_active and user.role not in ["principal", "staff", "student"]:
            return Response({"message": "Email is registered."}, status=status.HTTP_200_OK)
        return Response(
            {"error": "Email not registered, not active, or must use a dedicated portal."},
            status=status.HTTP_400_BAD_REQUEST,
        )

    def post(self, request, *args, **kwargs):
        if getattr(request, "limited", False):
            return _rate_limited_response()

        serializer = self.serializer_class(data=request.data)
        if not serializer.is_valid():
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

        email = serializer.validated_data["email"]
        user  = User.objects.filter(email=email).first()

        if not user or not user.is_active:
            return Response(
                {"error": "Email not registered or not active."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        if user.role in ["principal", "staff", "student"]:
            return Response(
                {"error": "Please use your designated portal to reset your password."},
                status=status.HTTP_403_FORBIDDEN,
            )

        verification_code = get_random_string(length=6, allowed_chars="0123456789")
        user.verification_code = verification_code
        user.save()

        send_password_reset_code_task.delay(email, verification_code)
        logger.info("[Website] Password reset code task enqueued for %s", email)
        return Response(
            {"message": "Verification code sent to your email."},
            status=status.HTTP_200_OK,
        )


# ---------------------------------------------------------------------------
# Password Reset Confirm & Verify Code (shared)
# ---------------------------------------------------------------------------

class PasswordResetConfirmView(APIView):
    permission_classes = [permissions.AllowAny]
    serializer_class = PasswordResetConfirmSerializer

    def post(self, request, *args, **kwargs):
        if getattr(request, "limited", False):
            return _rate_limited_response()

        serializer = self.serializer_class(data=request.data)
        if not serializer.is_valid():
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

        email             = serializer.validated_data["email"]
        verification_code = serializer.validated_data["verification_code"]
        new_password      = serializer.validated_data["new_password"]

        user = User.objects.filter(email=email, verification_code=verification_code).first()
        if user:
            user.password          = make_password(new_password)
            user.verification_code = None
            user.save()
            logger.info("Password successfully reset for %s", email)
            return Response({"message": "Password reset successful."}, status=status.HTTP_200_OK)
        return Response(
            {"error": "Invalid verification code."},
            status=status.HTTP_400_BAD_REQUEST,
        )


class VerifyResetCodeView(APIView):
    permission_classes = [permissions.AllowAny]

    def post(self, request, *args, **kwargs):
        if getattr(request, "limited", False):
            return _rate_limited_response()

        email             = request.data.get("email")
        verification_code = request.data.get("verification_code")
        user = User.objects.filter(email=email, verification_code=verification_code).first()
        if user:
            return Response({"message": "Verification code is valid."}, status=status.HTTP_200_OK)
        return Response(
            {"error": "Invalid verification code."},
            status=status.HTTP_400_BAD_REQUEST,
        )


# ---------------------------------------------------------------------------
# Change Password (authenticated users)
# ---------------------------------------------------------------------------

class ChangePasswordRequestView(APIView):
    permission_classes = [permissions.IsAuthenticated]
    serializer_class = ChangePasswordRequestSerializer

    def post(self, request, *args, **kwargs):
        if getattr(request, "limited", False):
            return _rate_limited_response()

        serializer = self.serializer_class(data=request.data, context={"request": request})
        if not serializer.is_valid():
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

        user = request.user
        if not user.email or not user.is_active:
            return Response(
                {"error": "User account is not active or email is missing."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        verification_code          = get_random_string(length=6, allowed_chars="0123456789")
        user.verification_code     = verification_code
        user.save()

        send_change_password_code_task.delay(user.email, verification_code)
        logger.info("Change-password code task enqueued for %s", user.email)
        return Response(
            {"message": "Verification code sent to your email."},
            status=status.HTTP_200_OK,
        )


class ChangePasswordView(APIView):
    permission_classes = [permissions.IsAuthenticated]
    serializer_class = ChangePasswordSerializer

    def post(self, request, *args, **kwargs):
        if getattr(request, "limited", False):
            return _rate_limited_response()

        serializer = self.serializer_class(data=request.data, context={"request": request})
        if not serializer.is_valid():
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

        user              = request.user
        verification_code = serializer.validated_data["verification_code"]
        old_password      = serializer.validated_data["old_password"]
        new_password      = serializer.validated_data["new_password"]

        if user.verification_code != verification_code:
            return Response(
                {"error": "Invalid verification code."},
                status=status.HTTP_400_BAD_REQUEST,
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

        user.set_password(new_password)
        user.verification_code = None
        user.save()

        logger.info("Password changed for %s", user.email)
        return Response({"message": "Password changed successfully."}, status=status.HTTP_200_OK)


class VerifyChangePasswordCodeView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request, *args, **kwargs):
        if getattr(request, "limited", False):
            return _rate_limited_response()

        verification_code = request.data.get("verification_code")
        if not verification_code:
            return Response(
                {"error": "Verification code is required."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        user = request.user
        if user.verification_code == verification_code:
            return Response({"message": "Verification code is valid."}, status=status.HTTP_200_OK)
        return Response(
            {"error": "Invalid verification code. Please check your email and try again."},
            status=status.HTTP_400_BAD_REQUEST,
        )


class LogoutView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request):
        logout(request)
        return Response({"detail": "Logout successful"})