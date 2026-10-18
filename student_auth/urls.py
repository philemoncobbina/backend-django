# student_auth/urls.py

from django.urls import path

from authapp.shared_auth import (
    CookieTokenRefreshView,
    CSRFTokenView,
    LogoutView,
)

from .views import (
    BatchStudentCreationView,
    StudentLoginView,
    StudentSignUpView,
    StudentVerifyEmailView,
)

app_name = "student_auth"

urlpatterns = [
    # Authentication and CSRF
    path(
        "student-csrf/",
        CSRFTokenView.as_view(),
        name="student-csrf",
    ),
    path(
        "student-token-refresh/",
        CookieTokenRefreshView.as_view(),
        name="student-token-refresh",
    ),
    path(
        "student-logout/",
        LogoutView.as_view(),
        name="student-logout",
    ),

    # Student account management
    path(
        "student-signup/",
        StudentSignUpView.as_view(),
        name="student-signup",
    ),
    path(
        "student-login/",
        StudentLoginView.as_view(),
        name="student-login",
    ),
    path(
        "batch-create/",
        BatchStudentCreationView.as_view(),
        name="student-batch-create",
    ),

    # Student email verification
    #
    # This path must be different from authapp's:
    #     verify-email/<str:token>/
    #
    # Otherwise, when both URL files are mounted under /api/, Django can send
    # a student verification token to authapp.views.VerifyEmailView.
    path(
        "student/verify-email/<str:token>/",
        StudentVerifyEmailView.as_view(),
        name="student-verify-email",
    ),
]