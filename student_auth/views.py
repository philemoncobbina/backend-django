from rest_framework import generics, status
from rest_framework.response import Response
from rest_framework.views import APIView
from rest_framework.permissions import IsAuthenticated, AllowAny
from rest_framework_simplejwt.tokens import RefreshToken
from django.contrib.auth import login
from django.contrib.auth.hashers import check_password
from django.urls import reverse
from django.shortcuts import get_object_or_404, redirect
from django.core.exceptions import ObjectDoesNotExist
import jwt
import logging

from authapp.models import CustomUser
from .serializers import StudentUserSerializer
from .permissions import IsTeacherOrPrincipalOrSuperuser
from sib_api_v3_sdk import Configuration, ApiClient, SendSmtpEmail
from sib_api_v3_sdk.api.transactional_emails_api import TransactionalEmailsApi
from sib_api_v3_sdk.rest import ApiException
from django.conf import settings

from django.db.models.signals import pre_save
from django.dispatch import receiver

User = get_user_model() if False else None  # resolved below via CustomUser directly

@receiver(pre_save, sender=CustomUser)
def set_is_active_to_false(sender, instance, **kwargs):
    if instance._state.adding:
        instance.is_active = False


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

class StudentSignUpView(generics.CreateAPIView):
    permission_classes = [IsTeacherOrPrincipalOrSuperuser]
    serializer_class = StudentUserSerializer

    def create(self, request, *args, **kwargs):
        # Rate limit: 20 student creations per hour per IP
        if getattr(request, 'limited', False):
            return _rate_limited_response()

        if not (request.user.is_superuser or request.user.role in ['principal', 'staff']):
            return Response({'error': 'Only principals, staff, or superusers can create student accounts.'},
                           status=status.HTTP_403_FORBIDDEN)

        mutable_data = request.data.copy()

        email = mutable_data.get('email')
        index_number = mutable_data.get('index_number')
        class_name = mutable_data.get('class_name')

        if not email or not index_number or not class_name:
            return Response({'error': 'Email, index number, and class name are required.'},
                           status=status.HTTP_400_BAD_REQUEST)

        if CustomUser.objects.filter(email=email).exists():
            return Response({'error': 'Email has already been used.'}, status=status.HTTP_400_BAD_REQUEST)

        mutable_data['role'] = 'student'

        if not mutable_data.get('username'):
            mutable_data['username'] = index_number.lower()

        serializer = self.get_serializer(data=mutable_data)
        serializer.is_valid(raise_exception=True)
        self.perform_create(serializer)

        try:
            user = CustomUser.objects.get(email=email)
        except ObjectDoesNotExist:
            return Response({'error': 'Student creation failed.'}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

        self.send_verification_email(user)

        headers = self.get_success_headers(serializer.data)
        return Response(
            {'message': 'Student registration successful. Please check email for the verification link.'},
            status=status.HTTP_201_CREATED,
            headers=headers
        )

    def send_verification_email(self, user):
        verification_token = RefreshToken.for_user(user).access_token

        # IDOR FIX: user_id removed from URL — identity comes from inside the token only
        verification_url = reverse('student-verify-email', kwargs={'token': str(verification_token)})
        verification_url = self.request.build_absolute_uri(verification_url)

        raw_password = self.request.data.get('password')

        email_body = f"""
        <html>
        <body style="font-family: Arial, sans-serif; color: #333;">
            Dear {user.first_name},
            <h2 style="color: #4CAF50;">Welcome to Our School System!</h2>
            <p>Your student account has been created. Please click the button below to verify your email address:</p>

            <a href="{verification_url}" style="display: inline-block; padding: 10px 20px; background-color: #4CAF50; color: #fff; text-decoration: none; border-radius: 5px; font-weight: bold;">
                Verify Your Email
            </a>

            <p>Your account details:</p>
            <ul>
                <li>Username: {user.username}</li>
                <li>Email: {user.email}</li>
                <li>Index Number: {user.index_number}</li>
                <li>Class: {user.get_class_name_display()}</li>
                <li>Password: {raw_password}</li>
            </ul>

            <p>Please keep these credentials safe and secure. After logging in for the first time, we recommend changing your password.</p>
            <p>If you did not expect this email, please contact the school administration.</p>

            <br>
            <p>Best regards,<br>School Administration</p>
        </body>
        </html>
        """

        configuration = Configuration()
        configuration.api_key['api-key'] = settings.BREVO_API_KEY

        api_instance = TransactionalEmailsApi(ApiClient(configuration))

        send_smtp_email = SendSmtpEmail(
            to=[{"email": user.email}],
            sender={"name": "School Admin", "email": settings.DEFAULT_FROM_EMAIL},
            subject="Verify Your Student Account",
            html_content=email_body
        )

        try:
            api_response = api_instance.send_transac_email(send_smtp_email)
            logger.info(f"Verification email sent to student {user.email}: {api_response}")
        except ApiException as e:
            logger.error(f"Exception when sending email to student: {e}")


class StudentVerifyEmailView(APIView):
    """
    IDOR FIX
    --------
    The original endpoint accepted `user_id` directly from the URL, which allowed
    an attacker to tamper with it and activate arbitrary student accounts.

    Fix: `user_id` is removed from the URL entirely. The JWT token already contains
    the authoritative `user_id` in its payload. We decode the token first and look
    up the user from that — the URL carries only the token, nothing else.

    URL change required in urls.py:
        OLD: path('verify-email/<int:user_id>/<str:token>/', ...)
        NEW: path('verify-email/<str:token>/',               ...)
    """

    def get(self, request, token):
        # Rate limit: 10 per hour per IP (set by middleware)
        if getattr(request, 'limited', False):
            return Response(
                {'error': 'Too many requests. Please try again later.'},
                status=status.HTTP_429_TOO_MANY_REQUESTS
            )

        try:
            # Decode token — this is the ONLY source of truth for user identity
            payload = jwt.decode(token, settings.SECRET_KEY, algorithms=['HS256'])
            user_id_from_token = payload.get('user_id')

            if not user_id_from_token:
                return Response({'error': 'Invalid token.'}, status=status.HTTP_400_BAD_REQUEST)

            # Look up user purely from the token payload — no URL parameter involved
            user = get_object_or_404(CustomUser, id=user_id_from_token)

            if user.is_active:
                return redirect('http://localhost:5173/student-dashboard')

            user.is_active = True
            user.save()
            logger.info(f"Student email verified for user id={user_id_from_token}")
            return redirect('http://localhost:5173/student-dashboard')

        except jwt.ExpiredSignatureError:
            logger.error("Activation link has expired.")
            return Response({'error': 'Activation link has expired.'}, status=status.HTTP_400_BAD_REQUEST)

        except jwt.InvalidTokenError:
            logger.error("Invalid activation link.")
            return Response({'error': 'Invalid activation link.'}, status=status.HTTP_400_BAD_REQUEST)


class StudentLoginView(APIView):
    permission_classes = [AllowAny]

    def post(self, request):
        # Rate limit: 5 per minute per IP (set by middleware)
        if getattr(request, 'limited', False):
            return _rate_limited_response()

        email = request.data.get('email')
        password = request.data.get('password')
        index_number = request.data.get('index_number')

        try:
            if email:
                user = CustomUser.objects.get(email=email)
            elif index_number:
                user = CustomUser.objects.get(index_number=index_number)
            else:
                return Response({'error': 'Please provide either an email or index number.'},
                              status=status.HTTP_400_BAD_REQUEST)
        except CustomUser.DoesNotExist:
            return Response({'error': 'Incorrect login credentials.'}, status=status.HTTP_401_UNAUTHORIZED)

        if user.role != 'student':
            return Response({'error': 'This login is for students only.'}, status=status.HTTP_403_FORBIDDEN)

        if user.is_blocked:
            return Response({'error': 'Your account has been blocked. Please contact school administration for assistance.'},
                          status=status.HTTP_403_FORBIDDEN)

        if not user.is_active:
            return Response({'error': 'Account not verified. Please check your email for the verification link.'},
                          status=status.HTTP_401_UNAUTHORIZED)

        if check_password(password, user.password):
            login(request, user)
            refresh = RefreshToken.for_user(user)
            return Response({
                'access_token': str(refresh.access_token),
                'refresh_token': str(refresh),
                'user': {
                    'id': user.id,
                    'email': user.email,
                    'index_number': user.index_number,
                    'class_name': user.class_name,
                    'first_name': user.first_name,
                    'last_name': user.last_name,
                    'role': user.role
                }
            })
        else:
            return Response({'error': 'Incorrect login credentials.'}, status=status.HTTP_401_UNAUTHORIZED)


class BatchStudentCreationView(generics.CreateAPIView):
    """API view for creating multiple student accounts at once from a CSV or data list."""
    permission_classes = [IsTeacherOrPrincipalOrSuperuser]
    serializer_class = StudentUserSerializer

    def create(self, request, *args, **kwargs):
        # Rate limit: 5 batch operations per hour per IP (set by middleware)
        if getattr(request, 'limited', False):
            return _rate_limited_response()

        if not (request.user.is_superuser or request.user.role in ['principal', 'staff']):
            return Response({'error': 'Only principals, staff, or superusers can create student accounts.'},
                          status=status.HTTP_403_FORBIDDEN)

        students_data = request.data.get('students', [])
        if not students_data or not isinstance(students_data, list):
            return Response({'error': 'Please provide a list of student data.'},
                          status=status.HTTP_400_BAD_REQUEST)

        created_students = []
        errors = []

        for i, student_data in enumerate(students_data):
            if not student_data.get('username') and student_data.get('index_number'):
                student_data['username'] = student_data['index_number'].lower()

            student_data['role'] = 'student'

            serializer = self.get_serializer(data=student_data)
            if serializer.is_valid():
                try:
                    user = serializer.save()
                    self.send_verification_email(user)
                    created_students.append({
                        'index_number': user.index_number,
                        'email': user.email,
                        'class_name': user.class_name
                    })
                except Exception as e:
                    errors.append({'index': i, 'error': str(e), 'data': student_data})
            else:
                errors.append({'index': i, 'error': serializer.errors, 'data': student_data})

        return Response({
            'message': f'Successfully created {len(created_students)} student accounts.',
            'created_students': created_students,
            'errors': errors
        }, status=status.HTTP_201_CREATED if created_students else status.HTTP_400_BAD_REQUEST)

    def send_verification_email(self, user):
        verification_token = RefreshToken.for_user(user).access_token

        # IDOR FIX: user_id removed from URL — identity comes from inside the token only
        verification_url = reverse('student-verify-email', kwargs={'token': str(verification_token)})
        verification_url = self.request.build_absolute_uri(verification_url)

        email_body = f"""
        <html>
        <body style="font-family: Arial, sans-serif; color: #333;">
            Dear {user.first_name},
            <h2 style="color: #4CAF50;">Welcome to Our School System!</h2>
            <p>Your student account has been created. Please click the button below to verify your email address:</p>

            <a href="{verification_url}" style="display: inline-block; padding: 10px 20px; background-color: #4CAF50; color: #fff; text-decoration: none; border-radius: 5px; font-weight: bold;">
                Verify Your Email
            </a>

            <p>Your account details:</p>
            <ul>
                <li>Index Number: {user.index_number}</li>
                <li>Class: {user.get_class_name_display()}</li>
            </ul>

            <p>If you did not expect this email, please contact the school administration.</p>

            <br>
            <p>Best regards,<br>School Administration</p>
        </body>
        </html>
        """

        configuration = Configuration()
        configuration.api_key['api-key'] = settings.BREVO_API_KEY

        api_instance = TransactionalEmailsApi(ApiClient(configuration))

        send_smtp_email = SendSmtpEmail(
            to=[{"email": user.email}],
            sender={"name": "School Admin", "email": settings.DEFAULT_FROM_EMAIL},
            subject="Verify Your Student Account",
            html_content=email_body
        )

        try:
            api_response = api_instance.send_transac_email(send_smtp_email)
            logger.info(f"Verification email sent to student {user.email}: {api_response}")
        except ApiException as e:
            logger.error(f"Exception when sending email to student: {e}")



