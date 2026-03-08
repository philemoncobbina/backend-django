import base64
import logging
import os
import platform
import threading
from pathlib import Path

import jwt
import requests
from django.conf import settings
from django.contrib.auth import (
    authenticate, get_user_model, login, logout,
    hashers
)
from django.contrib.auth.hashers import check_password, make_password
from django.core.exceptions import ObjectDoesNotExist
from django.core.mail import EmailMultiAlternatives, send_mail
from django.db import IntegrityError
from django.shortcuts import get_object_or_404, redirect, render
from django.template.loader import render_to_string, get_template
from django.urls import reverse
from django.utils import timezone
from django.utils.crypto import get_random_string
from django.views.decorators.http import require_http_methods
from django_ratelimit.decorators import ratelimit
from django_ratelimit.exceptions import Ratelimited
from dotenv import load_dotenv
from geoip2 import database
from google.auth.transport import requests as google_requests
from google.oauth2 import id_token
from rest_framework import generics, permissions, status
from rest_framework.response import Response
from rest_framework.views import APIView
from rest_framework_simplejwt.tokens import RefreshToken
from rest_framework_social_oauth2.views import ConvertTokenView
from sib_api_v3_sdk import (
    ApiClient, Configuration, SendSmtpEmail,
    TransactionalEmailsApi
)
from sib_api_v3_sdk.rest import ApiException
from social_core.backends.google import GoogleOAuth2
from social_core.exceptions import AuthException
from social_django.utils import load_backend, load_strategy

from authapp.models import CustomUser
from .models import CustomUser
from .serializers import (
    ChangePasswordRequestSerializer, ChangePasswordSerializer,
    CustomUserSerializer, GoogleSignInSerializer,
    PasswordResetConfirmSerializer, PasswordResetSerializer
)

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
# Shared password reset email helper
# ---------------------------------------------------------------------------

