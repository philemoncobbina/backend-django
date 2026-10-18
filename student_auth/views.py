"""
student_auth/views.py

Student accounts are created by staff. Credentials are issued as HttpOnly cookies (named
per portal, see authapp/authentication.py) and the cookie path is protected by CSRF.
Email runs in Celery; the student's password is never emailed or queued.
"""

import logging

import jwt
from django.conf import settings
from django.contrib.auth.hashers import make_password
from django.contrib.auth.models import update_last_login
from django.contrib.auth.password_validation import validate_password
from django.core.cache import cache
from django.core.exceptions import ValidationError as DjangoValidationError
from django.db import transaction
from django.db.models.signals import pre_save
from django.dispatch import receiver
from django.shortcuts import redirect
from django.utils.crypto import get_random_string
from rest_framework import generics, status
from rest_framework.exceptions import ValidationError
from rest_framework.response import Response
from rest_framework.views import APIView
from rest_framework_simplejwt.tokens import RefreshToken

from authapp.authentication import set_auth_cookies
from authapp.models import CustomUser
from authapp.shared_auth import (
    PublicMixin,
    base_url,
    blocked,
    rate_limited,
    tokens_in_body,
)

from .permissions import IsTeacherOrPrincipalOrSuperuser
from .serializers import ParentGuardianSerializer, StudentUserSerializer
from .tasks import STUDENT_VERIFY_TOKEN_TYPE, send_student_verification_email

logger = logging.getLogger(__name__)

GUARDIAN_HIDDEN_FIELDS = ("id_type", "id_number")


# ---------------------------------------------------------------------------
# Signal (unchanged): every newly created user starts inactive
# ---------------------------------------------------------------------------

@receiver(pre_save, sender=CustomUser)
def set_is_active_to_false(sender, instance, **kwargs):
    if instance._state.adding:
        instance.is_active = False


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _password_errors(password):
    try:
        validate_password(password)
    except DjangoValidationError as exc:
        return {"password": list(exc.messages)}
    return None


def _guardians_for_client(user):
    """Guardian contact details for the student's own screen, without ID document numbers."""
    data = ParentGuardianSerializer(user.guardians.all(), many=True).data
    return [{k: v for k, v in g.items() if k not in GUARDIAN_HIDDEN_FIELDS} for g in data]


def _clean_student_payload(raw):
    """Normalise one student's input. Returns (data, error); error is a str or a dict."""
    if not hasattr(raw, "get"):
        return None, "Each student must be an object."

    data = raw.copy()
    email = data.get("email")
    index_number = data.get("index_number")
    class_name = data.get("class_name")

    if not all(isinstance(v, str) and v.strip() for v in (email, index_number, class_name)):
        return None, "Email, index number, and class name are required."

    data["email"] = email.strip()
    data["index_number"] = index_number.strip()
    if not data.get("username"):
        data["username"] = data["index_number"].lower()
    data["role"] = "student"

    password = data.get("password")
    if not password:
        # Nobody needs to know it: the student picks their own with "Forgot password".
        data["password"] = get_random_string(32)
    elif not isinstance(password, str):
        return None, {"password": ["Invalid password."]}
    elif getattr(settings, "STUDENT_ENFORCE_PASSWORD_POLICY", True):
        errors = _password_errors(password)
        if errors:
            return None, errors

    return data, None


class _StudentCreation:
    serializer_class = StudentUserSerializer
    permission_classes = [IsTeacherOrPrincipalOrSuperuser]

    @staticmethod
    def _may_create(user):
        return user.is_superuser or user.role in ("principal", "staff")

    def _save_and_queue(self, data, url):
        serializer = self.get_serializer(data=data)
        serializer.is_valid(raise_exception=True)
        with transaction.atomic():  # a failure while saving guardians rolls the student back
            user = serializer.save()
            transaction.on_commit(lambda uid=user.id: send_student_verification_email.delay(uid, url))
        return user, serializer


# ---------------------------------------------------------------------------
# Account creation
# ---------------------------------------------------------------------------

