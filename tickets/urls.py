from django.urls import path, include
from rest_framework.routers import DefaultRouter
from .views import TicketViewSet, TicketLogListView

router = DefaultRouter()
router.register(r'tickets', TicketViewSet)

urlpatterns = [
    path('', include(router.urls)),
    path('tickets/<int:ticket_id>/logs/', TicketLogListView.as_view(), name='ticket-logs'),  # New URL for ticket logs
]