def _send_password_reset_verification_email(subject, context, to_email):
    """Shared logic for sending a password reset verification code email via Brevo."""
    html_content = render_to_string('password_reset_verification.html', context)

    configuration = Configuration()
    configuration.api_key['api-key'] = settings.BREVO_API_KEY

    api_instance = TransactionalEmailsApi(ApiClient(configuration))

    send_smtp_email = SendSmtpEmail(
        to=[{"email": to_email}],
        sender={"name": "Your Company", "email": settings.DEFAULT_FROM_EMAIL},
        subject=subject,
        html_content=html_content
    )

    try:
        api_response = api_instance.send_transac_email(send_smtp_email)
        logger.info(f"Verification email sent to {to_email}: {api_response}")
    except ApiException as e:
        logger.error(f"Exception when sending email: {e}")


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

    # Rate limit: max 5 sign-up attempts per hour per IP
    def create(self, request, *args, **kwargs):
        # Apply rate limiting manually so we stay inside DRF's class-based flow
        decorator = ratelimit(key='ip', rate='5/h', method='POST', block=False)
        limited = getattr(decorator(lambda r: r)(request), 'limited', False)
        # django-ratelimit sets request.limited when block=False
        if getattr(request, 'limited', False):
            return _rate_limited_response()

        # Make request data mutable
        mutable_data = request.data.copy()

        email = mutable_data.get('email')

        # Check if the email already exists
        if CustomUser.objects.filter(email=email).exists():
            print(f"[INFO] Email {email} already exists in the database.")
            return Response({'error': 'Email has already been used.'}, status=status.HTTP_400_BAD_REQUEST)

        # Modify the mutable data to set the user as inactive initially
        mutable_data['is_active'] = False
        request._mutable_data = mutable_data
        print(f"[INFO] User data modified, setting is_active=False for email: {email}")

        # Perform the user creation
        print(f"[INFO] Attempting to create user with email: {email}")
        serializer = self.get_serializer(data=mutable_data)
        serializer.is_valid(raise_exception=True)
        self.perform_create(serializer)

        # Try to get the user instance that was just created
        try:
            user = CustomUser.objects.get(email=email)
            print(f"[INFO] User {email} successfully created with ID {user.id}.")
        except ObjectDoesNotExist:
            print(f"[ERROR] Failed to find user {email} after creation attempt.")
            return Response({'error': 'User creation failed.'}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

        # Asynchronously send verification email
        print(f"[INFO] Starting verification email thread for user {email}.")
        threading.Thread(target=self.send_verification_email, args=(user, request)).start()

        # Return a success response
        response = Response(serializer.data, status=status.HTTP_201_CREATED)
        response.data['message'] = 'User registration successful. Please check your email for the verification link.'
        print(f"[INFO] Registration successful for user {email}. Returning response to client.")
        return response

    def send_verification_email(self, user, request):
        print(f"[INFO] Preparing to send verification email to {user.email}...")
        try:
            # Generate the verification token and URL
            verification_token = RefreshToken.for_user(user).access_token
            # IDOR FIX: user_id is NOT embedded in the URL; the token alone identifies the user
            verification_url = reverse('verify-email', kwargs={'token': str(verification_token)})
            verification_url = request.build_absolute_uri(verification_url)
            print(f"[INFO] Verification URL generated for {user.email}: {verification_url}")

            # Render the HTML content from the template
            context = {'verification_url': verification_url}
            html_content = render_to_string('email_verification.html', context)
            print(f"[INFO] HTML content rendered for email verification for {user.email}.")

            # Brevo email sending logic
            configuration = Configuration()
            configuration.api_key['api-key'] = os.getenv('BREVO_API_KEY')
            api_instance = TransactionalEmailsApi(ApiClient(configuration))

            send_smtp_email = SendSmtpEmail(
                to=[{"email": user.email}],
                sender={"name": "Your Company", "email": settings.DEFAULT_FROM_EMAIL},
                subject="Verify Your Email",
                html_content=html_content
            )

            print(f"[INFO] Attempting to send email to {user.email}...")
            api_instance.send_transac_email(send_smtp_email)
            print(f"[INFO] Verification email successfully sent to {user.email}")

        except ApiException as e:
            print(f"[ERROR] Exception when sending email to {user.email}: {e}")
        except Exception as e:
            print(f"[ERROR] Unexpected error when preparing or sending email to {user.email}: {e}")


class VerifyEmailView(APIView):
    """
    IDOR FIX
    --------
    The original endpoint accepted `user_id` directly from the URL, allowing an
    attacker to tamper with it and activate arbitrary accounts.

    The fix: drop `user_id` from the URL entirely. The JWT token already contains
    the `user_id` in its payload (added by SimpleJWT via the `user_id` claim).
    We decode the token to extract the authoritative user_id and look up the user
    from that — the URL parameter is no longer trusted or needed.

    URL change required in urls.py:
        OLD: path('verify-email/<int:user_id>/<str:token>/', VerifyEmailView.as_view(), name='verify-email')
        NEW: path('verify-email/<str:token>/', VerifyEmailView.as_view(), name='verify-email')
    """

    # Rate limit: 10 verification attempts per hour per IP to prevent enumeration
    def get(self, request, token):
        if getattr(request, 'limited', False):
            return Response(
                {'error': 'Too many requests. Please try again later.'},
                status=status.HTTP_429_TOO_MANY_REQUESTS
            )

        try:
            # Decode the token — this is the ONLY source of truth for user identity
            payload = jwt.decode(token, settings.SECRET_KEY, algorithms=['HS256'])
            user_id_from_token = payload.get('user_id')

            if not user_id_from_token:
                return Response({'error': 'Invalid token.'}, status=status.HTTP_400_BAD_REQUEST)

            # Look up user purely from the token payload — no URL parameter involved
            user = get_object_or_404(User, id=user_id_from_token)

            if user.is_active:
                # Already verified — redirect gracefully
                return redirect('https://plvcmonline.uk/login')

            user.is_active = True
            user.save()
            logger.info(f"Email verified for user id={user_id_from_token}")
            return redirect('https://plvcmonline.uk/login')

        except jwt.ExpiredSignatureError:
            logger.error("Activation link has expired.")
            return Response({'error': 'Activation link has expired.'}, status=status.HTTP_400_BAD_REQUEST)

        except jwt.InvalidTokenError:
            logger.error("Invalid activation link.")
            return Response({'error': 'Invalid activation link.'}, status=status.HTTP_400_BAD_REQUEST)


class GoogleSignInView(APIView):
    # Rate limit: 10 attempts per minute per IP
    def post(self, request):
        if getattr(request, 'limited', False):
            return _rate_limited_response()

        print("=== START GOOGLE SIGN-IN PROCESS ===")
        print("Received request data:", request.data)

        serializer = GoogleSignInSerializer(data=request.data)
        if serializer.is_valid():
            access_token = serializer.validated_data['access_token']
            print("✅ Access token received:", access_token)

            try:
                # Step 1: Verify ID Token
                id_token_info = self.get_id_token_from_access_token(access_token)
                if id_token_info:
                    print("✅ ID Token Info:", id_token_info)
                    email = id_token_info.get('email')
                    print("📧 Email extracted:", email)

                    if not email:
                        print("❌ ERROR: No email found in token")
                        return Response({
                            'success': False,
                            'error': 'Email not found in token'
                        }, status=status.HTTP_400_BAD_REQUEST)

                    # Step 2: Fetch User Profile with Multiple Methods
                    user_profile = self.get_user_profile_from_google(access_token)
                    print("👤 User Profile Fetched:", user_profile)

                    # Comprehensive Name Extraction
                    first_name = (
                        user_profile.get('given_name') or
                        user_profile.get('first_name') or
                        (user_profile.get('names', [{}])[0] if user_profile.get('names') else {}).get('givenName', '') or
                        ''
                    )
                    last_name = (
                        user_profile.get('family_name') or
                        user_profile.get('last_name') or
                        (user_profile.get('names', [{}])[0] if user_profile.get('names') else {}).get('familyName', '') or
                        ''
                    )

                    print(f"👥 Extracted Names - First: '{first_name}', Last: '{last_name}'")

                    # Use the custom user model
                    User = get_user_model()
                    user, created = User.objects.get_or_create(email=email)

                    if created:
                        print("🆕 New user created")
                        user.is_google_account = True
                        user.is_active = True
                        user.is_blocked = False
                        user.date_joined = timezone.now()
                        user.save()
                        print(f"👤 User created with Date Joined: '{user.date_joined}'")

                    # Update the first and last name regardless of whether the user was newly created
                    user.first_name = first_name
                    user.last_name = last_name
                    user.last_login = timezone.now()
                    user.save()
                    print(f"👤 User saved with First Name: '{user.first_name}', Last Name: '{user.last_name}', Last Login: '{user.last_login}'")

                    # Additional User Checks
                    if user.is_blocked:
                        print("🚫 User account is blocked")
                        return Response({
                            'success': False,
                            'error': 'User account is blocked. Please contact support.'
                        }, status=status.HTTP_403_FORBIDDEN)

                    if not user.is_active:
                        print("❌ User account is inactive")
                        return Response({
                            'success': False,
                            'error': 'User account is inactive. Please verify your email or contact support.'
                        }, status=status.HTTP_403_FORBIDDEN)

                    if not user.is_google_account:
                        print("❌ Not a Google account")
                        return Response({
                            'success': False,
                            'error': 'Account was not created with Gmail. Please login with your email and password'
                        }, status=status.HTTP_403_FORBIDDEN)

                    # Generate JWT tokens
                    refresh = RefreshToken.for_user(user)
                    print("🔑 JWT tokens generated successfully")

                    return Response({
                        'success': True,
                        'email': email,
                        'created': created,
                        'first_name': user.first_name,
                        'last_name': user.last_name,
                        'access': str(refresh.access_token),
                        'refresh': str(refresh),
                    })

                else:
                    print("❌ ERROR: Invalid or expired token")
                    return Response({
                        'success': False,
                        'error': 'Invalid or expired token'
                    }, status=status.HTTP_400_BAD_REQUEST)

            except Exception as e:
                print(f"❌ UNEXPECTED ERROR: {str(e)}")
                return Response({
                    'success': False,
                    'error': str(e)
                }, status=status.HTTP_400_BAD_REQUEST)

        print("❌ Serializer validation failed")
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

    def get_id_token_from_access_token(self, access_token):
        try:
            print("🔍 Fetching ID token info...")
            response = requests.get(
                "https://www.googleapis.com/oauth2/v3/tokeninfo",
                params={"access_token": access_token}
            )
            print(f"ID Token Response Status: {response.status_code}")
            print(f"ID Token Response Content: {response.text}")

            if response.status_code == 200:
                return response.json()
            return None
        except Exception as e:
            print(f"❌ Error while fetching ID token: {e}")
            return None

    def get_user_profile_from_google(self, access_token):
        print("🌐 Attempting to fetch user profile...")

        # Method 1: UserInfo Endpoint
        try:
            print("🔍 Trying UserInfo Endpoint...")
            response = requests.get(
                "https://www.googleapis.com/oauth2/v3/userinfo",
                headers={'Authorization': f'Bearer {access_token}'}
            )
            print(f"UserInfo Response Status: {response.status_code}")
            print(f"UserInfo Response Content: {response.text}")

            if response.status_code == 200:
                user_info = response.json()
                print("✅ Successfully retrieved UserInfo")
                return user_info
        except Exception as e:
            print(f"❌ UserInfo Endpoint Error: {e}")

        # Method 2: People API
        try:
            print("🔍 Trying People API...")
            response = requests.get(
                "https://people.googleapis.com/v1/people/me?personFields=names,emailAddresses",
                headers={'Authorization': f'Bearer {access_token}'}
            )
            print(f"People API Response Status: {response.status_code}")
            print(f"People API Response Content: {response.text}")

            if response.status_code == 200:
                people_data = response.json()
                print("✅ Successfully retrieved People API data")
                return people_data
        except Exception as e:
            print(f"❌ People API Error: {e}")

        print("❌ Failed to retrieve user profile")
        return {}


def get_location_data():
    """Get IP address and location information using ipinfo.io API"""
    try:
        token = os.getenv('IPINFO_TOKEN', '')
        url = f'https://ipinfo.io/json?token={token}'
        response = requests.get(url)

        if response.status_code == 200:
            data = response.json()
            print(f"Location data fetched: {data}")
            return {
                'ip': data.get('ip', 'N/A'),
                'city': data.get('city', 'N/A'),
                'country': data.get('country', 'N/A'),
                'region': data.get('region', 'N/A'),
                'loc': data.get('loc', 'N/A'),
            }
        else:
            print(f"Failed to fetch location data: {response.status_code}")
            return {'ip': 'N/A', 'city': 'N/A', 'country': 'N/A', 'region': 'N/A', 'loc': 'N/A'}
    except Exception as e:
        print(f"Error fetching location data: {e}")
        return {'ip': 'N/A', 'city': 'N/A', 'country': 'N/A', 'region': 'N/A', 'loc': 'N/A'}


def send_login_email(user, request, location_data, device_info):
    try:
        verification_token = RefreshToken.for_user(user).access_token
        # IDOR FIX: consistent with the new single-parameter verify-email URL
        verification_url = reverse('verify-email', kwargs={'token': str(verification_token)})
        verification_url = request.build_absolute_uri(verification_url)

        context = {
            'verification_url': verification_url,
            'first_name': user.first_name,
            'city': location_data.get('city'),
            'country_name': location_data.get('country'),
            'ip_address': location_data.get('ip'),
            'device_os': device_info.get('os'),
            'device_name': device_info.get('name'),
        }
        html_content = render_to_string('login_alert.html', context)

        configuration = Configuration()
        configuration.api_key['api-key'] = os.getenv('BREVO_API_KEY')
        api_instance = TransactionalEmailsApi(ApiClient(configuration))

        send_smtp_email = SendSmtpEmail(
            to=[{"email": user.email, "name": user.first_name}],
            sender={"name": "Your Company", "email": settings.DEFAULT_FROM_EMAIL},
            subject="New Login Alert",
            html_content=html_content
        )

        api_instance.send_transac_email(send_smtp_email)
        print(f"Email sent successfully to: {user.email}")
    except ApiException as e:
        print(f"Error sending email to {user.email}: {e}")


class LoginView(APIView):
    # Rate limit: 5 login attempts per minute per IP
    def post(self, request):
        if getattr(request, 'limited', False):
            return _rate_limited_response()

        email = request.data.get('email')
        password = request.data.get('password')
        print(f"Login attempt for email: {email}")

        try:
            user = CustomUser.objects.get(email=email)
            print(f"User found: {user.email}")
        except CustomUser.DoesNotExist:
            print(f"User not found with email: {email}")
            return Response({'error': 'Incorrect username or password.'}, status=status.HTTP_401_UNAUTHORIZED)

        # Check various account conditions
        if user.is_blocked:
            return Response({'error': 'Your account has been blocked.'}, status=status.HTTP_403_FORBIDDEN)

        if user.is_google_account:
            return Response({'error': 'Your account was created with Google. Please login with your Google account'},
                           status=status.HTTP_403_FORBIDDEN)

        if not user.is_active:
            return Response({'error': 'Account not verified.'}, status=status.HTTP_401_UNAUTHORIZED)

        # Check user role
        if user.role in ['principal', 'staff']:
            return Response({
                'error': 'You do not have access to this system. Please use the staff portal.'
            }, status=status.HTTP_403_FORBIDDEN)

        if user.role == 'student':
            return Response({
                'error': 'Please login through the student portal.'
            }, status=status.HTTP_403_FORBIDDEN)

        if check_password(password, user.password):
            # Authentication successful
            login(request, user)
            refresh = RefreshToken.for_user(user)

            # Get device information
            device_info = {
                'os': platform.system(),
                'name': platform.node()
            }

            # Send login alert email in background thread
            def send_login_notification():
                location_data = get_location_data()
                send_login_email(user, request, location_data, device_info)

            threading.Thread(target=send_login_notification).start()

            # Return user data and tokens
            user_data = CustomUserSerializer(user).data
            return Response({
                'access_token': str(refresh.access_token),
                'refresh_token': str(refresh),
                'user': user_data,
            }, status=status.HTTP_200_OK)
        else:
            return Response({'error': 'Incorrect username or password.'}, status=status.HTTP_401_UNAUTHORIZED)


# ---------------------------------------------------------------------------
# Password Reset — Student Portal
# Allowed roles: student only
# ---------------------------------------------------------------------------

class StudentPasswordResetView(APIView):
    """
    Password reset endpoint for the student portal.
    Rejects the request if the email does not belong to a user with role='student'.
    """
    permission_classes = [permissions.AllowAny]
    serializer_class = PasswordResetSerializer

    # Rate limit: 5 password-reset requests per hour per IP
    def get(self, request, *args, **kwargs):
        if getattr(request, 'limited', False):
            return _rate_limited_response()

        email = request.query_params.get('email')
        if not email:
            return Response({'error': 'Email parameter is missing.'}, status=status.HTTP_400_BAD_REQUEST)

        user = User.objects.filter(email=email).first()
        if user and user.is_active and user.role == 'student':
            return Response({'message': 'Email is registered.'}, status=status.HTTP_200_OK)
        return Response(
            {'error': 'Email not registered, not active, or does not belong to a student account.'},
            status=status.HTTP_400_BAD_REQUEST
        )

    def post(self, request, *args, **kwargs):
        if getattr(request, 'limited', False):
            return _rate_limited_response()

        serializer = self.serializer_class(data=request.data)
        if not serializer.is_valid():
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

        email = serializer.validated_data['email']
        user = User.objects.filter(email=email).first()

        if not user or not user.is_active:
            return Response(
                {'error': 'Email not registered or not active.'},
                status=status.HTTP_400_BAD_REQUEST
            )

        if user.role != 'student':
            return Response(
                {'error': 'This endpoint is only for student accounts. Please use the correct portal.'},
                status=status.HTTP_403_FORBIDDEN
            )

        verification_code = get_random_string(length=6, allowed_chars='0123456789')
        user.verification_code = verification_code
        user.save()

        context = {'verification_code': verification_code}
        _send_password_reset_verification_email(
            subject='Password Reset Verification Code',
            context=context,
            to_email=email
        )

        logger.info(f"[Student] Password reset verification code sent to {email}.")
        return Response({'message': 'Verification code sent to your email.'}, status=status.HTTP_200_OK)


# ---------------------------------------------------------------------------
# Password Reset — Admin Portal
# Allowed roles: principal, staff
# ---------------------------------------------------------------------------

class AdminPasswordResetView(APIView):
    """
    Password reset endpoint for the admin portal.
    Rejects the request if the email does not belong to a user with role 'principal' or 'staff'.
    """
    permission_classes = [permissions.AllowAny]
    serializer_class = PasswordResetSerializer

    # Rate limit: 5 password-reset requests per hour per IP
    def get(self, request, *args, **kwargs):
        if getattr(request, 'limited', False):
            return _rate_limited_response()

        email = request.query_params.get('email')
        if not email:
            return Response({'error': 'Email parameter is missing.'}, status=status.HTTP_400_BAD_REQUEST)

        user = User.objects.filter(email=email).first()
        if user and user.is_active and user.role in ['principal', 'staff']:
            return Response({'message': 'Email is registered.'}, status=status.HTTP_200_OK)
        return Response(
            {'error': 'Email not registered, not active, or does not belong to an admin account.'},
            status=status.HTTP_400_BAD_REQUEST
        )

    def post(self, request, *args, **kwargs):
        if getattr(request, 'limited', False):
            return _rate_limited_response()

        serializer = self.serializer_class(data=request.data)
        if not serializer.is_valid():
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

        email = serializer.validated_data['email']
        user = User.objects.filter(email=email).first()

        if not user or not user.is_active:
            return Response(
                {'error': 'Email not registered or not active.'},
                status=status.HTTP_400_BAD_REQUEST
            )

        if user.role not in ['principal', 'staff']:
            return Response(
                {'error': 'This endpoint is only for admin accounts (principal or staff). Please use the correct portal.'},
                status=status.HTTP_403_FORBIDDEN
            )

        verification_code = get_random_string(length=6, allowed_chars='0123456789')
        user.verification_code = verification_code
        user.save()

        context = {'verification_code': verification_code}
        _send_password_reset_verification_email(
            subject='Password Reset Verification Code',
            context=context,
            to_email=email
        )

        logger.info(f"[Admin] Password reset verification code sent to {email}.")
        return Response({'message': 'Verification code sent to your email.'}, status=status.HTTP_200_OK)


# ---------------------------------------------------------------------------
# Password Reset — School Website
# No specific role required — but users with any defined role are rejected.
# This is for general/public accounts that have no assigned role.
# ---------------------------------------------------------------------------

class WebsitePasswordResetView(APIView):
    """
    Password reset endpoint for the main school website.
    Rejects the request if the user has any of the defined roles
    (principal, staff, or student) — those users must use their
    respective portals.
    """
    permission_classes = [permissions.AllowAny]
    serializer_class = PasswordResetSerializer

    # Rate limit: 5 password-reset requests per hour per IP
    def get(self, request, *args, **kwargs):
        if getattr(request, 'limited', False):
            return _rate_limited_response()

        email = request.query_params.get('email')
        if not email:
            return Response({'error': 'Email parameter is missing.'}, status=status.HTTP_400_BAD_REQUEST)

        user = User.objects.filter(email=email).first()
        if user and user.is_active and user.role not in ['principal', 'staff', 'student']:
            return Response({'message': 'Email is registered.'}, status=status.HTTP_200_OK)
        return Response(
            {'error': 'Email not registered, not active, or must use a dedicated portal.'},
            status=status.HTTP_400_BAD_REQUEST
        )

    def post(self, request, *args, **kwargs):
        if getattr(request, 'limited', False):
            return _rate_limited_response()

        serializer = self.serializer_class(data=request.data)
        if not serializer.is_valid():
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

        email = serializer.validated_data['email']
        user = User.objects.filter(email=email).first()

        if not user or not user.is_active:
            return Response(
                {'error': 'Email not registered or not active.'},
                status=status.HTTP_400_BAD_REQUEST
            )

        if user.role in ['principal', 'staff', 'student']:
            return Response(
                {'error': 'Please use your designated portal to reset your password.'},
                status=status.HTTP_403_FORBIDDEN
            )

        verification_code = get_random_string(length=6, allowed_chars='0123456789')
        user.verification_code = verification_code
        user.save()

        context = {'verification_code': verification_code}
        _send_password_reset_verification_email(
            subject='Password Reset Verification Code',
            context=context,
            to_email=email
        )

        logger.info(f"[Website] Password reset verification code sent to {email}.")
        return Response({'message': 'Verification code sent to your email.'}, status=status.HTTP_200_OK)


# ---------------------------------------------------------------------------
# Password Reset Confirm & Verify Code (shared across all portals)
# ---------------------------------------------------------------------------

class PasswordResetConfirmView(APIView):
    permission_classes = [permissions.AllowAny]
    serializer_class = PasswordResetConfirmSerializer

    # Rate limit: 5 attempts per 15 minutes per IP
    def post(self, request, *args, **kwargs):
        if getattr(request, 'limited', False):
            return _rate_limited_response()

        serializer = self.serializer_class(data=request.data)
        if serializer.is_valid():
            email = serializer.validated_data['email']
            verification_code = serializer.validated_data['verification_code']
            new_password = serializer.validated_data['new_password']
            user = User.objects.filter(email=email, verification_code=verification_code).first()
            if user:
                user.password = make_password(new_password)
                user.verification_code = None
                user.save()
                logger.info(f"Password successfully reset for {email}.")
                return Response({'message': 'Password reset successful.'}, status=status.HTTP_200_OK)
            return Response({'error': 'Invalid verification code.'}, status=status.HTTP_400_BAD_REQUEST)
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)


