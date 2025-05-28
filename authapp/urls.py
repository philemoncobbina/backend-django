from django.urls import path
from .views import (
    SignUpView, GoogleSignInView ,
    VerifyEmailView, 
    LoginView, 
    UserDetailView, 
    PasswordResetView, 
    PasswordResetConfirmView, 
    VerifyResetCodeView, 
    ChangePasswordRequestView, 
    ChangePasswordView, 
    VerifyChangePasswordCodeView,
    
)

urlpatterns = [
    path('signup/', SignUpView.as_view(), name='signup'),
    path('google-signin/', GoogleSignInView.as_view(), name='google-signin'),
    path('verify-email/<int:user_id>/<str:token>/', VerifyEmailView.as_view(), name='verify-email'),
    path('login/', LoginView.as_view(), name='login'),
    path('user-detail/', UserDetailView.as_view(), name='user-detail'),
    path('password-reset/', PasswordResetView.as_view(), name='password-reset'),
    path('password-reset-confirm/', PasswordResetConfirmView.as_view(), name='password-reset-confirm'),
    path('verify-reset-code/', VerifyResetCodeView.as_view(), name='verify-reset-code'),

    # Change Password endpoints
    path('change-password-request/', ChangePasswordRequestView.as_view(), name='change-password-request'),
    path('change-password/', ChangePasswordView.as_view(), name='change-password'),
    path('verify-change-password-code/', VerifyChangePasswordCodeView.as_view(), name='verify-change-password-code'),

    
]
