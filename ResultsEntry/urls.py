from django.urls import path, include
from rest_framework.routers import DefaultRouter
from .views import (
    CourseViewSet,
    ClassCourseViewSet,
    ResultViewSet,
    StudentResultsViewSet,  # Changed from StudentResultsView
)

router = DefaultRouter()
router.register(r'courses', CourseViewSet)
router.register(r'class-courses', ClassCourseViewSet)
router.register(r'results', ResultViewSet)
router.register(r'my-results', StudentResultsViewSet, basename='my-results')  # Register as ViewSet

urlpatterns = [
    path('', include(router.urls)),
]