class VerifyResetCodeView(APIView):
    permission_classes = [permissions.AllowAny]

    # Rate limit: 10 attempts per 15 minutes per IP
    def post(self, request, *args, **kwargs):
        if getattr(request, 'limited', False):
            return _rate_limited_response()

        email = request.data.get('email')
        verification_code = request.data.get('verification_code')
        user = User.objects.filter(email=email, verification_code=verification_code).first()
        if user:
            return Response({'message': 'Verification code is valid.'}, status=status.HTTP_200_OK)
        return Response({'error': 'Invalid verification code.'}, status=status.HTTP_400_BAD_REQUEST)


class ChangePasswordRequestView(APIView):
    permission_classes = [permissions.IsAuthenticated]
    serializer_class = ChangePasswordRequestSerializer

    # Rate limit: 5 per hour per user (by authenticated user ID)
    def post(self, request, *args, **kwargs):
        if getattr(request, 'limited', False):
            return _rate_limited_response()

        serializer = self.serializer_class(data=request.data, context={'request': request})
        if serializer.is_valid():
            user = request.user

            if user.email and user.is_active:
                verification_code = get_random_string(length=6, allowed_chars='0123456789')
                user.verification_code = verification_code
                user.save()

                context = {'verification_code': verification_code}
                subject = 'Change Password Verification Code'
                to_email = user.email
                self.send_verification_email(subject, context, to_email)

                logger.info(f"Change password verification code sent to {user.email}.")
                return Response({'message': 'Verification code sent to your email.'}, status=status.HTTP_200_OK)
            else:
                return Response({'error': 'User account is not active or email is missing.'}, status=status.HTTP_400_BAD_REQUEST)

        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

    def send_verification_email(self, subject, context, to_email):
        html_content = render_to_string('change_password_verification.html', context)

        configuration = Configuration()
        configuration.api_key['api-key'] = settings.BREVO_API_KEY

        api_instance = TransactionalEmailsApi(ApiClient(configuration))

        send_smtp_email = SendSmtpEmail(
            to=[{"email": to_email}],
            sender={"name": "Your Company", "email": settings.DEFAULT_FROM_EMAIL},
            subject=subject,
            html_content=html_content
        )

        try:
            api_response = api_instance.send_transac_email(send_smtp_email)
            logger.info(f"Verification email sent to {to_email}: {api_response}")
        except ApiException as e:
            logger.error(f"Exception when sending email: {e}")


