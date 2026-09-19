import logging

import jwt
from django.apps import apps
from django.conf import settings
from django.contrib.auth import get_user_model, login, logout
from django.contrib.auth.hashers import check_password
from django.db import transaction
from django.shortcuts import get_object_or_404, redirect
from django.urls import reverse
from django.utils import timezone
from rest_framework import generics, permissions, status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView
from rest_framework_simplejwt.tokens import RefreshToken
from sib_api_v3_sdk import ApiClient, Configuration, SendSmtpEmail
from sib_api_v3_sdk.api.transactional_emails_api import (
    TransactionalEmailsApi,
)
from sib_api_v3_sdk.rest import ApiException

from authapp.models import CustomUser

from .models import UserChangeLog
from .permissions import IsPrincipalOrSuperuser
from .serializers import (
    AdminUserSerializer,
    UserChangeLogSerializer,
    create_user_change_log,
)


User = get_user_model()
logger = logging.getLogger(__name__)


def _rate_limited_response():
    return Response(
        {"error": "Too many requests. Please try again later."},
        status=status.HTTP_429_TOO_MANY_REQUESTS,
    )


class UserDetailView(generics.RetrieveAPIView):
    permission_classes = [permissions.IsAuthenticated]
    serializer_class = AdminUserSerializer

    def get_object(self):
        return self.request.user

    def get(self, request, *args, **kwargs):
        user = self.get_object()
        serializer = self.get_serializer(user)

        user_data = dict(serializer.data)
        user_data["role"] = user.role

        return Response(
            user_data,
            status=status.HTTP_200_OK,
        )


class SessionCheckView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request, *args, **kwargs):
        user = request.user

        response = Response(
            {
                "authenticated": True,
                "user": {
                    "id": user.id,
                    "email": user.email,
                    "role": user.role,
                },
            },
            status=status.HTTP_200_OK,
        )
        response["Cache-Control"] = "private, max-age=300"

        return response


