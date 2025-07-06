import logging
import os
from rest_framework import viewsets, status, generics
from rest_framework.response import Response
from rest_framework.decorators import action
from rest_framework.permissions import IsAuthenticated, AllowAny
from django.utils import timezone
from django.db.models import Max
from django.conf import settings
from pathlib import Path
from dotenv import load_dotenv
from sib_api_v3_sdk import Configuration, ApiClient, SendSmtpEmail
from sib_api_v3_sdk.api.transactional_emails_api import TransactionalEmailsApi
from sib_api_v3_sdk.rest import ApiException

from .models import Ticket, TicketLog
from .serializers import TicketSerializer, TicketLogSerializer

# Configure logger
logger = logging.getLogger(__name__)

class TicketViewSet(viewsets.ModelViewSet):
    queryset = Ticket.objects.all()
    serializer_class = TicketSerializer

    def create(self, request):
        logger.info("Attempting to create new ticket")
        try:
            # Validate and create ticket
            serializer = self.get_serializer(data=request.data)
            serializer.is_valid(raise_exception=True)

            # Generate a unique TicketID
            ticket_id = self.generate_ticket_id()
            serializer.validated_data['TicketID'] = ticket_id
            logger.debug(f"Generated ticket ID: {ticket_id}")

            # Save the ticket
            ticket = serializer.save()
            logger.info(f"Successfully created ticket with ID: {ticket_id}")

            # Send confirmation email
            self.send_ticket_confirmation_email(ticket)

            return Response(
                {'detail': 'Ticket submitted successfully!', 'data': serializer.data}, 
                status=status.HTTP_200_OK
            )
        except Exception as e:
            logger.error(f"Failed to create ticket: {str(e)}", exc_info=True)
            return Response(
                {'detail': 'Failed to create ticket'}, 
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )

    def generate_ticket_id(self):
        logger.debug("Generating new ticket ID")
        try:
            last_ticket = Ticket.objects.filter(TicketID__regex=r'^RCSTK\d{4}$').aggregate(Max('TicketID'))
            last_ticket_id = last_ticket['TicketID__max']

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

    def send_ticket_confirmation_email(self, ticket):
        logger.info(f"Attempting to send confirmation email for ticket: {ticket.TicketID}")
        
        # Check for required environment variables
        brevo_api_key = os.getenv('BREVO_API_KEY')
        sender_email = os.getenv('DEFAULT_FROM_EMAIL')
        
        if not brevo_api_key:
            logger.error("BREVO_API_KEY not found in environment variables")
            raise ValueError("BREVO_API_KEY not configured")
        
        if not sender_email:
            logger.error("DEFAULT_FROM_EMAIL not found in environment variables")
            raise ValueError("DEFAULT_FROM_EMAIL not configured")
        
        logger.info("Email configuration variables successfully loaded")
        logger.debug(f"Using sender email: {sender_email}")

        try:
            configuration = Configuration()
            configuration.api_key['api-key'] = brevo_api_key
            api_instance = TransactionalEmailsApi(ApiClient(configuration))

            send_smtp_email = SendSmtpEmail(
                to=[{"email": ticket.email}],
                sender={"name": "Support Team", "email": sender_email},  # Using email from environment variables
                subject="Your Support Ticket ID",
                html_content=f"""
                <html>
                    <body>
                        <p>Dear {ticket.full_name},</p>
                        <p>Your ticket ID is <strong>{ticket.TicketID}</strong>.</p>
                        <p>We will review your request soon.</p>
                        <p>Best regards,<br>Support Team</p>
                    </body>
                </html>
                """
            )

            api_response = api_instance.send_transac_email(send_smtp_email)
            logger.info(f"Confirmation email sent successfully for ticket {ticket.TicketID}")
            logger.debug(f"Email API response: {api_response}")
        
        except ApiException as e:
            error_body = str(e.body.decode()) if hasattr(e, 'body') else str(e)
            logger.error(f"Failed to send confirmation email for ticket {ticket.TicketID}. "
                        f"Status: {getattr(e, 'status', 'N/A')}, "
                        f"Reason: {getattr(e, 'reason', 'N/A')}, "
                        f"Body: {error_body}", exc_info=True)
            raise
        except Exception as e:
            logger.error(f"Unexpected error sending confirmation email for ticket {ticket.TicketID}: {str(e)}", exc_info=True)
            raise

    def get_permissions(self):
        logger.debug(f"Getting permissions for action: {self.action}")
        if self.action in ['update', 'partial_update']:
            self.permission_classes = [IsAuthenticated]
            logger.debug("Using IsAuthenticated permission class")
        else:
            self.permission_classes = [AllowAny]
            logger.debug("Using AllowAny permission class")
        return super().get_permissions()

    def update(self, request, pk=None):
        logger.info(f"Attempting to update ticket with ID: {pk}")
        try:
            ticket = self.get_object()
            original_data = TicketSerializer(ticket).data
            logger.debug(f"Original ticket data: {original_data}")

            # Remove screenshot from update request data
            request_data = request.data.copy()
            if 'screenshot' in request_data:
                logger.debug("Removing screenshot from update request")
                del request_data['screenshot']

            serializer = self.get_serializer(ticket, data=request_data, partial=False)
            serializer.is_valid(raise_exception=True)

            # Save updated ticket
            serializer.save()
            updated_data = serializer.data
            logger.debug(f"Updated ticket data: {updated_data}")

            # Track changes
            changed_fields = self.get_changed_fields(original_data, updated_data)
            logger.info(f"Fields changed: {changed_fields}")

            TicketLog.objects.create(
                ticket=ticket,
                user=request.user,
                user_email=request.user.email,
                changed_fields=changed_fields
            )
            logger.info(f"Successfully updated ticket {pk} and created log entry")

            return Response(serializer.data)
        except Exception as e:
            logger.error(f"Failed to update ticket {pk}: {str(e)}", exc_info=True)
            raise

    def get_changed_fields(self, original_data, updated_data):
        try:
            changed_fields = []
            # Fields to exclude from comparison
            exclude_fields = ['updated_at', 'last_modified_by']
            
            # Special handling for screenshot field
            if 'screenshot' in original_data and 'screenshot' in updated_data:
                original_screenshot = original_data['screenshot']
                updated_screenshot = updated_data['screenshot']
                
                # Extract just the filename for comparison
                if original_screenshot and updated_screenshot:
                    original_filename = original_screenshot.split('/')[-1]
                    updated_filename = updated_screenshot.split('/')[-1]
                    
                    # Only consider screenshot changed if the actual filename changed
                    if original_filename != updated_filename:
                        changed_fields.append(f"screenshot: {original_screenshot} -> {updated_screenshot}")
                        logger.debug(f"Field 'screenshot' changed from '{original_screenshot}' to '{updated_screenshot}'")
            
            # Compare all other fields
            for key, original_value in original_data.items():
                # Skip screenshot (already handled) and excluded fields
                if key == 'screenshot' or key in exclude_fields:
                    continue
                    
                updated_value = updated_data.get(key)
                if original_value != updated_value:
                    changed_fields.append(f"{key}: {original_value} -> {updated_value}")
                    logger.debug(f"Field '{key}' changed from '{original_value}' to '{updated_value}'")
                    
            return ', '.join(changed_fields)
        except Exception as e:
            logger.error(f"Error comparing changed fields: {str(e)}", exc_info=True)
            raise

class TicketLogListView(generics.ListAPIView):
    serializer_class = TicketLogSerializer

    def get_queryset(self):
        ticket_id = self.kwargs['ticket_id']
        logger.debug(f"Fetching logs for ticket: {ticket_id}")
        return TicketLog.objects.filter(ticket_id=ticket_id)
