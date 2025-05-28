from django.urls import path, include
from rest_framework.routers import DefaultRouter
from .views import AdmissionViewSet, AdmissionLogViewSet

# Create a router and register the admission viewset
router = DefaultRouter()
router.register(r'admissions', AdmissionViewSet, basename='admission')

# Define the URL patterns
urlpatterns = [
    path('', include(router.urls)),
    # Custom route for fetching logs based on the admission ID
    path('admissions/<int:admission_id>/logs/', AdmissionLogViewSet.as_view({'get': 'list'}), name='admission-logs'),
]