@rate_limited(ip="300/h")
class StudentSignUpView(_StudentCreation, generics.CreateAPIView):
    def create(self, request, *args, **kwargs):
        if (r := blocked(request)):
            return r

        if not self._may_create(request.user):
            return Response(
                {"error": "Only principals, staff, or superusers can create student accounts."},
                status=status.HTTP_403_FORBIDDEN,
            )

        data, error = _clean_student_payload(request.data)
        if error:
            body = {"error": error} if isinstance(error, str) else error
            return Response(body, status=status.HTTP_400_BAD_REQUEST)

        if CustomUser.objects.filter(email__iexact=data["email"]).exists():
            return Response(
                {"error": "Email has already been used."}, status=status.HTTP_400_BAD_REQUEST
            )

        _, serializer = self._save_and_queue(data, base_url(request))

        headers = self.get_success_headers(serializer.data)
        return Response(
            {"message": "Student registration successful. Please check email for the verification link."},
            status=status.HTTP_201_CREATED,
            headers=headers,
        )


@rate_limited(ip="30/h")
class BatchStudentCreationView(_StudentCreation, generics.CreateAPIView):
    """Create several student accounts at once. Each student succeeds or fails on its own."""

    def create(self, request, *args, **kwargs):
        if (r := blocked(request)):
            return r

        if not self._may_create(request.user):
            return Response(
                {"error": "Only principals, staff, or superusers can create student accounts."},
                status=status.HTTP_403_FORBIDDEN,
            )

        students = request.data.get("students") if hasattr(request.data, "get") else None
        if not isinstance(students, list) or not students:
            return Response(
                {"error": "Please provide a list of student data."}, status=status.HTTP_400_BAD_REQUEST
            )

        limit = getattr(settings, "STUDENT_BATCH_MAX", 50)
        if len(students) > limit:
            return Response(
                {"error": f"Send at most {limit} students per request."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        url = base_url(request)
        created, errors = [], []

        for i, raw in enumerate(students):
            label = raw.get("index_number") if hasattr(raw, "get") else None
            data, error = _clean_student_payload(raw)
            if error:
                errors.append({"index": i, "index_number": label, "error": error})
                continue

            if CustomUser.objects.filter(email__iexact=data["email"]).exists():
                errors.append({"index": i, "index_number": label, "error": "Email has already been used."})
                continue

            try:
                user, _ = self._save_and_queue(data, url)
            except ValidationError as exc:
                errors.append({"index": i, "index_number": label, "error": exc.detail})
            except Exception:  # noqa: BLE001
                logger.exception("Batch student creation failed at index %s", i)
                errors.append({"index": i, "index_number": label, "error": "Could not create this student."})
            else:
                created.append(
                    {
                        "index_number": user.index_number,
                        "email": user.email,
                        "class_name": user.class_name,
                        "guardians_created": user.guardians.count(),
                    }
                )

        return Response(
            {
                "message": f"Successfully created {len(created)} student accounts.",
                "created_students": created,
                "errors": errors,  # no longer echoes the submitted data (it contained passwords)
            },
            status=status.HTTP_201_CREATED if created else status.HTTP_400_BAD_REQUEST,
        )


# ---------------------------------------------------------------------------
# Email verification
# ---------------------------------------------------------------------------

@rate_limited(ip="60/h", methods=("GET",))
class StudentVerifyEmailView(PublicMixin, APIView):
    """Only accepts the dedicated student verification token, never a session token."""

    invalid_response = staticmethod(
        lambda: Response({"error": "Invalid activation link."}, status=status.HTTP_400_BAD_REQUEST)
    )

    def get(self, request, token):
        if (r := blocked(request)):
            return r

        try:
            payload = jwt.decode(token, settings.SECRET_KEY, algorithms=["HS256"])
        except jwt.ExpiredSignatureError:
            return self._expired(request, token)
        except jwt.InvalidTokenError:
            return self.invalid_response()

        user_id = payload.get("user_id")
        if payload.get("token_type") != STUDENT_VERIFY_TOKEN_TYPE or not user_id:
            return self.invalid_response()

        with transaction.atomic():
            user = CustomUser.objects.select_for_update().filter(id=user_id).first()
            if not user:
                return Response({"error": "Account not found."}, status=status.HTTP_404_NOT_FOUND)
            if user.role != "student":
                return self.invalid_response()
            if user.is_blocked:
                return Response(
                    {"error": "This account is blocked. Please contact school administration."},
                    status=status.HTTP_403_FORBIDDEN,
                )
            if not user.is_active:
                user.is_active = True
                user.save(update_fields=["is_active"])
                logger.info("Student email verified for user id=%s", user_id)

        return self._done()

    def _expired(self, request, token):
        try:
            payload = jwt.decode(
                token, settings.SECRET_KEY, algorithms=["HS256"], options={"verify_exp": False}
            )
        except jwt.InvalidTokenError:
            return self.invalid_response()

        if payload.get("token_type") != STUDENT_VERIFY_TOKEN_TYPE or not payload.get("user_id"):
            return self.invalid_response()

        user = CustomUser.objects.filter(id=payload["user_id"], role="student").first()
        message = {"error": "Activation link has expired."}

        if user and user.is_active and not user.is_blocked:
            return self._done()

        if user and not user.is_blocked:
            # At most one fresh link per hour for each student.
            if cache.add(f"student-verify-resend:{user.id}", 1, timeout=3600):
                send_student_verification_email.delay(user.id, base_url(request))
                message["message"] = "A new verification link has been sent to your email."
            else:
                message["message"] = "A new link was sent recently. Please check your email."
        else:
            message["message"] = "Please contact school administration."

        return Response(message, status=status.HTTP_400_BAD_REQUEST)

    @staticmethod
    def _done():
        login_url = getattr(settings, "STUDENT_PORTAL_LOGIN_URL", None)
        if login_url:
            return redirect(login_url)
        return Response({"message": "Email verified. You can now log in."}, status=status.HTTP_200_OK)


# ---------------------------------------------------------------------------
# Login
# ---------------------------------------------------------------------------

@rate_limited(ip="60/10m", email="10/15m")
class StudentLoginView(PublicMixin, APIView):
    def post(self, request):
        if (r := blocked(request)):
            return r

        email = request.data.get("email")
        index_number = request.data.get("index_number")
        password = request.data.get("password")

        if isinstance(email, str) and email.strip():
            user = CustomUser.objects.filter(email__iexact=email.strip()).first()
        elif isinstance(index_number, str) and index_number.strip():
            user = CustomUser.objects.filter(index_number__iexact=index_number.strip()).first()
        else:
            return Response(
                {"error": "Please provide either an email or index number."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        if not isinstance(password, str) or not password:
            return Response({"error": "Password is required."}, status=status.HTTP_400_BAD_REQUEST)

        bad_credentials = Response(
            {"error": "Incorrect login credentials."}, status=status.HTTP_401_UNAUTHORIZED
        )

        if not user:
            make_password(password)  # spend the same time as a real check
            return bad_credentials

        # Verify the password BEFORE saying anything about the account's role or state.
        if not user.check_password(password):
            return bad_credentials

        if user.role != "student":
            return Response(
                {"error": "This login is for students only."}, status=status.HTTP_403_FORBIDDEN
            )
        if user.is_blocked:
            return Response(
                {"error": "Your account has been blocked. Please contact school administration."},
                status=status.HTTP_403_FORBIDDEN,
            )
        if not user.is_active:
            return Response(
                {"error": "Account not verified. Please check your email for the verification link."},
                status=status.HTTP_401_UNAUTHORIZED,
            )

        update_last_login(None, user)

        refresh = RefreshToken.for_user(user)
        access = str(refresh.access_token)

        body = {
            "user": {
                "id": user.id,
                "email": user.email,
                "index_number": user.index_number,
                "class_name": user.class_name,
                "first_name": user.first_name,
                "last_name": user.last_name,
                "role": user.role,
                "guardians": _guardians_for_client(user),
            }
        }
        if tokens_in_body():
            body["access_token"] = access
            body["refresh_token"] = str(refresh)

        return set_auth_cookies(Response(body), access, str(refresh), request=request)