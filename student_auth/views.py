"""
student_auth/views.py
=====================
All heavy I/O (email dispatch) is now offloaded to Celery tasks.
Auth checks, token generation, and validation remain synchronous so the
security surface is unchanged — only latency is reduced.
"""

import jwt
import logging

from django.contrib.auth import login
from django.contrib.auth.hashers import check_password
from django.core.exceptions import ObjectDoesNotExist
from django.shortcuts import get_object_or_404, redirect
from django.urls import reverse

from rest_framework import generics, status
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.views import APIView
from rest_framework_simplejwt.tokens import RefreshToken

from django.db.models.signals import pre_save
from django.dispatch import receiver
from django.conf import settings

from authapp.models import CustomUser, ParentGuardian
from .serializers import StudentUserSerializer, ParentGuardianSerializer
from .permissions import IsTeacherOrPrincipalOrSuperuser
from .tasks import (
    send_student_verification_email,
    send_batch_student_verification_email,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Signal
# ---------------------------------------------------------------------------

@receiver(pre_save, sender=CustomUser)
def set_is_active_to_false(sender, instance, **kwargs):
    if instance._state.adding:
        instance.is_active = False


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def _rate_limited_response():
    return Response(
        {"error": "Too many requests. Please try again later."},
        status=status.HTTP_429_TOO_MANY_REQUESTS,
    )


def _serialise_guardians(user: CustomUser) -> list[dict]:
    """
    Return a plain list of dicts describing a student's guardians so it can
    be passed safely to a Celery task (JSON-serialisable, no ORM objects).
    """
    result = []
    for g in user.guardians.all():
        result.append(
            {
                "full_name": g.full_name,
                "relationship": g.relationship,
                "relationship_display": g.get_relationship_display(),
                "primary_phone": g.primary_phone,
                "secondary_phone": g.secondary_phone or "",
                "email": g.email or "",
                "is_primary_contact": g.is_primary_contact,
            }
        )
    return result


# ---------------------------------------------------------------------------
# Views
# ---------------------------------------------------------------------------

class StudentSignUpView(generics.CreateAPIView):
    permission_classes = [IsTeacherOrPrincipalOrSuperuser]
    serializer_class = StudentUserSerializer

    def create(self, request, *args, **kwargs):
        if getattr(request, "limited", False):
            return _rate_limited_response()

        if not (request.user.is_superuser or request.user.role in ["principal", "staff"]):
            return Response(
                {"error": "Only principals, staff, or superusers can create student accounts."},
                status=status.HTTP_403_FORBIDDEN,
            )

        mutable_data = request.data.copy()

        email = mutable_data.get("email")
        index_number = mutable_data.get("index_number")
        class_name = mutable_data.get("class_name")

        if not email or not index_number or not class_name:
            return Response(
                {"error": "Email, index number, and class name are required."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        if CustomUser.objects.filter(email=email).exists():
            return Response(
                {"error": "Email has already been used."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        mutable_data["role"] = "student"
        if not mutable_data.get("username"):
            mutable_data["username"] = index_number.lower()

        serializer = self.get_serializer(data=mutable_data)
        serializer.is_valid(raise_exception=True)
        self.perform_create(serializer)

        try:
            user = CustomUser.objects.get(email=email)
        except ObjectDoesNotExist:
            return Response(
                {"error": "Student creation failed."},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )

        # Build the verification URL synchronously (needs request context).
        verification_token = RefreshToken.for_user(user).access_token
        verification_url = request.build_absolute_uri(
            reverse("student-verify-email", kwargs={"token": str(verification_token)})
        )

        # Serialise guardian data before handing off to Celery.
        guardian_list = _serialise_guardians(user)

        # Fire-and-forget — response returns immediately.
        send_student_verification_email.delay(
            to_email=user.email,
            first_name=user.first_name,
            username=user.username,
            index_number=user.index_number,
            class_name_display=user.get_class_name_display(),
            raw_password=request.data.get("password", ""),
            verification_url=verification_url,
            guardian_list=guardian_list,
        )

        headers = self.get_success_headers(serializer.data)
        return Response(
            {
                "message": (
                    "Student registration successful. "
                    "Please check email for the verification link."
                )
            },
            status=status.HTTP_201_CREATED,
            headers=headers,
        )


class StudentVerifyEmailView(APIView):
    """No async needed here — this is a lightweight DB read/write."""

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
                    {"error": "Invalid token."}, status=status.HTTP_400_BAD_REQUEST
                )

            user = get_object_or_404(CustomUser, id=user_id_from_token)

            if user.is_active:
                return redirect("http://localhost:5173/student-dashboard")

            user.is_active = True
            user.save()
            logger.info("Student email verified for user id=%s", user_id_from_token)
            return redirect("http://localhost:5173/student-dashboard")

        except jwt.ExpiredSignatureError:
            logger.error("Activation link has expired.")
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


class StudentLoginView(APIView):
    permission_classes = [AllowAny]

    def post(self, request):
        if getattr(request, "limited", False):
            return _rate_limited_response()

        email = request.data.get("email")
        password = request.data.get("password")
        index_number = request.data.get("index_number")

        try:
            if email:
                user = CustomUser.objects.get(email=email)
            elif index_number:
                user = CustomUser.objects.get(index_number=index_number)
            else:
                return Response(
                    {"error": "Please provide either an email or index number."},
                    status=status.HTTP_400_BAD_REQUEST,
                )
        except CustomUser.DoesNotExist:
            return Response(
                {"error": "Incorrect login credentials."},
                status=status.HTTP_401_UNAUTHORIZED,
            )

        if user.role != "student":
            return Response(
                {"error": "This login is for students only."},
                status=status.HTTP_403_FORBIDDEN,
            )

        if user.is_blocked:
            return Response(
                {
                    "error": (
                        "Your account has been blocked. "
                        "Please contact school administration."
                    )
                },
                status=status.HTTP_403_FORBIDDEN,
            )

        if not user.is_active:
            return Response(
                {
                    "error": (
                        "Account not verified. "
                        "Please check your email for the verification link."
                    )
                },
                status=status.HTTP_401_UNAUTHORIZED,
            )

        if check_password(password, user.password):
            login(request, user)
            refresh = RefreshToken.for_user(user)
            guardians = ParentGuardianSerializer(user.guardians.all(), many=True).data

            return Response(
                {
                    "access_token": str(refresh.access_token),
                    "refresh_token": str(refresh),
                    "user": {
                        "id": user.id,
                        "email": user.email,
                        "index_number": user.index_number,
                        "class_name": user.class_name,
                        "first_name": user.first_name,
                        "last_name": user.last_name,
                        "role": user.role,
                        "guardians": guardians,
                    },
                }
            )

        return Response(
            {"error": "Incorrect login credentials."},
            status=status.HTTP_401_UNAUTHORIZED,
        )


class BatchStudentCreationView(generics.CreateAPIView):
    """Create multiple student accounts at once from a data list."""

    permission_classes = [IsTeacherOrPrincipalOrSuperuser]
    serializer_class = StudentUserSerializer

    def create(self, request, *args, **kwargs):
        if getattr(request, "limited", False):
            return _rate_limited_response()

        if not (request.user.is_superuser or request.user.role in ["principal", "staff"]):
            return Response(
                {"error": "Only principals, staff, or superusers can create student accounts."},
                status=status.HTTP_403_FORBIDDEN,
            )

        students_data = request.data.get("students", [])
        if not students_data or not isinstance(students_data, list):
            return Response(
                {"error": "Please provide a list of student data."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        created_students = []
        errors = []

        for i, student_data in enumerate(students_data):
            if not student_data.get("username") and student_data.get("index_number"):
                student_data["username"] = student_data["index_number"].lower()
            student_data["role"] = "student"

            serializer = self.get_serializer(data=student_data)
            if serializer.is_valid():
                try:
                    user = serializer.save()

                    # Build URL and serialise guardians while we still have
                    # the request context — before handing off to Celery.
                    verification_token = RefreshToken.for_user(user).access_token
                    verification_url = request.build_absolute_uri(
                        reverse(
                            "student-verify-email",
                            kwargs={"token": str(verification_token)},
                        )
                    )
                    guardian_list = _serialise_guardians(user)

                    # Dispatch async — does not block the loop.
                    send_batch_student_verification_email.delay(
                        to_email=user.email,
                        first_name=user.first_name,
                        index_number=user.index_number,
                        class_name_display=user.get_class_name_display(),
                        verification_url=verification_url,
                        guardian_list=guardian_list,
                    )

                    created_students.append(
                        {
                            "index_number": user.index_number,
                            "email": user.email,
                            "class_name": user.class_name,
                            "guardians_created": user.guardians.count(),
                        }
                    )
                except Exception as exc:
                    errors.append({"index": i, "error": str(exc), "data": student_data})
            else:
                errors.append(
                    {"index": i, "error": serializer.errors, "data": student_data}
                )

        return Response(
            {
                "message": f"Successfully created {len(created_students)} student accounts.",
                "created_students": created_students,
                "errors": errors,
            },
            status=(
                status.HTTP_201_CREATED if created_students else status.HTTP_400_BAD_REQUEST
            ),
        )