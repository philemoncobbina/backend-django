from django.urls import path
from .views import AdminSignUpView, AdminUserManagementView, VerifyEmailView, LoginView, UserDetailView, SessionCheckView , LogoutView

app_name = 'admin_auth'

urlpatterns = [
    path('signup-auth/', AdminSignUpView.as_view(), name='signup'),
    path('verify-email/<int:user_id>/<str:token>/', VerifyEmailView.as_view(), name='verify-email'),
    path('login-auth/', LoginView.as_view(), name='login'),
    path('admin/users/', AdminUserManagementView.as_view(), name='admin-user-management'), 
    path('admin/user/<int:user_id>/', AdminUserManagementView.as_view(), name='admin-user-management'),
    
    # Add UserDetailView and SessionCheckView
    path('user-detail-auth/', UserDetailView.as_view(), name='user-detail'),
    path('session-check/', SessionCheckView.as_view(), name='session-check'),
    path('logout-auth/', LogoutView.as_view(), name='logout'),
]
