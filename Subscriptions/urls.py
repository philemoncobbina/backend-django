from django.urls import path, include
from rest_framework.routers import DefaultRouter
from .views import SubscriptionViewSet, EmailListViewSet  # Make sure to import the new viewset

router = DefaultRouter()
router.register(r'subscriptions', SubscriptionViewSet)
router.register(r'email-list', EmailListViewSet, basename='email-list')  # Add this line

urlpatterns = [
    path('', include(router.urls)),
]
