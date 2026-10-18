# authapp/urls.py

from django.urls import path

from .views import (
    AdminPasswordResetView,
    ChangePasswordRequestView,
    ChangePasswordView,
    CookieTokenRefreshView,
    CSRFTokenView,
    GoogleSignInView,
    LoginView,
    LogoutView,
    PasswordResetConfirmView,
    ResendVerificationEmailView,
    SignUpView,
    StudentPasswordResetView,
    UserDetailView,
    VerifyChangePasswordCodeView,
    VerifyEmailView,
    VerifyResetCodeView,
    WebsitePasswordResetView,
)

app_name = "authapp"

urlpatterns = [
    # CSRF, token refresh and logout
    path(
        "csrf/",
        CSRFTokenView.as_view(),
        name="csrf",
    ),
    path(
        "token/refresh/",
        CookieTokenRefreshView.as_view(),
        name="token-refresh",
    ),
    path(
        "logout/",
        LogoutView.as_view(),
        name="logout",
    ),

    # Registration and authentication
    path(
        "signup/",
        SignUpView.as_view(),
        name="signup",
    ),
    path(
        "google-signin/",
        GoogleSignInView.as_view(),
        name="google-signin",
    ),
    path(
        "verify-email/<str:token>/",
        VerifyEmailView.as_view(),
        name="verify-email",
    ),
    path(
        "resend-verification-email/",
        ResendVerificationEmailView.as_view(),
        name="resend-verification-email",
    ),
    path(
        "login/",
        LoginView.as_view(),
        name="login",
    ),
    path(
        "user-detail/",
        UserDetailView.as_view(),
        name="user-detail",
    ),

    # Portal-specific password reset requests
    path(
        "student/password-reset/",
        StudentPasswordResetView.as_view(),
        name="student-password-reset",
    ),
    path(
        "admin/password-reset/",
        AdminPasswordResetView.as_view(),
        name="admin-password-reset",
    ),
    path(
        "website/password-reset/",
        WebsitePasswordResetView.as_view(),
        name="website-password-reset",
    ),

    # Password reset confirmation
    path(
        "password-reset-confirm/",
        PasswordResetConfirmView.as_view(),
        name="password-reset-confirm",
    ),
    path(
        "verify-reset-code/",
        VerifyResetCodeView.as_view(),
        name="verify-reset-code",
    ),

    # Authenticated password change
    path(
        "change-password-request/",
        ChangePasswordRequestView.as_view(),
        name="change-password-request",
    ),
    path(
        "change-password/",
        ChangePasswordView.as_view(),
        name="change-password",
    ),
    path(
        "verify-change-password-code/",
        VerifyChangePasswordCodeView.as_view(),
        name="verify-change-password-code",
    ),
]