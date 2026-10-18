import logging
from datetime import time

from django.db import transaction
from django.utils import timezone
from rest_framework import generics, status, viewsets
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response

from .models import Reservation, ReservationLog
from .serializers import ReservationLogSerializer, ReservationSerializer
from .tasks import send_reservation_confirmed_email, send_reservation_received_email

logger = logging.getLogger(__name__)


def queue_email(task, reservation_pk, label):
    """
    Queue a Celery email task after the DB transaction commits. Emails are best-effort:
    any failure here (Redis down, etc.) is logged and never reaches the user.
    """

    def _enqueue():
        try:
            task.delay(reservation_pk)
            logger.info("Queued %s email for reservation %s", label, reservation_pk)
        except Exception as e:
            logger.error(
                "Could not queue %s email for reservation %s: %s",
                label,
                reservation_pk,
                e,
                exc_info=True,
            )

    try:
        transaction.on_commit(_enqueue)
    except Exception as e:
        logger.error(
            "Could not schedule %s email for reservation %s: %s",
            label,
            reservation_pk,
            e,
            exc_info=True,
        )


class ReservationViewSet(viewsets.ModelViewSet):
    queryset = Reservation.objects.all()
    serializer_class = ReservationSerializer

    def get_permissions(self):
        if self.action in ["update", "partial_update"]:
            self.permission_classes = [IsAuthenticated]
        else:
            self.permission_classes = [AllowAny]
        return super().get_permissions()

    # ------------------------------------------------------------------ create
    def create(self, request):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        booking_date = serializer.validated_data["booking_date"]
        booking_time = serializer.validated_data["booking_time"]

        if not self.is_within_business_hours(booking_date, booking_time):
            return Response(
                {"detail": "Booking must be on a weekday between 9 AM and 4 PM."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        reservation = serializer.save()
        logger.info("Reservation %s created", reservation.pk)

        queue_email(send_reservation_received_email, reservation.pk, "received")

        return Response(
            {"detail": "Reservation submitted successfully!", "data": serializer.data},
            status=status.HTTP_201_CREATED,
        )

    # ------------------------------------------------------------------ update
    # DRF's partial_update() calls update(partial=True), so one method covers both
    # PUT and PATCH.
    def update(self, request, *args, **kwargs):
        partial = kwargs.pop("partial", False)
        reservation = self.get_object()
        logger.info("Updating reservation %s (partial=%s)", reservation.pk, partial)

        original_data = ReservationSerializer(reservation).data

        serializer = self.get_serializer(reservation, data=request.data, partial=partial)
        serializer.is_valid(raise_exception=True)

        booking_date = serializer.validated_data.get("booking_date", reservation.booking_date)
        booking_time = serializer.validated_data.get("booking_time", reservation.booking_time)
        department = serializer.validated_data.get("department", reservation.department)
        new_status = serializer.validated_data.get("status", reservation.status)

        if not self.is_within_business_hours(booking_date, booking_time):
            logger.info("Reservation %s update rejected: outside business hours", reservation.pk)
            return Response(
                {"detail": "Booking updates must be on a weekday between 9 AM and 4 PM."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        if self.is_conflicting_reservation(booking_date, booking_time, department, reservation.id):
            logger.info("Reservation %s update rejected: conflict", reservation.pk)
            return Response(
                {
                    "detail": "There is already a confirmed booking at this date and time for the same department."
                },
                status=status.HTTP_409_CONFLICT,
            )

        reservation.last_modified_by = request.user
        updated_reservation = serializer.save()

        updated_data = serializer.data
        changed_fields = self.get_changed_fields(original_data, updated_data)
        logger.info("Reservation %s changed fields: %s", reservation.pk, changed_fields)

        ReservationLog.objects.create(
            reservation=reservation,
            user=request.user,
            user_email=request.user.email,
            changed_fields=changed_fields,
        )

        if new_status == "Confirmed" and original_data.get("status") != "Confirmed":
            queue_email(send_reservation_confirmed_email, updated_reservation.pk, "confirmed")

        return Response(serializer.data)

    # ----------------------------------------------------------------- helpers
    def is_within_business_hours(self, booking_date, booking_time):
        """Weekday, not in the past, and between 9 AM and 4 PM."""
        if booking_date < timezone.now().date():
            return False
        if booking_time < time(9, 0) or booking_time >= time(16, 0):
            return False
        if booking_date.weekday() >= 5:
            return False
        return True

    def get_changed_fields(self, original_data, updated_data):
        changed_fields = []
        for key, original_value in original_data.items():
            updated_value = updated_data.get(key)
            if original_value != updated_value:
                changed_fields.append(f"{key}: {original_value} -> {updated_value}")
        return ", ".join(changed_fields)

    def is_conflicting_reservation(self, booking_date, booking_time, department, current_reservation_id=None):
        """Is there another Confirmed reservation for the same date, time and department?"""
        return (
            Reservation.objects.filter(
                booking_date=booking_date,
                booking_time=booking_time,
                department=department,
                status="Confirmed",
            )
            .exclude(id=current_reservation_id)
            .exists()
        )


class ReservationLogListView(generics.ListAPIView):
    serializer_class = ReservationLogSerializer

    def get_queryset(self):
        reservation_id = self.kwargs["reservation_id"]
        return ReservationLog.objects.filter(reservation_id=reservation_id)