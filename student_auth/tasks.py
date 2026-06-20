"""
student_auth/tasks.py
=====================
Celery tasks that handle all I/O-bound work asynchronously so the API
response returns immediately.  Views pass only primitive, JSON-serialisable
arguments — never model instances — to keep serialisation simple and safe.
"""

import logging

from celery import shared_task
from django.conf import settings

from sib_api_v3_sdk import Configuration, ApiClient, SendSmtpEmail
from sib_api_v3_sdk.api.transactional_emails_api import TransactionalEmailsApi
from sib_api_v3_sdk.rest import ApiException

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Internal helpers (not tasks themselves)
# ---------------------------------------------------------------------------

def _dispatch_email(to_email: str, subject: str, html_body: str) -> None:
    """Low-level Brevo send.  Raises ApiException on failure."""
    configuration = Configuration()
    configuration.api_key["api-key"] = settings.BREVO_API_KEY

    api_instance = TransactionalEmailsApi(ApiClient(configuration))
    send_smtp_email = SendSmtpEmail(
        to=[{"email": to_email}],
        sender={"name": "School Admin", "email": settings.DEFAULT_FROM_EMAIL},
        subject=subject,
        html_content=html_body,
    )
    api_response = api_instance.send_transac_email(send_smtp_email)
    logger.info("Email sent to %s: %s", to_email, api_response)


def _build_guardian_html(guardian_list: list[dict]) -> str:
    """
    Render guardian rows from a list of plain dicts (already serialised by the view).
    This avoids any ORM access inside the task.
    """
    if not guardian_list:
        return "<li>No guardian information provided.</li>"

    rows = []
    for g in guardian_list:
        label = "(Primary Contact)" if g.get("is_primary_contact") else ""
        secondary = f" / {g['secondary_phone']}" if g.get("secondary_phone") else ""
        email_line = f"<br>Email: {g['email']}" if g.get("email") else ""
        rows.append(
            f"<li><strong>{g['full_name']}</strong> — "
            f"{g.get('relationship_display', g.get('relationship', ''))} {label}<br>"
            f"Phone: {g['primary_phone']}{secondary}"
            f"{email_line}</li>"
        )
    return "\n".join(rows)


# ---------------------------------------------------------------------------
# Email tasks
# ---------------------------------------------------------------------------

@shared_task(
    bind=True,
    max_retries=3,
    default_retry_delay=60,   # seconds between retries
    name="student_auth.tasks.send_student_verification_email",
)
def send_student_verification_email(
    self,
    to_email: str,
    first_name: str,
    username: str,
    index_number: str,
    class_name_display: str,
    raw_password: str,
    verification_url: str,
    guardian_list: list[dict],
) -> None:
    """
    Send the initial account-verification email to a newly created student.

    All arguments are primitives so Celery can serialise them with JSON.
    ``guardian_list`` is a list of dicts with keys:
        full_name, relationship, relationship_display,
        primary_phone, secondary_phone, email, is_primary_contact
    """
    guardian_html = _build_guardian_html(guardian_list)

    html_body = f"""
    <html>
    <body style="font-family: Arial, sans-serif; color: #333;">
        <p>Dear {first_name},</p>
        <h2 style="color: #4CAF50;">Welcome to Our School System!</h2>
        <p>Your student account has been created.
           Please click the button below to verify your email address:</p>

        <a href="{verification_url}"
           style="display:inline-block;padding:10px 20px;background-color:#4CAF50;
                  color:#fff;text-decoration:none;border-radius:5px;font-weight:bold;">
            Verify Your Email
        </a>

        <p>Your account details:</p>
        <ul>
            <li>Username: {username}</li>
            <li>Email: {to_email}</li>
            <li>Index Number: {index_number}</li>
            <li>Class: {class_name_display}</li>
            <li>Password: {raw_password}</li>
        </ul>

        <p><strong>Parent / Guardian on record:</strong></p>
        <ul>{guardian_html}</ul>

        <p>Please keep these credentials safe.
           We recommend changing your password after first login.</p>
        <p>If you did not expect this email, please contact the school administration.</p>
        <br>
        <p>Best regards,<br>School Administration</p>
    </body>
    </html>
    """

    try:
        _dispatch_email(to_email, "Verify Your Student Account", html_body)
    except ApiException as exc:
        logger.error("Email error for %s: %s", to_email, exc)
        # Exponential back-off: 60 s, 120 s, 240 s
        raise self.retry(exc=exc, countdown=60 * (2 ** self.request.retries))


@shared_task(
    bind=True,
    max_retries=3,
    default_retry_delay=60,
    name="student_auth.tasks.send_batch_student_verification_email",
)
def send_batch_student_verification_email(
    self,
    to_email: str,
    first_name: str,
    index_number: str,
    class_name_display: str,
    verification_url: str,
    guardian_list: list[dict],
) -> None:
    """
    Lighter version of the verification email used during batch creation —
    omits the plaintext password (not available after hashing in batch flow).
    """
    guardian_html = _build_guardian_html(guardian_list)

    html_body = f"""
    <html>
    <body style="font-family: Arial, sans-serif; color: #333;">
        <p>Dear {first_name},</p>
        <h2 style="color: #4CAF50;">Welcome to Our School System!</h2>
        <p>Your student account has been created. Please verify your email address:</p>

        <a href="{verification_url}"
           style="display:inline-block;padding:10px 20px;background-color:#4CAF50;
                  color:#fff;text-decoration:none;border-radius:5px;font-weight:bold;">
            Verify Your Email
        </a>

        <p>Your account details:</p>
        <ul>
            <li>Index Number: {index_number}</li>
            <li>Class: {class_name_display}</li>
        </ul>

        <p><strong>Parent / Guardian on record:</strong></p>
        <ul>{guardian_html}</ul>

        <p>If you did not expect this email, please contact the school administration.</p>
        <br>
        <p>Best regards,<br>School Administration</p>
    </body>
    </html>
    """

    try:
        _dispatch_email(to_email, "Verify Your Student Account", html_body)
    except ApiException as exc:
        logger.error("Batch email error for %s: %s", to_email, exc)
        raise self.retry(exc=exc, countdown=60 * (2 ** self.request.retries))


@shared_task(
    bind=True,
    max_retries=3,
    default_retry_delay=60,
    name="student_auth.tasks.send_generic_email",
)
def send_generic_email(self, to_email: str, subject: str, html_body: str) -> None:
    """
    General-purpose async email task.  Use this for any ad-hoc transactional
    emails (password-reset notifications, admin alerts, etc.).
    """
    try:
        _dispatch_email(to_email, subject, html_body)
    except ApiException as exc:
        logger.error("Generic email error for %s: %s", to_email, exc)
        raise self.retry(exc=exc, countdown=60 * (2 ** self.request.retries))