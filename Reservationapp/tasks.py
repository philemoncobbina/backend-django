import logging

from celery import shared_task
from django.conf import settings
from django.utils.html import escape
from sib_api_v3_sdk import ApiClient, Configuration, SendSmtpEmail
from sib_api_v3_sdk.api.transactional_emails_api import TransactionalEmailsApi
from sib_api_v3_sdk.rest import ApiException

from .models import Reservation

logger = logging.getLogger(__name__)

MAX_RETRIES = 3
BREVO_TIMEOUT_SECONDS = 10
SENDER_NAME = "Your Company"


def _decode_body(exc):
    """ApiException.body may be bytes, str or None depending on the SDK version."""
    body = getattr(exc, "body", None)
    if body is None:
        return str(exc)
    if isinstance(body, (bytes, bytearray)):
        return body.decode("utf-8", errors="replace")
    return str(body)


def _is_retryable(exc):
    """Retry network errors, rate limits and Brevo 5xx. 4xx errors (bad key,
    unverified sender, invalid recipient) can't be fixed by retrying."""
    status = getattr(exc, "status", None)
    if status is None:
        return True
    return status == 429 or status >= 500


def _deliver(task, label, to_email, subject, html_content):
    """
    Send one email through Brevo. Logs every failure, retries only the ones that can
    succeed later, and never raises anything except Celery's retry signal.
    """
    api_key = getattr(settings, "BREVO_API_KEY", "")
    sender_email = getattr(settings, "DEFAULT_FROM_EMAIL", "")
    if not api_key or not sender_email:
        logger.error("%s not sent: BREVO_API_KEY or DEFAULT_FROM_EMAIL missing", label)
        return
    if not to_email:
        logger.warning("%s skipped: no recipient email", label)
        return

    try:
        configuration = Configuration()
        configuration.api_key["api-key"] = api_key
        api_instance = TransactionalEmailsApi(ApiClient(configuration))

        email = SendSmtpEmail(
            to=[{"email": to_email}],
            sender={"name": SENDER_NAME, "email": sender_email},
            subject=subject,
            html_content=html_content,
        )
        response = api_instance.send_transac_email(
            email, _request_timeout=BREVO_TIMEOUT_SECONDS
        )
        logger.info("%s sent", label)
        logger.debug("Brevo response: %s", response)

    except ApiException as exc:
        logger.error(
            "%s rejected by Brevo. Status: %s, Reason: %s, Body: %s",
            label,
            getattr(exc, "status", "N/A"),
            getattr(exc, "reason", "N/A"),
            _decode_body(exc),
        )
        if _is_retryable(exc) and task.request.retries < MAX_RETRIES:
            raise task.retry(exc=exc, countdown=60 * (2 ** task.request.retries))

    except Exception as exc:
        logger.error("Unexpected error sending %s: %s", label, exc, exc_info=True)
        if task.request.retries < MAX_RETRIES:
            raise task.retry(exc=exc, countdown=60 * (2 ** task.request.retries))


@shared_task(bind=True, max_retries=MAX_RETRIES, acks_late=True)
def send_reservation_received_email(self, reservation_pk):
    """'We received your reservation' email, sent right after creation."""
    try:
        reservation = Reservation.objects.get(pk=reservation_pk)
    except Reservation.DoesNotExist:
        logger.error("Received-email skipped: reservation pk=%s no longer exists", reservation_pk)
        return

    html = f"""
    <html>
    <body>
        <p>Dear {escape(reservation.full_name)},</p>
        <p>Your reservation for {escape(str(reservation.booking_date))} at {escape(str(reservation.booking_time))} has been received and is currently pending.</p>
        <p>Thank you for choosing our services!</p>
        <p>Best regards,<br>{escape(SENDER_NAME)}</p>
    </body>
    </html>
    """
    _deliver(
        self,
        f"Reservation received email (reservation {reservation_pk})",
        reservation.email,
        "Your Reservation Confirmation",
        html,
    )


@shared_task(bind=True, max_retries=MAX_RETRIES, acks_late=True)
def send_reservation_confirmed_email(self, reservation_pk):
    """'Your reservation is confirmed' email, sent when status changes to Confirmed."""
    try:
        reservation = Reservation.objects.get(pk=reservation_pk)
    except Reservation.DoesNotExist:
        logger.error("Confirmed-email skipped: reservation pk=%s no longer exists", reservation_pk)
        return

    # The task may run (or retry) minutes later; don't send if it was changed back meanwhile.
    if reservation.status != "Confirmed":
        logger.info(
            "Confirmed-email skipped: reservation %s is now '%s'",
            reservation_pk,
            reservation.status,
        )
        return

    html = f"""
    <html>
    <body>
        <h2>Reservation Confirmation</h2>
        <p>Dear {escape(reservation.full_name)},</p>
        <p>We are pleased to inform you that your reservation has been <strong>confirmed</strong>.</p>

        <h3>Appointment Details:</h3>
        <ul>
            <li><strong>Date:</strong> {reservation.booking_date.strftime('%A, %B %d, %Y')}</li>
            <li><strong>Time:</strong> {reservation.booking_time.strftime('%I:%M %p')}</li>
            <li><strong>Department:</strong> {escape(str(reservation.department))}</li>
        </ul>

        <p>Please arrive 10 minutes before your scheduled appointment time.</p>
        <p>If you need to cancel or reschedule, please contact us at least 24 hours in advance.</p>

        <p>Thank you for choosing our services!</p>
        <p>Best regards,<br>{escape(SENDER_NAME)}</p>
    </body>
    </html>
    """
    _deliver(
        self,
        f"Reservation confirmed email (reservation {reservation_pk})",
        reservation.email,
        "Your Reservation Has Been Confirmed",
        html,
    )