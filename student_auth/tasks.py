# student_auth/tasks.py

"""
Celery tasks for student email verification.

The verification link generated here points to the dedicated student
verification endpoint. Student tokens must never be sent to the general
authapp verification endpoint because the two endpoints accept different
token types.
"""

import logging
from datetime import datetime, timedelta, timezone

import jwt
from celery import shared_task
from django.conf import settings
from django.contrib.auth import get_user_model
from django.urls import reverse
from django.utils.html import escape
from sib_api_v3_sdk import (
    ApiClient,
    Configuration,
    SendSmtpEmail,
)
from sib_api_v3_sdk.api.transactional_emails_api import (
    TransactionalEmailsApi,
)
from sib_api_v3_sdk.rest import ApiException

logger = logging.getLogger(__name__)

STUDENT_VERIFY_TOKEN_TYPE = "student_email_verification"


def make_student_verification_token(user) -> str:
    """
    Generate a JWT that can only be used to verify a student account.

    The token is intentionally different from:
        authapp email verification tokens
        Simple JWT access tokens
        Simple JWT refresh tokens
    """
    now = datetime.now(timezone.utc)
    hours = getattr(
        settings,
        "STUDENT_EMAIL_VERIFICATION_HOURS",
        168,
    )

    payload = {
        "user_id": user.id,
        "token_type": STUDENT_VERIFY_TOKEN_TYPE,
        "iat": now,
        "exp": now + timedelta(hours=hours),
    }

    return jwt.encode(
        payload,
        settings.SECRET_KEY,
        algorithm="HS256",
    )


def _build_student_verification_url(user, base_url: str) -> str:
    """
    Build the complete student verification URL.

    The namespaced URL ensures that reverse() always resolves the route
    from student_auth/urls.py instead of a similarly named route in
    another application.
    """
    token = make_student_verification_token(user)

    path = reverse(
        "student_auth:student-verify-email",
        kwargs={"token": token},
    )

    return f"{base_url.rstrip('/')}{path}"


def _get_brevo_client() -> TransactionalEmailsApi:
    """
    Return a configured Brevo transactional email client.
    """
    configuration = Configuration()
    configuration.api_key["api-key"] = settings.BREVO_API_KEY

    return TransactionalEmailsApi(
        ApiClient(configuration)
    )


def _guardian_rows_html(user) -> str:
    """
    Return a safe HTML list containing the student's guardians.

    Guardian identification numbers and other sensitive document
    information are deliberately excluded.
    """
    rows = []

    for guardian in user.guardians.all():
        full_name = escape(guardian.full_name or "")
        relationship = escape(
            guardian.get_relationship_display() or ""
        )

        primary_text = (
            " - primary contact"
            if guardian.is_primary_contact
            else ""
        )

        rows.append(
            f"<li>{full_name} ({relationship}){primary_text}</li>"
        )

    if not rows:
        return ""

    return (
        "<p>Guardians on file:</p>"
        f"<ul>{''.join(rows)}</ul>"
    )


@shared_task(
    bind=True,
    autoretry_for=(ApiException,),
    retry_backoff=True,
    retry_backoff_max=600,
    max_retries=3,
    acks_late=True,
    name="student_auth.tasks.send_student_verification_email",
)
def send_student_verification_email(
    self,
    user_id: int,
    base_url: str,
) -> None:
    """
    Send a student account verification email.

    Only the user ID and backend base URL are passed through Celery.
    The student's password, guardian information, and verification token
    are not placed in the Celery task arguments.

    The task exits without sending an email when:
        - the student does not exist
        - the account is not a student account
        - the account is already active
        - the account is blocked
    """
    User = get_user_model()

    user = (
        User.objects
        .filter(
            id=user_id,
            role="student",
        )
        .prefetch_related("guardians")
        .first()
    )

    if not user:
        logger.warning(
            "Student verification email was not sent because "
            "student user id=%s was not found.",
            user_id,
        )
        return

    if user.is_active:
        logger.info(
            "Student verification email was not sent because "
            "user id=%s is already active.",
            user.id,
        )
        return

    if user.is_blocked:
        logger.info(
            "Student verification email was not sent because "
            "user id=%s is blocked.",
            user.id,
        )
        return

    verification_url = _build_student_verification_url(
        user,
        base_url,
    )

    hours = getattr(
        settings,
        "STUDENT_EMAIL_VERIFICATION_HOURS",
        168,
    )

    sender_name = getattr(
        settings,
        "STUDENT_EMAIL_SENDER_NAME",
        getattr(
            settings,
            "ADMIN_EMAIL_SENDER_NAME",
            "School Administration",
        ),
    )

    guardian_html = _guardian_rows_html(user)

    first_name = escape(user.first_name or "Student")
    index_number = escape(user.index_number or "")

    try:
        class_display = user.get_class_name_display() or ""
    except (AttributeError, TypeError, ValueError):
        class_display = user.class_name or ""

    class_name = escape(str(class_display))

    # django.utils.html.escape() takes no `quote` kwarg — it always
    # escapes <, >, &, ' and ", so it is safe inside an HTML attribute.
    safe_verification_url = escape(verification_url)
    safe_sender_name = escape(sender_name)

    if hours % 24 == 0:
        expiry_text = f"{hours // 24} days"
    else:
        expiry_text = f"{hours} hours"

    html_content = f"""
    <!DOCTYPE html>
    <html lang="en">
    <head>
        <meta charset="UTF-8">
        <meta
            name="viewport"
            content="width=device-width, initial-scale=1.0"
        >
        <title>Verify your student account</title>
    </head>

    <body
        style="
            margin: 0;
            padding: 0;
            background-color: #f5f7fa;
            font-family: Arial, sans-serif;
            color: #333333;
        "
    >
        <div
            style="
                max-width: 620px;
                margin: 30px auto;
                padding: 30px;
                background-color: #ffffff;
                border-radius: 8px;
            "
        >
            <p>Dear {first_name},</p>

            <h2 style="color: #2f5d9e;">
                Your student account is ready
            </h2>

            <p>
                Index number:
                <strong>{index_number}</strong>
                <br>

                Class:
                <strong>{class_name}</strong>
            </p>

            <p>
                Please verify your email address to activate
                your student account.
            </p>

            <p style="margin: 30px 0;">
                <a
                    href="{safe_verification_url}"
                    style="
                        display: inline-block;
                        background-color: #2f5d9e;
                        color: #ffffff;
                        padding: 12px 20px;
                        border-radius: 6px;
                        text-decoration: none;
                        font-weight: bold;
                    "
                >
                    Verify your email
                </a>
            </p>

            <p>
                This link expires in {expiry_text}.
            </p>

            <p>
                Your password was set by the school administration.
                If you do not have it, use
                <strong>Forgot password</strong>
                on the student login page to choose a new one.
            </p>

            {guardian_html}

            <p>
                If you did not expect this email, please contact
                the school administration.
            </p>

            <p>
                Regards,<br>
                {safe_sender_name}
            </p>
        </div>
    </body>
    </html>
    """

    email = SendSmtpEmail(
        to=[
            {
                "email": user.email,
                "name": first_name,
            }
        ],
        sender={
            "name": sender_name,
            "email": settings.DEFAULT_FROM_EMAIL,
        },
        subject="Verify your student account",
        html_content=html_content,
    )

    client = _get_brevo_client()

    try:
        client.send_transac_email(email)
    except ApiException:
        logger.exception(
            "Failed to send student verification email for "
            "user id=%s.",
            user.id,
        )
        raise

    logger.info(
        "Student verification email sent for user id=%s.",
        user.id,
    )