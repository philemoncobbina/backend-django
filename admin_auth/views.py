# admin_auth/views.py
from rest_framework import generics, permissions, status
import requests
from rest_framework.response import Response
from rest_framework.views import APIView
from django.contrib.auth import authenticate, login, logout
from django.contrib.auth import get_user_model
from django.contrib.auth import authenticate, login
from django.core.mail import send_mail
from django.urls import reverse
from django.conf import settings
from rest_framework_simplejwt.tokens import RefreshToken
import jwt
import logging
from django.shortcuts import get_object_or_404, redirect
import os
from django.template.loader import render_to_string
from django.core.mail import EmailMultiAlternatives
from django.contrib.auth.hashers import check_password
from django.utils.crypto import get_random_string
from django.contrib.auth.hashers import make_password
from rest_framework.permissions import IsAuthenticated, AllowAny
import base64
from sib_api_v3_sdk import Configuration, ApiClient, SendSmtpEmail
from sib_api_v3_sdk.api.transactional_emails_api import TransactionalEmailsApi
from sib_api_v3_sdk.rest import ApiException
from django.template.loader import render_to_string
from rest_framework import generics, permissions, status
from rest_framework.response import Response
from authapp.models import CustomUser
from .serializers import AdminUserSerializer
from django.core.exceptions import ObjectDoesNotExist
from rest_framework import generics
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
import logging
from django.utils import timezone
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework import status
from authapp.models import CustomUser
from datetime import datetime
from django.apps import apps
from django.utils import timezone
from .permissions import IsPrincipalOrSuperuser

User = get_user_model()
logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Shared rate-limit helper
# ---------------------------------------------------------------------------

def _rate_limited_response():
    """Return a standard 429 response when a rate limit is exceeded."""
    return Response(
        {'error': 'Too many requests. Please try again later.'},
        status=status.HTTP_429_TOO_MANY_REQUESTS
    )


# ---------------------------------------------------------------------------
# Views
# ---------------------------------------------------------------------------

class UserDetailView(generics.RetrieveAPIView):
    permission_classes = [permissions.IsAuthenticated]
    serializer_class = AdminUserSerializer

    def get_object(self):
        return self.request.user

    def get(self, request, *args, **kwargs):
        user = self.get_object()
        serializer = self.get_serializer(user)
        user_data = serializer.data
        user_data['role'] = user.role
        return Response(user_data)


class SessionCheckView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request, *args, **kwargs):
        user = request.user
        response = Response({
            'authenticated': True,
            'user': {
                'id': user.id,
                'email': user.email,
                'role': user.role,
            }
        })
        response['Cache-Control'] = 'private, max-age=300'
        return response