class ChangePasswordView(APIView):
    permission_classes = [permissions.IsAuthenticated]
    serializer_class = ChangePasswordSerializer

    # Rate limit: 5 per 15 minutes per user
    def post(self, request, *args, **kwargs):
        if getattr(request, 'limited', False):
            return _rate_limited_response()

        serializer = self.serializer_class(data=request.data, context={'request': request})
        if serializer.is_valid():
            user = request.user
            verification_code = serializer.validated_data['verification_code']
            old_password = serializer.validated_data['old_password']
            new_password = serializer.validated_data['new_password']

            # Check verification code first
            if user.verification_code != verification_code:
                return Response({'error': 'Invalid verification code.'}, status=status.HTTP_400_BAD_REQUEST)

            # Check old password with specific error message
            if not user.check_password(old_password):
                return Response({'error': 'Current password is incorrect. Please enter your correct current password.'}, status=status.HTTP_400_BAD_REQUEST)

            # Additional validation for new password
            if old_password == new_password:
                return Response({'error': 'New password must be different from your current password.'}, status=status.HTTP_400_BAD_REQUEST)

            user.set_password(new_password)
            user.verification_code = None
            user.save()

            logger.info(f"Password successfully changed for {user.email}.")
            return Response({'message': 'Password changed successfully.'}, status=status.HTTP_200_OK)
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)


class VerifyChangePasswordCodeView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    # Rate limit: 10 per 15 minutes per user
    def post(self, request, *args, **kwargs):
        if getattr(request, 'limited', False):
            return _rate_limited_response()

        user = request.user
        verification_code = request.data.get('verification_code')

        if not verification_code:
            return Response({'error': 'Verification code is required.'}, status=status.HTTP_400_BAD_REQUEST)

        if user.verification_code == verification_code:
            return Response({'message': 'Verification code is valid.'}, status=status.HTTP_200_OK)
        return Response({'error': 'Invalid verification code. Please check your email and try again.'}, status=status.HTTP_400_BAD_REQUEST)


class LogoutView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request):
        logout(request)
        return Response({'detail': 'Logout successful'})