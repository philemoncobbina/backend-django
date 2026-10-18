import logging

from django.db import transaction
from django.db.models import Max
from rest_framework import generics, status, viewsets
from rest_framework.exceptions import ValidationError
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response

from .models import Ticket, TicketLog
from .serializers import TicketLogSerializer, TicketSerializer
from .tasks import send_ticket_confirmation_email

logger = logging.getLogger(__name__)


class TicketViewSet(viewsets.ModelViewSet):
    queryset = Ticket.objects.all()
    serializer_class = TicketSerializer

    def create(self, request):
        logger.info("Attempting to create new ticket")
        try:
            serializer = self.get_serializer(data=request.data)
            serializer.is_valid(raise_exception=True)

            ticket_id = self.generate_ticket_id()
            serializer.validated_data["TicketID"] = ticket_id
            logger.debug(f"Generated ticket ID: {ticket_id}")

            ticket = serializer.save()
            logger.info(f"Successfully created ticket with ID: {ticket_id}")
        except ValidationError:
            # Let DRF return a proper 400 with the field errors.
            raise
        except Exception as e:
            logger.error(f"Failed to create ticket: {str(e)}", exc_info=True)
            return Response(
                {"detail": "Failed to create ticket"},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )

        # The ticket is saved. Email is best-effort and must never change the response.
        self.queue_confirmation_email(ticket)

        return Response(
            {"detail": "Ticket submitted successfully!", "data": serializer.data},
            status=status.HTTP_200_OK,
        )

    def queue_confirmation_email(self, ticket):
        """Queue the email on Celery. Swallows every error (e.g. Redis down)."""
        try:
            # on_commit guarantees the worker can find the row, even inside ATOMIC_REQUESTS.
            transaction.on_commit(
                lambda: self._enqueue_email(ticket.pk, ticket.TicketID)
            )
        except Exception as e:
            logger.error(
                f"Could not schedule confirmation email for ticket {ticket.TicketID}: {e}",
                exc_info=True,
            )

    @staticmethod
    def _enqueue_email(ticket_pk, ticket_id):
        try:
            send_ticket_confirmation_email.delay(ticket_pk)
            logger.info(f"Queued confirmation email for ticket {ticket_id}")
        except Exception as e:
            # Broker unreachable, serialization problem, etc.
            logger.error(
                f"Could not queue confirmation email for ticket {ticket_id}: {e}",
                exc_info=True,
            )

    def generate_ticket_id(self):
        logger.debug("Generating new ticket ID")
        try:
            last_ticket = Ticket.objects.filter(
                TicketID__regex=r"^RCSTK\d{4}$"
            ).aggregate(Max("TicketID"))
            last_ticket_id = last_ticket["TicketID__max"]

            if last_ticket_id:
                new_id_number = int(last_ticket_id[5:]) + 1
                logger.debug(f"Generated next ID from last ticket: {last_ticket_id}")
            else:
                new_id_number = 1
                logger.debug("No existing tickets found, starting with ID 1")

            new_ticket_id = f"RCSTK{new_id_number:04d}"
            logger.info(f"Successfully generated new ticket ID: {new_ticket_id}")
            return new_ticket_id
        except Exception as e:
            logger.error(f"Failed to generate ticket ID: {str(e)}", exc_info=True)
            raise

    def get_permissions(self):
        logger.debug(f"Getting permissions for action: {self.action}")
        if self.action in ["update", "partial_update"]:
            self.permission_classes = [IsAuthenticated]
        else:
            self.permission_classes = [AllowAny]
        return super().get_permissions()

    def update(self, request, pk=None):
        logger.info(f"Attempting to update ticket with ID: {pk}")
        try:
            ticket = self.get_object()
            original_data = TicketSerializer(ticket).data
            logger.debug(f"Original ticket data: {original_data}")

            # Remove screenshot from update request data
            request_data = request.data.copy()
            if "screenshot" in request_data:
                logger.debug("Removing screenshot from update request")
                del request_data["screenshot"]

            serializer = self.get_serializer(ticket, data=request_data, partial=False)
            serializer.is_valid(raise_exception=True)

            serializer.save()
            updated_data = serializer.data
            logger.debug(f"Updated ticket data: {updated_data}")

            changed_fields = self.get_changed_fields(original_data, updated_data)
            logger.info(f"Fields changed: {changed_fields}")

            TicketLog.objects.create(
                ticket=ticket,
                user=request.user,
                user_email=request.user.email,
                changed_fields=changed_fields,
            )
            logger.info(f"Successfully updated ticket {pk} and created log entry")

            return Response(serializer.data)
        except Exception as e:
            logger.error(f"Failed to update ticket {pk}: {str(e)}", exc_info=True)
            raise

    def get_changed_fields(self, original_data, updated_data):
        try:
            changed_fields = []
            exclude_fields = ["updated_at", "last_modified_by"]

            # Special handling for screenshot field
            if "screenshot" in original_data and "screenshot" in updated_data:
                original_screenshot = original_data["screenshot"]
                updated_screenshot = updated_data["screenshot"]

                if original_screenshot and updated_screenshot:
                    original_filename = original_screenshot.split("/")[-1]
                    updated_filename = updated_screenshot.split("/")[-1]

                    if original_filename != updated_filename:
                        changed_fields.append(
                            f"screenshot: {original_screenshot} -> {updated_screenshot}"
                        )
                        logger.debug(
                            f"Field 'screenshot' changed from '{original_screenshot}' to '{updated_screenshot}'"
                        )

            for key, original_value in original_data.items():
                if key == "screenshot" or key in exclude_fields:
                    continue

                updated_value = updated_data.get(key)
                if original_value != updated_value:
                    changed_fields.append(f"{key}: {original_value} -> {updated_value}")
                    logger.debug(
                        f"Field '{key}' changed from '{original_value}' to '{updated_value}'"
                    )

            return ", ".join(changed_fields)
        except Exception as e:
            logger.error(f"Error comparing changed fields: {str(e)}", exc_info=True)
            raise


class TicketLogListView(generics.ListAPIView):
    serializer_class = TicketLogSerializer

    def get_queryset(self):
        ticket_id = self.kwargs["ticket_id"]
        logger.debug(f"Fetching logs for ticket: {ticket_id}")
        return TicketLog.objects.filter(ticket_id=ticket_id)