class AdminUserManagementView(APIView):
    permission_classes = [IsPrincipalOrSuperuser]

    # ── Guardian e-mail conflict check ────────────────────────────────────

    @staticmethod
    def _validate_guardian_emails(student_email, guardians_data):
        """
        Return a Response(400) if any guardian in *guardians_data* carries the
        same e-mail as the student, otherwise return None.

        This is an explicit early-exit guard that runs *before* the serializer
        so the caller gets a clear, human-readable error message rather than a
        cryptic model-level ValidationError.

        Parameters
        ----------
        student_email : str | None
            The e-mail that will be recorded for the student after the update.
        guardians_data : list[dict] | None
            The raw guardian dicts from the request payload, or None when the
            key was omitted entirely (PATCH without guardians).
        """
        if not student_email or not guardians_data:
            return None

        conflicting = [
            gd.get('email', '')
            for gd in guardians_data
            if gd.get('email', '').strip().lower() == student_email.strip().lower()
        ]

        if conflicting:
            return Response(
                {
                    'error': (
                        "A guardian's email cannot be the same as the student's "
                        f"email ({student_email}). Please use a different email "
                        "address for the guardian."
                    )
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        return None

    @staticmethod
    def _validate_student_email_vs_existing_guardians(user, new_email):
        """
        Return a Response(400) if changing the student's e-mail to *new_email*
        would create a conflict with any guardian already saved on that user,
        otherwise return None.

        This prevents a subtle edge-case: an admin changes the student's e-mail
        to a value that is already stored as a guardian's e-mail without touching
        the guardians list at all.
        """
        conflicting = list(
            user.guardians
            .filter(email__iexact=new_email.strip())
            .values_list('email', flat=True)
        )

        if conflicting:
            return Response(
                {
                    'error': (
                        f"The new student email ({new_email}) conflicts with an "
                        "existing guardian email on this account. Update or remove "
                        "the guardian's email first."
                    )
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        return None

    # ── HTTP verbs ─────────────────────────────────────────────────────────

    def get(self, request, *args, **kwargs):
        """GET — accessible by staff (read-only), principals, and superusers."""
        users = CustomUser.objects.all()
        serializer = AdminUserSerializer(users, many=True)
        return Response(serializer.data, status=status.HTTP_200_OK)

    def patch(self, request, user_id, *args, **kwargs):
        """PATCH — only principals and superusers."""
        if not (request.user.is_superuser or request.user.role == 'principal'):
            logger.warning(
                f"Unauthorized PATCH attempt by user {request.user.email} "
                f"with role {request.user.role}"
            )
            return Response(
                {'error': 'Permission denied. Only principals can modify users.'},
                status=status.HTTP_403_FORBIDDEN
            )

        try:
            user = CustomUser.objects.get(id=user_id)
            logger.info(f"Found user with ID {user_id}: {user.email}")
        except CustomUser.DoesNotExist:
            logger.error(f"User with ID {user_id} not found")
            return Response({'error': 'User not found.'}, status=status.HTTP_404_NOT_FOUND)

        action = request.data.get('action')
        logger.debug(
            f"Processing action: {action} for user {user.email} by {request.user.email}"
        )

        if action == 'block':
            user.is_blocked = True
            user.is_active = False
            user.save()
            logger.info(f"User {user.email} blocked successfully by {request.user.email}")
            return Response(
                {'message': f'User {user.email} has been blocked.'},
                status=status.HTTP_200_OK
            )

        elif action == 'unblock':
            user.is_blocked = False
            user.is_active = True
            user.save()
            logger.info(f"User {user.email} unblocked successfully by {request.user.email}")
            return Response(
                {'message': f'User {user.email} has been unblocked.'},
                status=status.HTTP_200_OK
            )

        elif action == 'edit':
            old_class = user.class_name
            new_class = request.data.get('class_name')

            # ── Guardian e-mail validation (view-level guard) ──────────────
            #
            # Resolve the student e-mail that will be in effect after the save:
            #   • Use the incoming value when the payload includes 'email'.
            #   • Fall back to the current persisted value otherwise.
            incoming_student_email = request.data.get('email') or user.email
            guardians_data = request.data.get('guardians')  # may be None or a list

            # 1. New/updated guardian list vs student e-mail.
            conflict_response = self._validate_guardian_emails(
                incoming_student_email, guardians_data
            )
            if conflict_response:
                logger.warning(
                    f"Guardian e-mail conflict detected for student {user.email} "
                    f"by {request.user.email}"
                )
                return conflict_response

            # 2. Changing the student's e-mail vs already-saved guardian e-mails.
            if 'email' in request.data and request.data['email'] != user.email:
                conflict_response = self._validate_student_email_vs_existing_guardians(
                    user, incoming_student_email
                )
                if conflict_response:
                    logger.warning(
                        f"New student email {incoming_student_email} conflicts with "
                        f"an existing guardian email for user {user.email} "
                        f"(requested by {request.user.email})"
                    )
                    return conflict_response

            # ── Build a clean mutable copy of the request payload ──────────
            # QueryDict.copy() gives us a mutable version; if the client sends
            # JSON (the common case with DRF), request.data is already a plain
            # dict so we just use it directly.
            if hasattr(request.data, 'copy'):
                mutable_data = request.data.copy()
            else:
                mutable_data = dict(request.data)

            serializer = AdminUserSerializer(user, data=mutable_data, partial=True)

            if serializer.is_valid():
                serializer.save()

                if new_class and new_class != old_class and user.is_student:
                    logger.info(
                        f"Detected class change for student {user.email}: "
                        f"{old_class} -> {new_class}"
                    )
                    self._handle_class_change(user, old_class, new_class)

                logger.info(
                    f"User {user.email} details updated successfully by {request.user.email}"
                )
                return Response(
                    {'message': 'User details updated successfully.'},
                    status=status.HTTP_200_OK
                )

            logger.error(f"Validation errors for user {user.email}: {serializer.errors}")
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

        elif action == 'activate':
            user.is_active = True
            user.save()
            logger.info(
                f"User {user.email} activated successfully by {request.user.email}"
            )
            return Response(
                {'message': f'User {user.email} has been activated.'},
                status=status.HTTP_200_OK
            )

        logger.warning(f"Unknown action received: {action} by {request.user.email}")
        return Response({'error': 'Invalid action.'}, status=status.HTTP_400_BAD_REQUEST)

    # ── Helpers ───────────────────────────────────────────────────────────

    def _handle_class_change(self, user, old_class, new_class):
        """Handle the class change for a student and save ALL historical changes."""
        try:
            StudentClassHistory = apps.get_model('booklist', 'StudentClassHistory')
            current_year = self._get_current_academic_year(user)
            logger.debug(f"Current academic year: {current_year}")

            if old_class != new_class:
                logger.info(
                    f"Recording class change for {user.email}: {old_class} → {new_class}"
                )
                StudentClassHistory.objects.create(
                    student=user,
                    academic_year=current_year,
                    class_name=old_class
                )
                logger.info(f"New class history entry created for {user.email}")
            else:
                logger.debug("No actual class change detected, skipping history update")

        except Exception as e:
            logger.error(f"Error updating class history for {user.email}: {str(e)}")
            raise

    def _get_current_academic_year(self, user):
        """Helper to get current academic year string from user's history or use default."""
        try:
            latest_history = user.class_history.order_by('-created_at').first()
            if latest_history:
                return latest_history.academic_year
            current_year = datetime.now().year
            return f"{current_year}-{current_year + 1}"
        except Exception as e:
            logger.error(f"Error getting current academic year: {str(e)}")
            current_year = datetime.now().year
            return f"{current_year}-{current_year + 1}"

    def delete(self, request, user_id, *args, **kwargs):
        """DELETE — only principals and superusers."""
        if not (request.user.is_superuser or request.user.role == 'principal'):
            logger.warning(
                f"Unauthorized DELETE attempt by user {request.user.email} "
                f"with role {request.user.role}"
            )
            return Response(
                {'error': 'Permission denied. Only principals can delete users.'},
                status=status.HTTP_403_FORBIDDEN
            )

        try:
            user = CustomUser.objects.get(id=user_id)
            logger.info(f"Found user to delete: {user.email}")
        except CustomUser.DoesNotExist:
            logger.error(f"User with ID {user_id} not found for deletion")
            return Response({'error': 'User not found.'}, status=status.HTTP_404_NOT_FOUND)

        email = user.email
        user.delete()
        logger.info(f"User {email} deleted successfully by {request.user.email}")
        return Response(
            {'message': f'User {email} has been deleted successfully.'},
            status=status.HTTP_200_OK
        )


class AdminSignUpView(generics.CreateAPIView):
    permission_classes = [IsPrincipalOrSuperuser]
    serializer_class = AdminUserSerializer

    def create(self, request, *args, **kwargs):
        if getattr(request, 'limited', False):
            return _rate_limited_response()

        if not (request.user.is_superuser or request.user.role == 'principal'):
            return Response(
                {'error': 'Only principals or superusers can create users.'},
                status=status.HTTP_403_FORBIDDEN
            )

        mutable_data = request.data.copy()
        email = mutable_data.get('email')
        role = mutable_data.get('role', 'staff')

        if role not in ['staff', 'principal']:
            return Response(
                {'error': 'Invalid role provided.'},
                status=status.HTTP_400_BAD_REQUEST
            )

        if CustomUser.objects.filter(email=email).exists():
            return Response(
                {'error': 'Email has already been used.'},
                status=status.HTTP_400_BAD_REQUEST
            )

        serializer = self.get_serializer(data=mutable_data)
        serializer.is_valid(raise_exception=True)
        self.perform_create(serializer)

        try:
            user = CustomUser.objects.get(email=email)
        except ObjectDoesNotExist:
            return Response(
                {'error': 'User creation failed.'},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )

        self.send_verification_email(user)

        headers = self.get_success_headers(serializer.data)
        return Response(
            {'message': 'User registration successful. Please check your email for the verification link.'},
            status=status.HTTP_201_CREATED,
            headers=headers
        )

    def send_verification_email(self, user):
        verification_token = RefreshToken.for_user(user).access_token

        verification_url = reverse(
            'admin_auth:verify-email', kwargs={'token': str(verification_token)}
        )
        verification_url = self.request.build_absolute_uri(verification_url)

        email_body = f"""
        <html>
        <body style="font-family: Arial, sans-serif; color: #333;">
            Dear {user.first_name},
            <h2 style="color: #4CAF50;">Welcome to Our Service!</h2>
            <p>Thank you for registering with us. Please click the button below to verify your email address:</p>

            <a href="{verification_url}" style="display: inline-block; padding: 10px 20px;
               background-color: #4CAF50; color: #fff; text-decoration: none;
               border-radius: 5px; font-weight: bold;">
                Verify Your Email
            </a>

            <p>If you did not register for this account, please ignore this email.</p>

            <br>
            <p>Best regards,<br>Your Company Team</p>
        </body>
        </html>
        """

        configuration = Configuration()
        configuration.api_key['api-key'] = settings.BREVO_API_KEY

        api_instance = TransactionalEmailsApi(ApiClient(configuration))

        send_smtp_email = SendSmtpEmail(
            to=[{"email": user.email}],
            sender={"name": "Your Company", "email": settings.DEFAULT_FROM_EMAIL},
            subject="Verify Your Email",
            html_content=email_body
        )

        try:
            api_response = api_instance.send_transac_email(send_smtp_email)
            logger.info(f"Verification email sent to {user.email}: {api_response}")
        except ApiException as e:
            logger.error(f"Exception when sending email: {e}")


class VerifyEmailView(APIView):
    """
    IDOR FIX: user_id removed from URL — identity comes from inside the token only.

    URL change required in urls.py:
        OLD: path('verify-email/<int:user_id>/<str:token>/', ...)
        NEW: path('verify-email/<str:token>/',               ...)
    """

    def get(self, request, token):
        if getattr(request, 'limited', False):
            return Response(
                {'error': 'Too many requests. Please try again later.'},
                status=status.HTTP_429_TOO_MANY_REQUESTS
            )

        try:
            payload = jwt.decode(token, settings.SECRET_KEY, algorithms=['HS256'])
            user_id_from_token = payload.get('user_id')

            if not user_id_from_token:
                return Response(
                    {'error': 'Invalid token.'},
                    status=status.HTTP_400_BAD_REQUEST
                )

            user = get_object_or_404(User, id=user_id_from_token)

            if user.is_active:
                return redirect('http://localhost:5173/dashboard')

            user.is_active = True
            user.save()
            logger.info(f"Email verified for user id={user_id_from_token}")
            return redirect('http://localhost:5173/dashboard')

        except jwt.ExpiredSignatureError:
            logger.error("Activation link has expired.")
            return Response(
                {'error': 'Activation link has expired.'},
                status=status.HTTP_400_BAD_REQUEST
            )

        except jwt.InvalidTokenError:
            logger.error("Invalid activation link.")
            return Response(
                {'error': 'Invalid activation link.'},
                status=status.HTTP_400_BAD_REQUEST
            )


class LoginView(APIView):
    def post(self, request):
        if getattr(request, 'limited', False):
            return _rate_limited_response()

        email = request.data.get('email')
        password = request.data.get('password')

        try:
            user = CustomUser.objects.get(email=email)
        except CustomUser.DoesNotExist:
            return Response(
                {'error': 'Incorrect username or password.'},
                status=status.HTTP_401_UNAUTHORIZED
            )

        if user.role.lower() not in ['principal', 'staff']:
            return Response(
                {'error': 'You are not authorized to access the admin system.'},
                status=status.HTTP_403_FORBIDDEN
            )

        if user.is_blocked:
            return Response(
                {'error': 'Your account has been blocked. Please contact support for assistance.'},
                status=status.HTTP_403_FORBIDDEN
            )

        if not user.is_active:
            return Response(
                {'error': 'Account not verified. Please check your email for the verification link.'},
                status=status.HTTP_401_UNAUTHORIZED
            )

        if check_password(password, user.password):
            login(request, user)
            refresh = RefreshToken.for_user(user)
            return Response({
                'access_token': str(refresh.access_token),
                'refresh_token': str(refresh),
                'user': {
                    'id': user.id,
                    'email': user.email,
                    'role': user.role
                }
            })
        else:
            return Response(
                {'error': 'Incorrect username or password.'},
                status=status.HTTP_401_UNAUTHORIZED
            )


class LogoutView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request):
        logout(request)
        return Response({'success': True, 'message': 'Logged out successfully.'})