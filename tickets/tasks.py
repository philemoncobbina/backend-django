import logging

from celery import shared_task
from django.conf import settings
from django.utils.html import escape
from sib_api_v3_sdk import ApiClient, Configuration, SendSmtpEmail
from sib_api_v3_sdk.api.transactional_emails_api import TransactionalEmailsApi
from sib_api_v3_sdk.rest import ApiException

from .models import Ticket

logger = logging.getLogger(__name__)

MAX_RETRIES = 3
BREVO_TIMEOUT_SECONDS = 10


def _decode_body(exc):
    """
    ApiException.body may be bytes, str or None depending on the SDK version.
    Never let error-reporting code raise its own exception.
    """
    body = getattr(exc, "body", None)
    if body is None:
        return str(exc)
    if isinstance(body, (bytes, bytearray)):
        return body.decode("utf-8", errors="replace")
    return str(body)


def _is_retryable(exc):
    """Retry network problems, rate limits and Brevo 5xx. Do not retry 4xx
    (bad API key, unverified sender, invalid recipient): retrying cannot fix them."""
    status = getattr(exc, "status", None)
    if status is None:
        return True
    return status == 429 or status >= 500


@shared_task(bind=True, max_retries=MAX_RETRIES, acks_late=True)
def send_ticket_confirmation_email(self, ticket_pk):
    """
    Send the "your ticket ID" email. Runs in the Celery worker, so a failure here
    can never affect the ticket that was already saved.
    """
    try:
        ticket = Ticket.objects.get(pk=ticket_pk)
    except Ticket.DoesNotExist:
        logger.error("Confirmation email skipped: ticket pk=%s no longer exists", ticket_pk)
        return

    if not ticket.email:
        logger.warning("Confirmation email skipped: ticket %s has no email", ticket.TicketID)
        return

    api_key = getattr(settings, "BREVO_API_KEY", "")
    sender_email = getattr(settings, "DEFAULT_FROM_EMAIL", "")
    if not api_key or not sender_email:
        logger.error(
            "Confirmation email for ticket %s not sent: BREVO_API_KEY or DEFAULT_FROM_EMAIL missing",
            ticket.TicketID,
        )
        return

    logger.info("Sending confirmation email for ticket %s", ticket.TicketID)

    try:
        configuration = Configuration()
        configuration.api_key["api-key"] = api_key
        api_instance = TransactionalEmailsApi(ApiClient(configuration))

        email = SendSmtpEmail(
            to=[{"email": ticket.email}],
            sender={"name": "Support Team", "email": sender_email},
            subject="Your Support Ticket ID",
            html_content=f"""
            <html>
                <body>
                    <p>Dear {escape(ticket.full_name)},</p>
                    <p>Your ticket ID is <strong>{escape(ticket.TicketID)}</strong>.</p>
                    <p>We will review your request soon.</p>
                    <p>Best regards,<br>Support Team</p>
                </body>
            </html>
            """,
        )

        response = api_instance.send_transac_email(
            email, _request_timeout=BREVO_TIMEOUT_SECONDS
        )
        logger.info("Confirmation email sent for ticket %s", ticket.TicketID)
        logger.debug("Brevo response: %s", response)

    except ApiException as exc:
        logger.error(
            "Brevo rejected confirmation email for ticket %s. Status: %s, Reason: %s, Body: %s",
            ticket.TicketID,
            getattr(exc, "status", "N/A"),
            getattr(exc, "reason", "N/A"),
            _decode_body(exc),
        )
        if _is_retryable(exc) and self.request.retries < MAX_RETRIES:
            raise self.retry(exc=exc, countdown=60 * (2 ** self.request.retries))
        # Permanent failure (or retries exhausted): logged above, nothing more to do.

    except Exception as exc:
        logger.error(
            "Unexpected error sending confirmation email for ticket %s: %s",
            ticket.TicketID,
            exc,
            exc_info=True,
        )
        if self.request.retries < MAX_RETRIES:
            raise self.retry(exc=exc, countdown=60 * (2 ** self.request.retries))