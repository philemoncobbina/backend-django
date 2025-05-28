from django.urls import path, include
from rest_framework.routers import DefaultRouter
from rest_framework_nested import routers
from . import views

# Main router
router = DefaultRouter()

router.register(r'booklists', views.BookListViewSet, basename='booklists')  # <-- Add basename here

# Nested router for book list items
booklists_router = routers.NestedSimpleRouter(router, r'booklists', lookup='booklist')
booklists_router.register(r'items', views.BookListItemViewSet, basename='booklist-items')

urlpatterns = [
    path('', include(router.urls)),
    path('', include(booklists_router.urls)),
    
]