from django.urls import path
from .views import request_payment

urlpatterns = [
    path('request-payment/', request_payment, name='request_payment'),
    # Add other URLs for your Django app as needed
]
