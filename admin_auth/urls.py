from django.urls import path

from .views import (
    AdminSignUpView,
    AdminUserManagementView,
    CookieTokenRefreshView,
    CSRFTokenView,
    LoginView,
    LogoutView,
    SessionCheckView,
    UserChangeLogListView,
    UserDetailView,
    VerifyEmailView,
)

app_name = "admin_auth"

urlpatterns = [
    path("csrf-auth/", CSRFTokenView.as_view(), name="csrf"),
    path("token-refresh-auth/", CookieTokenRefreshView.as_view(), name="token-refresh"),
    path("signup-auth/", AdminSignUpView.as_view(), name="signup"),
    path("verify-email/<str:token>/", VerifyEmailView.as_view(), name="verify-email"),
    path("login-auth/", LoginView.as_view(), name="login"),
    path("admin/users/", AdminUserManagementView.as_view(), name="admin-user-management-list"),
    path("admin/user/<int:user_id>/", AdminUserManagementView.as_view(), name="admin-user-management-detail"),
    path("admin/user-change-logs/", UserChangeLogListView.as_view(), name="user-change-log-list"),
    path("user-detail-auth/", UserDetailView.as_view(), name="user-detail"),
    path("session-check/", SessionCheckView.as_view(), name="session-check"),
    path("logout-auth/", LogoutView.as_view(), name="logout"),
]