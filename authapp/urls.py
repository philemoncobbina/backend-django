from django.urls import path

from .views import (
    AdminPasswordResetView,
    ChangePasswordRequestView,
    ChangePasswordView,
    GoogleSignInView,
    LoginView,
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

urlpatterns = [
    path("signup/", SignUpView.as_view(), name="signup"),
    path("google-signin/", GoogleSignInView.as_view(), name="google-signin"),
    path("verify-email/<str:token>/", VerifyEmailView.as_view(), name="verify-email"),
    path("resend-verification-email/", ResendVerificationEmailView.as_view(), name="resend-verification-email"),
    path("login/", LoginView.as_view(), name="login"),
    path("user-detail/", UserDetailView.as_view(), name="user-detail"),

    # ----- Password Reset — Student Portal (role=student only) -----
    path("student/password-reset/", StudentPasswordResetView.as_view(), name="student-password-reset"),

    # ----- Password Reset — Admin Portal (role=principal or staff only) -----
    path("admin/password-reset/", AdminPasswordResetView.as_view(), name="admin-password-reset"),

    # ----- Password Reset — School Website (no role / general accounts only) -----
    path("website/password-reset/", WebsitePasswordResetView.as_view(), name="website-password-reset"),

    # ----- Shared: confirm reset & verify code (used by all three portals) -----
    path("password-reset-confirm/", PasswordResetConfirmView.as_view(), name="password-reset-confirm"),
    path("verify-reset-code/", VerifyResetCodeView.as_view(), name="verify-reset-code"),

    # ----- Change Password (authenticated users) -----
    path("change-password-request/", ChangePasswordRequestView.as_view(), name="change-password-request"),
    path("change-password/", ChangePasswordView.as_view(), name="change-password"),
    path("verify-change-password-code/", VerifyChangePasswordCodeView.as_view(), name="verify-change-password-code"),
]