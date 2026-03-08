from django.urls import path, include
from rest_framework.routers import DefaultRouter
from . import views

router = DefaultRouter()
router.register(r'posts', views.BlogPostViewSet, basename='blogpost')
router.register(r'published-posts', views.PublishedBlogPostViewSet, basename='published-posts')

urlpatterns = [
    path('', include(router.urls)),
]