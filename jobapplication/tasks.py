import logging

from celery import shared_task
from django.conf import settings
from django.utils.html import escape
from sib_api_v3_sdk import ApiClient, Configuration, SendSmtpEmail
from sib_api_v3_sdk.api.transactional_emails_api import TransactionalEmailsApi
from sib_api_v3_sdk.rest import ApiException

from .models import JobApplication

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
def send_application_received_email(self, application_pk):
    """'Application received' email, sent right after a candidate applies."""
    try:
        application = JobApplication.objects.get(pk=application_pk)
    except JobApplication.DoesNotExist:
        logger.error("Received-email skipped: application pk=%s no longer exists", application_pk)
        return

    html = f"""
    <html>
    <body style="font-family: Arial, sans-serif; line-height: 1.6; color: #333;">
        <h2>Application Received</h2>
        <p>Dear {escape(application.first_name)} {escape(application.last_name)},</p>

        <p>Thank you for applying for the <strong>{escape(str(application.job_title))}</strong> position
        (Ref: {escape(str(application.job_reference_number))}).</p>

        <p>What happens next:</p>
        <ul>
            <li>Our hiring team will review your application</li>
            <li>We aim to respond within 5 business days</li>
            <li>If your profile matches our requirements, we'll invite you for an interview</li>
        </ul>

        <p>Best regards,<br>
        Hiring Team</p>
    </body>
    </html>
    """
    _deliver(
        self,
        f"Application received email (application {application_pk})",
        application.email,
        "Application Received - Next Steps",
        html,
    )


@shared_task(bind=True, max_retries=MAX_RETRIES, acks_late=True)
def send_application_rejection_email(self, application_pk):
    """Rejection email, sent when an application's status changes to REJECTED."""
    try:
        application = JobApplication.objects.get(pk=application_pk)
    except JobApplication.DoesNotExist:
        logger.error("Rejection-email skipped: application pk=%s no longer exists", application_pk)
        return

    # The task may run (or retry) minutes later; never send a rejection if staff
    # changed the status back in the meantime.
    if application.status != "REJECTED":
        logger.info(
            "Rejection-email skipped: application %s is now '%s'",
            application_pk,
            application.status,
        )
        return

    html = f"""
    <html>
    <body style="font-family: Arial, sans-serif; line-height: 1.6; color: #333;">
        <p>Dear {escape(application.first_name)} {escape(application.last_name)},</p>

        <p>Thank you for your interest in the <strong>{escape(str(application.job_title))}</strong> position
        (Ref: {escape(str(application.job_reference_number))}) and for taking the time to go through our application process.</p>

        <p>After careful consideration of all applications received, we regret to inform you that we will not be
        moving forward with your candidacy at this time. We received many qualified applications, making this
        a difficult decision.</p>

        <p>Please note that this decision is not necessarily a reflection of your qualifications or experience.
        We encourage you to apply for future openings that match your skills and interests.</p>

        <p>We appreciate your understanding and wish you success in your job search and future professional endeavors.</p>

        <p>Best regards,<br>
        Hiring Team</p>
    </body>
    </html>
    """
    _deliver(
        self,
        f"Application rejection email (application {application_pk})",
        application.email,
        f"Update on Your Application for {application.job_title}",
        html,
    )