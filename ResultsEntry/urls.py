from django.urls import path, include
from rest_framework.routers import DefaultRouter
from .views import (
    CourseViewSet,
    ClassCourseViewSet,
    ResultViewSet,
    StudentResultsViewSet,
    StudentCoursesViewSet,
)

router = DefaultRouter()
router.register(r'courses', CourseViewSet)
router.register(r'class-courses', ClassCourseViewSet)
router.register(r'results', ResultViewSet)
router.register(r'my-results', StudentResultsViewSet, basename='my-results')
router.register(r'my-courses', StudentCoursesViewSet, basename='my-courses')

urlpatterns = [
    path('', include(router.urls)),
]