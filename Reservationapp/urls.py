from django.urls import path, include
from rest_framework.routers import DefaultRouter
from .views import ReservationViewSet, ReservationLogListView

# Create a router for the ReservationViewSet
router = DefaultRouter()
router.register(r'reservations', ReservationViewSet, basename='reservation')

urlpatterns = [
    # Include the router URLs for the ReservationViewSet
    path('', include(router.urls)),

    # Add the URL for fetching reservation logs
    path('reservations/<int:reservation_id>/logs/', ReservationLogListView.as_view(), name='reservation-logs'),
]
