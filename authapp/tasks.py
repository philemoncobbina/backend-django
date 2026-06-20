"""
authapp/tasks.py
================
Celery tasks that handle all heavy / blocking work that was previously
done inline (or in raw threads) inside the API views.

Tasks defined here
------------------
- send_verification_email_task       : post-signup email verification link
- send_login_alert_task              : login-notification email with geo/device info
- send_password_reset_code_task      : shared password-reset code mailer
- send_change_password_code_task     : authenticated change-password code mailer

All tasks use:
  - bind=True              → gives access to self for retry logic
  - autoretry_for          → automatic retries on transient Brevo / network errors
  - max_retries=3
  - default_retry_delay=60 → 60-second back-off between retries
  - acks_late=True         → task is only acknowledged after it completes,
                             so a worker crash won't silently swallow a job
"""

import logging
import os
import platform

import requests as http_requests
from celery import shared_task
from django.conf import settings
from django.template.loader import render_to_string
from django.urls import reverse
from rest_framework_simplejwt.tokens import RefreshToken
from sib_api_v3_sdk import (
    ApiClient,
    Configuration,
    SendSmtpEmail,
    TransactionalEmailsApi,
)
from sib_api_v3_sdk.rest import ApiException

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Internal helpers (not exposed as tasks)
# ---------------------------------------------------------------------------

def _brevo_client() -> TransactionalEmailsApi:
    """Return a configured Brevo transactional-email API client."""
    configuration = Configuration()
    configuration.api_key["api-key"] = settings.BREVO_API_KEY
    return TransactionalEmailsApi(ApiClient(configuration))


def _send_via_brevo(*, to_email: str, to_name: str = "", subject: str, html_content: str) -> None:
    """
    Low-level Brevo send helper.
    Raises ApiException on failure so callers / Celery retry logic can react.
    """
    api_instance = _brevo_client()
    recipient = {"email": to_email}
    if to_name:
        recipient["name"] = to_name

    send_smtp_email = SendSmtpEmail(
        to=[recipient],
        sender={"name": "Your Company", "email": settings.DEFAULT_FROM_EMAIL},
        subject=subject,
        html_content=html_content,
    )
    api_response = api_instance.send_transac_email(send_smtp_email)
    logger.info("Brevo email sent to %s | response: %s", to_email, api_response)


def _get_location_data() -> dict:
    """
    Fetch geo-location data from ipinfo.io.
    Returns a safe fallback dict on any failure so the login flow is never blocked.
    """
    fallback = {"ip": "N/A", "city": "N/A", "country": "N/A", "region": "N/A", "loc": "N/A"}
    try:
        token = os.getenv("IPINFO_TOKEN", "")
        response = http_requests.get(
            f"https://ipinfo.io/json?token={token}",
            timeout=5,
        )
        if response.status_code == 200:
            data = response.json()
            logger.debug("Location data fetched: %s", data)
            return {
                "ip":      data.get("ip",      "N/A"),
                "city":    data.get("city",     "N/A"),
                "country": data.get("country",  "N/A"),
                "region":  data.get("region",   "N/A"),
                "loc":     data.get("loc",      "N/A"),
            }
        logger.warning("ipinfo returned %s", response.status_code)
    except Exception as exc:  # noqa: BLE001
        logger.error("Error fetching location data: %s", exc)
    return fallback


# ---------------------------------------------------------------------------
# Task 1 — Signup verification email
# ---------------------------------------------------------------------------

@shared_task(
    bind=True,
    autoretry_for=(ApiException, Exception),
    max_retries=3,
    default_retry_delay=60,
    acks_late=True,
    name="authapp.tasks.send_verification_email_task",
)
def send_verification_email_task(self, user_id: int, base_url: str) -> None:
    """
    Generate a JWT verification token for *user_id* and send the email
    verification link.

    Parameters
    ----------
    user_id  : PK of the CustomUser to verify
    base_url : scheme + host from the original request
               (e.g. "https://api.plvcmonline.uk") used to build the
               absolute verification URL — the request object itself is
               not serialisable and cannot be passed to a Celery task.
    """
    from django.contrib.auth import get_user_model  # local import avoids circular refs at module load

    User = get_user_model()
    try:
        user = User.objects.get(pk=user_id)
    except User.DoesNotExist:
        logger.error("send_verification_email_task: user %s not found — aborting.", user_id)
        return  # non-retryable; user was deleted between enqueue and execution

    try:
        token = RefreshToken.for_user(user).access_token
        relative_url = reverse("verify-email", kwargs={"token": str(token)})
        verification_url = f"{base_url}{relative_url}"

        html_content = render_to_string(
            "email_verification.html",
            {"verification_url": verification_url},
        )

        _send_via_brevo(
            to_email=user.email,
            subject="Verify Your Email",
            html_content=html_content,
        )
        logger.info("Verification email sent to %s", user.email)

    except ApiException as exc:
        logger.error("Brevo error for %s: %s", user.email, exc)
        raise self.retry(exc=exc)
    except Exception as exc:
        logger.error("Unexpected error sending verification email to %s: %s", user.email, exc)
        raise self.retry(exc=exc)


