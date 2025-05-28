from django.urls import path
from .views import (
    StudentSignUpView, 
    StudentVerifyEmailView, 
    StudentLoginView,
    BatchStudentCreationView
)

urlpatterns = [
    path('student-signup/', StudentSignUpView.as_view(), name='student-signup'),
    path('verify-email/<int:user_id>/<str:token>/', StudentVerifyEmailView.as_view(), name='student-verify-email'),
    path('student-login/', StudentLoginView.as_view(), name='student-login'),
    path('batch-create/', BatchStudentCreationView.as_view(), name='student-batch-create'),
]

# //2