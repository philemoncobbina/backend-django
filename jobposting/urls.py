# urls.py
from django.urls import path, include
from rest_framework.routers import DefaultRouter
from .views import JobPostViewSet , JobPostLogListView

router = DefaultRouter()
router.register(r'jobposts', JobPostViewSet)

urlpatterns = [
    path('api/', include(router.urls)),
    path('api/jobposts/<int:job_post_id>/logs/', JobPostLogListView.as_view(), name='jobpost-logs'),
]