# ---------------------------------------------------------------------------
# Task 2 — Login alert email (includes async geo-lookup)
# ---------------------------------------------------------------------------

@shared_task(
    bind=True,
    autoretry_for=(ApiException,),
    max_retries=3,
    default_retry_delay=60,
    acks_late=True,
    name="authapp.tasks.send_login_alert_task",
)
def send_login_alert_task(self, user_id: int, base_url: str) -> None:
    """
    Fetch the server's geo-location, render the login-alert template,
    and send it to the user.

    Parameters
    ----------
    user_id  : PK of the logged-in CustomUser
    base_url : used to build the re-verification URL embedded in the alert
    """
    from django.contrib.auth import get_user_model

    User = get_user_model()
    try:
        user = User.objects.get(pk=user_id)
    except User.DoesNotExist:
        logger.error("send_login_alert_task: user %s not found — aborting.", user_id)
        return

    try:
        # Geo-lookup happens inside the worker, not on the API process
        location_data = _get_location_data()

        device_info = {
            "os":   platform.system(),
            "name": platform.node(),
        }

        token = RefreshToken.for_user(user).access_token
        relative_url = reverse("verify-email", kwargs={"token": str(token)})
        verification_url = f"{base_url}{relative_url}"

        context = {
            "verification_url": verification_url,
            "first_name":       user.first_name,
            "city":             location_data.get("city"),
            "country_name":     location_data.get("country"),
            "ip_address":       location_data.get("ip"),
            "device_os":        device_info["os"],
            "device_name":      device_info["name"],
        }
        html_content = render_to_string("login_alert.html", context)

        _send_via_brevo(
            to_email=user.email,
            to_name=user.first_name,
            subject="New Login Alert",
            html_content=html_content,
        )
        logger.info("Login alert sent to %s", user.email)

    except ApiException as exc:
        logger.error("Brevo error sending login alert to user %s: %s", user_id, exc)
        raise self.retry(exc=exc)
    except Exception as exc:
        logger.error("Unexpected error sending login alert to user %s: %s", user_id, exc)
        # Don't retry non-Brevo errors (e.g. template render failures) to avoid
        # infinite loops; just log and bail.
        logger.exception(exc)


# ---------------------------------------------------------------------------
# Task 3 — Password reset code (student / admin / website portals)
# ---------------------------------------------------------------------------

@shared_task(
    bind=True,
    autoretry_for=(ApiException,),
    max_retries=3,
    default_retry_delay=60,
    acks_late=True,
    name="authapp.tasks.send_password_reset_code_task",
)
def send_password_reset_code_task(
    self,
    to_email: str,
    verification_code: str,
    subject: str = "Password Reset Verification Code",
) -> None:
    """
    Render and send a password-reset verification-code email.

    Parameters
    ----------
    to_email          : recipient address
    verification_code : the 6-digit code to embed in the email
    subject           : email subject line (defaults to the standard reset subject)
    """
    try:
        html_content = render_to_string(
            "password_reset_verification.html",
            {"verification_code": verification_code},
        )
        _send_via_brevo(to_email=to_email, subject=subject, html_content=html_content)
        logger.info("Password reset code sent to %s", to_email)

    except ApiException as exc:
        logger.error("Brevo error sending reset code to %s: %s", to_email, exc)
        raise self.retry(exc=exc)
    except Exception as exc:
        logger.error("Unexpected error sending reset code to %s: %s", to_email, exc)
        raise self.retry(exc=exc)


# ---------------------------------------------------------------------------
# Task 4 — Change-password verification code (authenticated users)
# ---------------------------------------------------------------------------

@shared_task(
    bind=True,
    autoretry_for=(ApiException,),
    max_retries=3,
    default_retry_delay=60,
    acks_late=True,
    name="authapp.tasks.send_change_password_code_task",
)
def send_change_password_code_task(
    self,
    to_email: str,
    verification_code: str,
) -> None:
    """
    Send the authenticated change-password verification code.

    Parameters
    ----------
    to_email          : recipient address (always the authenticated user's own email)
    verification_code : the 6-digit code
    """
    try:
        html_content = render_to_string(
            "change_password_verification.html",
            {"verification_code": verification_code},
        )
        _send_via_brevo(
            to_email=to_email,
            subject="Change Password Verification Code",
            html_content=html_content,
        )
        logger.info("Change-password code sent to %s", to_email)

    except ApiException as exc:
        logger.error("Brevo error sending change-password code to %s: %s", to_email, exc)
        raise self.retry(exc=exc)
    except Exception as exc:
        logger.error("Unexpected error sending change-password code to %s: %s", to_email, exc)
        raise self.retry(exc=exc)