class AdminUserManagementView(APIView):
    permission_classes = [IsPrincipalOrSuperuser]

    def _can_modify_users(self, request):
        return (
            request.user.is_superuser
            or request.user.role == "principal"
        )

    def get(self, request, *args, **kwargs):
        """
        Return all users.

        Access is controlled by IsPrincipalOrSuperuser.
        """
        users = (
            CustomUser.objects
            .prefetch_related("guardians")
            .all()
        )

        serializer = AdminUserSerializer(
            users,
            many=True,
            context={"request": request},
        )

        return Response(
            serializer.data,
            status=status.HTTP_200_OK,
        )

    @transaction.atomic
    def patch(self, request, user_id, *args, **kwargs):
        if not self._can_modify_users(request):
            logger.warning(
                "Unauthorized PATCH attempt by %s with role %s",
                request.user.email,
                request.user.role,
            )
            return Response(
                {
                    "error": (
                        "Permission denied. Only principals or superusers "
                        "can modify users."
                    )
                },
                status=status.HTTP_403_FORBIDDEN,
            )

        try:
            user = (
                CustomUser.objects
                .select_for_update()
                .get(id=user_id)
            )
        except CustomUser.DoesNotExist:
            logger.warning(
                "User with ID %s was not found for update",
                user_id,
            )
            return Response(
                {"error": "User not found."},
                status=status.HTTP_404_NOT_FOUND,
            )

        action = request.data.get("action")

        if action == "block":
            if user.is_blocked and not user.is_active:
                return Response(
                    {
                        "message": (
                            f"User {user.email} is already blocked."
                        )
                    },
                    status=status.HTTP_200_OK,
                )

            previous_value = {
                "is_blocked": user.is_blocked,
                "is_active": user.is_active,
            }

            user.is_blocked = True
            user.is_active = False
            user.save(
                update_fields=[
                    "is_blocked",
                    "is_active",
                ]
            )

            create_user_change_log(
                target_user=user,
                changed_by=request.user,
                action=UserChangeLog.Action.BLOCK,
                field_name="account_status",
                previous_value=previous_value,
                new_value={
                    "is_blocked": True,
                    "is_active": False,
                },
                request=request,
            )

            logger.info(
                "User %s blocked by %s",
                user.email,
                request.user.email,
            )

            return Response(
                {
                    "message": (
                        f"User {user.email} has been blocked."
                    )
                },
                status=status.HTTP_200_OK,
            )

        if action == "unblock":
            if not user.is_blocked and user.is_active:
                return Response(
                    {
                        "message": (
                            f"User {user.email} is already unblocked."
                        )
                    },
                    status=status.HTTP_200_OK,
                )

            previous_value = {
                "is_blocked": user.is_blocked,
                "is_active": user.is_active,
            }

            user.is_blocked = False
            user.is_active = True
            user.save(
                update_fields=[
                    "is_blocked",
                    "is_active",
                ]
            )

            create_user_change_log(
                target_user=user,
                changed_by=request.user,
                action=UserChangeLog.Action.UNBLOCK,
                field_name="account_status",
                previous_value=previous_value,
                new_value={
                    "is_blocked": False,
                    "is_active": True,
                },
                request=request,
            )

            logger.info(
                "User %s unblocked by %s",
                user.email,
                request.user.email,
            )

            return Response(
                {
                    "message": (
                        f"User {user.email} has been unblocked."
                    )
                },
                status=status.HTTP_200_OK,
            )

        if action == "activate":
            if user.is_active:
                return Response(
                    {
                        "message": (
                            f"User {user.email} is already active."
                        )
                    },
                    status=status.HTTP_200_OK,
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

            logger.info(
                "User %s activated by %s",
                user.email,
                request.user.email,
            )

            return Response(
                {
                    "message": (
                        f"User {user.email} has been activated."
                    )
                },
                status=status.HTTP_200_OK,
            )

        if action == "deactivate":
            if not user.is_active:
                return Response(
                    {
                        "message": (
                            f"User {user.email} is already inactive."
                        )
                    },
                    status=status.HTTP_200_OK,
                )

            if user.pk == request.user.pk:
                return Response(
                    {
                        "error": (
                            "You cannot deactivate your own account through "
                            "this administrative endpoint."
                        )
                    },
                    status=status.HTTP_400_BAD_REQUEST,
                )

            previous_value = user.is_active
            user.is_active = False
            user.save(update_fields=["is_active"])

            create_user_change_log(
                target_user=user,
                changed_by=request.user,
                action=UserChangeLog.Action.DEACTIVATE,
                field_name="is_active",
                previous_value=previous_value,
                new_value=False,
                request=request,
            )

            logger.info(
                "User %s deactivated by %s",
                user.email,
                request.user.email,
            )

            return Response(
                {
                    "message": (
                        f"User {user.email} has been deactivated."
                    )
                },
                status=status.HTTP_200_OK,
            )

        if action == "edit":
            old_class = user.class_name

            mutable_data = request.data.copy()
            mutable_data.pop("action", None)

            serializer = AdminUserSerializer(
                user,
                data=mutable_data,
                partial=True,
                context={"request": request},
            )
            serializer.is_valid(raise_exception=True)
            updated_user = serializer.save()

            new_class = updated_user.class_name

            if (
                new_class
                and new_class != old_class
                and updated_user.is_student
            ):
                self._handle_class_change(
                    updated_user,
                    old_class,
                    new_class,
                )

            logger.info(
                "User %s updated by %s",
                updated_user.email,
                request.user.email,
            )

            response_serializer = AdminUserSerializer(
                updated_user,
                context={"request": request},
            )

            return Response(
                {
                    "message": "User details updated successfully.",
                    "user": response_serializer.data,
                },
                status=status.HTTP_200_OK,
            )

        logger.warning(
            "Unknown user-management action '%s' submitted by %s",
            action,
            request.user.email,
        )

        return Response(
            {
                "error": (
                    "Invalid action. Supported actions are block, unblock, "
                    "activate, deactivate, and edit."
                )
            },
            status=status.HTTP_400_BAD_REQUEST,
        )

    def _handle_class_change(self, user, old_class, new_class):
        """
        Persist the previous class when a student's class changes.
        """
        StudentClassHistory = apps.get_model(
            "booklist",
            "StudentClassHistory",
        )
        current_year = self._get_current_academic_year(user)

        if old_class != new_class:
            StudentClassHistory.objects.create(
                student=user,
                academic_year=current_year,
                class_name=old_class or "",
            )

            logger.info(
                "Class history recorded for %s: %s to %s",
                user.email,
                old_class,
                new_class,
            )

    def _get_current_academic_year(self, user):
        # StudentClassHistory has no `created_at` field (its columns are
        # only academic_year, class_name, id, student), so ordering by
        # "-created_at" raises a FieldError. `id` is auto-incrementing, so
        # ordering by "-id" gives us the most recently created record
        # instead. If a `created_at` field is added to that model later,
        # switch this back to "-created_at" for true chronological order.
        latest_history = (
            user.class_history
            .order_by("-id")
            .first()
        )

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
                {
                    "error": (
                        "Permission denied. Only principals or superusers "
                        "can delete users."
                    )
                },
                status=status.HTTP_403_FORBIDDEN,
            )

        try:
            user = (
                CustomUser.objects
                .select_for_update()
                .get(id=user_id)
            )
        except CustomUser.DoesNotExist:
            return Response(
                {"error": "User not found."},
                status=status.HTTP_404_NOT_FOUND,
            )

        if user.pk == request.user.pk:
            return Response(
                {
                    "error": (
                        "You cannot delete your own account through this "
                        "administrative endpoint."
                    )
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

        logger.info(
            "User %s deleted by %s",
            user_email,
            request.user.email,
        )

        return Response(
            {
                "message": (
                    f"User {user_email} has been deleted successfully."
                )
            },
            status=status.HTTP_200_OK,
        )


class UserChangeLogListView(generics.ListAPIView):
    """
    Read-only endpoint for viewing user and guardian audit logs.

    Only principals and superusers can access this endpoint.

    Optional query parameters:
        target_user_email
        changed_by_email
        action
        field_name
    """

    permission_classes = [IsPrincipalOrSuperuser]
    serializer_class = UserChangeLogSerializer
    http_method_names = ["get", "head", "options"]

    def get_queryset(self):
        queryset = (
            UserChangeLog.objects
            .select_related(
                "target_user",
                "changed_by",
            )
            .all()
        )

        target_user_email = self.request.query_params.get(
            "target_user_email"
        )
        changed_by_email = self.request.query_params.get(
            "changed_by_email"
        )
        action = self.request.query_params.get("action")
        field_name = self.request.query_params.get("field_name")

        if target_user_email:
            queryset = queryset.filter(
                target_user_email__iexact=target_user_email.strip()
            )

        if changed_by_email:
            queryset = queryset.filter(
                changed_by_email__iexact=changed_by_email.strip()
            )

        if action:
            normalised_action = action.strip().lower()

            valid_actions = {
                choice.value
                for choice in UserChangeLog.Action
            }

            if normalised_action in valid_actions:
                queryset = queryset.filter(action=normalised_action)
            else:
                queryset = queryset.none()

        if field_name:
            queryset = queryset.filter(
                field_name__icontains=field_name.strip()
            )

        return queryset


class AdminSignUpView(generics.CreateAPIView):
    permission_classes = [IsPrincipalOrSuperuser]
    serializer_class = AdminUserSerializer

    def create(self, request, *args, **kwargs):
        if getattr(request, "limited", False):
            return _rate_limited_response()

        if not (
            request.user.is_superuser
            or request.user.role == "principal"
        ):
            return Response(
                {
                    "error": (
                        "Only principals or superusers can create users."
                    )
                },
                status=status.HTTP_403_FORBIDDEN,
            )

        mutable_data = request.data.copy()
        email = mutable_data.get("email")
        role = mutable_data.get("role", "staff")

        if role not in ["staff", "principal"]:
            return Response(
                {"error": "Invalid role provided."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        if not email:
            return Response(
                {"email": ["This field is required."]},
                status=status.HTTP_400_BAD_REQUEST,
            )

        normalised_email = email.strip()

        if CustomUser.objects.filter(
            email__iexact=normalised_email
        ).exists():
            return Response(
                {"error": "Email has already been used."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        mutable_data["email"] = normalised_email

        serializer = self.get_serializer(
            data=mutable_data,
            context={"request": request},
        )
        serializer.is_valid(raise_exception=True)
        user = serializer.save()

        self.send_verification_email(user)

        headers = self.get_success_headers(serializer.data)

        return Response(
            {
                "message": (
                    "User registration successful. Please check the email "
                    "address for the verification link."
                )
            },
            status=status.HTTP_201_CREATED,
            headers=headers,
        )

    def send_verification_email(self, user):
        verification_token = RefreshToken.for_user(user).access_token

        verification_url = reverse(
            "admin_auth:verify-email",
            kwargs={"token": str(verification_token)},
        )
        verification_url = self.request.build_absolute_uri(
            verification_url
        )

        email_body = f"""
        <html>
        <body style="font-family: Arial, sans-serif; color: #333;">
            <p>Dear {user.first_name},</p>

            <h2 style="color: #4CAF50;">
                Welcome to Our Service!
            </h2>

            <p>
                Thank you for registering with us. Please click the button
                below to verify your email address:
            </p>

            <p>
                {verification_url}
                    Verify Your Email
                </a>
            </p>

            <p>
                If you did not register for this account, please ignore this
                email.
            </p>

            <br>

            <p>
                Best regards,<br>
                Your Company Team
            </p>
        </body>
        </html>
        """

        configuration = Configuration()
        configuration.api_key["api-key"] = settings.BREVO_API_KEY

        api_instance = TransactionalEmailsApi(
            ApiClient(configuration)
        )

        send_smtp_email = SendSmtpEmail(
            to=[{"email": user.email}],
            sender={
                "name": "Your Company",
                "email": settings.DEFAULT_FROM_EMAIL,
            },
            subject="Verify Your Email",
            html_content=email_body,
        )

        try:
            response = api_instance.send_transac_email(
                send_smtp_email
            )
            logger.info(
                "Verification email sent to %s: %s",
                user.email,
                response,
            )
        except ApiException:
            logger.exception(
                "Failed to send verification email to %s",
                user.email,
            )


class VerifyEmailView(APIView):
    permission_classes = [permissions.AllowAny]

    def get(self, request, token):
        if getattr(request, "limited", False):
            return _rate_limited_response()

        try:
            payload = jwt.decode(
                token,
                settings.SECRET_KEY,
                algorithms=["HS256"],
            )
            user_id_from_token = payload.get("user_id")

            if not user_id_from_token:
                return Response(
                    {"error": "Invalid token."},
                    status=status.HTTP_400_BAD_REQUEST,
                )

            with transaction.atomic():
                user = get_object_or_404(
                    User.objects.select_for_update(),
                    id=user_id_from_token,
                )

                if not user.is_active:
                    previous_value = user.is_active
                    user.is_active = True
                    user.save(update_fields=["is_active"])

                    create_user_change_log(
                        target_user=user,
                        changed_by=user,
                        action=UserChangeLog.Action.ACTIVATE,
                        field_name="is_active",
                        previous_value=previous_value,
                        new_value=True,
                        request=request,
                    )

                    logger.info(
                        "Email verified for user id=%s",
                        user_id_from_token,
                    )

            dashboard_url = getattr(
                settings,
                "FRONTEND_DASHBOARD_URL",
                "http://localhost:5173/dashboard",
            )

            return redirect(dashboard_url)

        except jwt.ExpiredSignatureError:
            logger.warning("Activation link has expired.")
            return Response(
                {"error": "Activation link has expired."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        except jwt.InvalidTokenError:
            logger.warning("Invalid activation link.")
            return Response(
                {"error": "Invalid activation link."},
                status=status.HTTP_400_BAD_REQUEST,
            )


class LoginView(APIView):
    permission_classes = [permissions.AllowAny]

    def post(self, request):
        if getattr(request, "limited", False):
            return _rate_limited_response()

        email = request.data.get("email")
        password = request.data.get("password")

        if not email or not password:
            return Response(
                {"error": "Email and password are required."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        try:
            user = CustomUser.objects.get(
                email__iexact=email.strip()
            )
        except CustomUser.DoesNotExist:
            return Response(
                {"error": "Incorrect username or password."},
                status=status.HTTP_401_UNAUTHORIZED,
            )

        if user.role.lower() not in ["principal", "staff"]:
            return Response(
                {
                    "error": (
                        "You are not authorized to access the admin system."
                    )
                },
                status=status.HTTP_403_FORBIDDEN,
            )

        if user.is_blocked:
            return Response(
                {
                    "error": (
                        "Your account has been blocked. Please contact "
                        "support for assistance."
                    )
                },
                status=status.HTTP_403_FORBIDDEN,
            )

        if not user.is_active:
            return Response(
                {
                    "error": (
                        "Account not verified. Please check your email for "
                        "the verification link."
                    )
                },
                status=status.HTTP_401_UNAUTHORIZED,
            )

        if not check_password(password, user.password):
            return Response(
                {"error": "Incorrect username or password."},
                status=status.HTTP_401_UNAUTHORIZED,
            )

        login(request, user)
        refresh = RefreshToken.for_user(user)

        return Response(
            {
                "access_token": str(refresh.access_token),
                "refresh_token": str(refresh),
                "user": {
                    "id": user.id,
                    "email": user.email,
                    "role": user.role,
                },
            },
            status=status.HTTP_200_OK,
        )


class LogoutView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request):
        logout(request)

        return Response(
            {
                "success": True,
                "message": "Logged out successfully.",
            },
            status=status.HTTP_200_OK,
        )