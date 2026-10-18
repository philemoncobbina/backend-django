import logging
from datetime import timedelta

import jwt
from celery import shared_task
from django.conf import settings
from django.contrib.auth import get_user_model
from django.urls import reverse
from django.utils import timezone
from django.utils.html import escape
from sib_api_v3_sdk import ApiClient, Configuration, SendSmtpEmail
from sib_api_v3_sdk.api.transactional_emails_api import TransactionalEmailsApi
from sib_api_v3_sdk.rest import ApiException

logger = logging.getLogger(__name__)


def make_email_verification_token(user) -> str:
    """A token that can do exactly one thing: verify this user's email."""
    now = timezone.now()
    hours = getattr(settings, "ADMIN_EMAIL_VERIFICATION_HOURS", 48)
    payload = {
        "user_id": user.id,
        "token_type": "email_verification",
        "iat": now,
        "exp": now + timedelta(hours=hours),
    }
    return jwt.encode(payload, settings.SECRET_KEY, algorithm="HS256")


@shared_task(
    bind=True,
    autoretry_for=(ApiException,),
    retry_backoff=True,
    retry_backoff_max=600,
    max_retries=3,
)
def send_admin_verification_email_task(self, user_id: int, base_url: str):
    User = get_user_model()
    user = User.objects.filter(id=user_id).first()
    if not user or user.is_active:
        return  # nothing left to verify

    path = reverse(
        "admin_auth:verify-email",
        kwargs={"token": make_email_verification_token(user)},
    )
    url = f"{base_url.rstrip('/')}{path}"
    hours = getattr(settings, "ADMIN_EMAIL_VERIFICATION_HOURS", 48)
    sender_name = getattr(settings, "ADMIN_EMAIL_SENDER_NAME", "School Administration")

    html = f"""
    <html>
    <body style="font-family: Arial, sans-serif; color: #333;">
      <p>Dear {escape(user.first_name or "")},</p>
      <h2>Welcome!</h2>
      <p>An administrator account has been created for you. Please verify your email address:</p>
      <p>
        <a href="{escape(url)}"
           style="background:#2f5d9e;color:#fff;padding:10px 18px;border-radius:6px;text-decoration:none;">
          Verify your email
        </a>
      </p>
      <p>This link expires in {hours} hours. If you were not expecting this email, please ignore it.</p>
      <p>Best regards,<br>{escape(sender_name)}</p>
    </body>
    </html>
    """

    configuration = Configuration()
    configuration.api_key["api-key"] = settings.BREVO_API_KEY
    api = TransactionalEmailsApi(ApiClient(configuration))

    message = SendSmtpEmail(
        to=[{"email": user.email}],
        sender={"name": sender_name, "email": settings.DEFAULT_FROM_EMAIL},
        subject="Verify your email",
        html_content=html,
    )
    api.send_transac_email(message)
    logger.info("Admin verification email sent for user id=%s", user.id)