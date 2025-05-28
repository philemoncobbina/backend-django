# urls.py
from django.urls import path
from .views import request_money

urlpatterns = [
    path('request-money/', request_money, name='request_money'),
]
