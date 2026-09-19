import logging
from typing import Dict, List, Optional, Tuple

import requests
from celery import shared_task
from django.conf import settings
from django.utils.html import escape

from authapp.models import ParentGuardian
from .models import ClassAttendanceRegister, AttendanceRecord

logger = logging.getLogger(__name__)


BREVO_EMAIL_URL = "https://api.brevo.com/v3/smtp/email"


def _student_full_name(student) -> str:
    name = f"{student.first_name} {student.last_name}".strip()
    return name or student.username or student.email


def _guardian_full_name(guardian: ParentGuardian) -> str:
    name = getattr(guardian, "full_name", "") or ""
    if name.strip():
        return name.strip()

    parts = [
        getattr(guardian, "first_name", ""),
        getattr(guardian, "middle_name", ""),
        getattr(guardian, "last_name", ""),
    ]
    return " ".join(part for part in parts if part).strip() or "Parent/Guardian"


def _get_primary_guardian(student) -> Optional[ParentGuardian]:
    """
    Returns the primary guardian for a student.

    We intentionally use only is_primary_contact=True because the requirement is
    to notify the primary guardian.
    """
    return (
        ParentGuardian.objects.filter(
            student=student,
            is_primary_contact=True,
        )
        .order_by("-updated_at", "-created_at", "id")
        .first()
    )


def _build_absence_message(
    *,
    guardian_name: str,
    student_name: str,
    class_name: str,
    date,
    absence_reason: Optional[str],
) -> Tuple[str, str, str]:
    """
    Returns subject, plain text body, and HTML body.
    """
    reason_text = absence_reason.strip() if absence_reason else ""
    reason_line = f"\nReason recorded: {reason_text}" if reason_text else ""

    subject = f"Attendance Notice: {student_name} marked absent"

    text_body = (
        f"Dear {guardian_name},\n\n"
        f"This is to notify you that your ward, {student_name}, "
        f"was marked absent from school on {date} for {class_name}."
        f"{reason_line}\n\n"
        "If you believe this record is incorrect, kindly contact the school office.\n\n"
        "Thank you."
    )

    html_reason = (
        f"<p><strong>Reason recorded:</strong> {escape(reason_text)}</p>"
        if reason_text
        else ""
    )

    html_body = f"""
    <p>Dear {escape(guardian_name)},</p>

    <p>
        This is to notify you that your ward,
        <strong>{escape(student_name)}</strong>,
        was marked absent from school on
        <strong>{escape(str(date))}</strong>
        for <strong>{escape(class_name)}</strong>.
    </p>

    {html_reason}

    <p>
        If you believe this record is incorrect, kindly contact the school office.
    </p>

    <p>Thank you.</p>
    """

    return subject, text_body, html_body


def _send_brevo_email(
    *,
    to_email: str,
    to_name: str,
    subject: str,
    text_body: str,
    html_body: str,
) -> Dict:
    """
    Sends an email through Brevo Transactional Email API.

    Required settings:
    - BREVO_API_KEY
    - DEFAULT_FROM_EMAIL
    """
    brevo_api_key = getattr(settings, "BREVO_API_KEY", None)
    default_from_email = getattr(settings, "DEFAULT_FROM_EMAIL", None)

    if not brevo_api_key:
        return {
            "success": False,
            "channel": "email",
            "error": "BREVO_API_KEY is not configured.",
        }

    if not default_from_email:
        return {
            "success": False,
            "channel": "email",
            "error": "DEFAULT_FROM_EMAIL is not configured.",
        }

    payload = {
        "sender": {
            "name": getattr(settings, "SCHOOL_NAME", "School"),
            "email": default_from_email,
        },
        "to": [
            {
                "email": to_email,
                "name": to_name,
            }
        ],
        "subject": subject,
        "htmlContent": html_body,
        "textContent": text_body,
    }

    headers = {
        "accept": "application/json",
        "api-key": brevo_api_key,
        "content-type": "application/json",
    }

    try:
        response = requests.post(
            BREVO_EMAIL_URL,
            json=payload,
            headers=headers,
            timeout=20,
        )

        if 200 <= response.status_code < 300:
            return {
                "success": True,
                "channel": "email",
                "status_code": response.status_code,
                "response": response.json() if response.content else {},
            }

        return {
            "success": False,
            "channel": "email",
            "status_code": response.status_code,
            "error": response.text,
        }

    except requests.RequestException as exc:
        logger.exception("Brevo email request failed.")
        return {
            "success": False,
            "channel": "email",
            "error": str(exc),
        }


def _send_twilio_sms(*, to_phone: str, sms_body: str) -> Dict:
    """
    Sends SMS through Twilio REST API using requests.

    Required settings:
    - TWILIO_ACCOUNT_SID
    - TWILIO_AUTH_TOKEN
    - TWILIO_PHONE_NUMBER
    """
    account_sid = getattr(settings, "TWILIO_ACCOUNT_SID", None)
    auth_token = getattr(settings, "TWILIO_AUTH_TOKEN", None)
    from_phone = getattr(settings, "TWILIO_PHONE_NUMBER", None)

    if not account_sid or not auth_token or not from_phone:
        return {
            "success": False,
            "channel": "sms",
            "error": "Twilio settings are not fully configured.",
        }

    if not to_phone:
        return {
            "success": False,
            "channel": "sms",
            "error": "Guardian phone number is missing.",
        }

    cleaned_phone = to_phone.strip()

    if not cleaned_phone.startswith("+"):
        return {
            "success": False,
            "channel": "sms",
            "error": (
                "Guardian phone number must include the country code, "
                "for example +233241234567."
            ),
        }

    url = (
        f"https://api.twilio.com/2010-04-01/Accounts/"
        f"{account_sid}/Messages.json"
    )

    data = {
        "From": from_phone,
        "To": cleaned_phone,
        "Body": sms_body,
    }

    try:
        response = requests.post(
            url,
            data=data,
            auth=(account_sid, auth_token),
            timeout=20,
        )

        if 200 <= response.status_code < 300:
            return {
                "success": True,
                "channel": "sms",
                "status_code": response.status_code,
                "response": response.json() if response.content else {},
            }

        return {
            "success": False,
            "channel": "sms",
            "status_code": response.status_code,
            "error": response.text,
        }

    except requests.RequestException as exc:
        logger.exception("Twilio SMS request failed.")
        return {
            "success": False,
            "channel": "sms",
            "error": str(exc),
        }


@shared_task(
    bind=True,
    autoretry_for=(requests.RequestException,),
    retry_backoff=True,
    retry_kwargs={"max_retries": 3},
)
def notify_absent_students_after_finalize(self, register_id: int) -> Dict:
    """
    Sends email and SMS to the primary guardian of every absent student
    after a class attendance register is finalized.

    This task is intentionally idempotent at the notification-result level only.
    It does not create a separate notification database record, so if you manually
    rerun the task, parents may receive the notification again.

    Recommended future improvement:
    Create an AttendanceNotificationLog model to permanently prevent duplicate
    notifications per register, student, and channel.
    """
    try:
        register = (
            ClassAttendanceRegister.objects.select_related("term", "marked_by")
            .get(pk=register_id)
        )
    except ClassAttendanceRegister.DoesNotExist:
        logger.warning(
            "Attendance notification skipped. Register %s does not exist.",
            register_id,
        )
        return {
            "success": False,
            "register_id": register_id,
            "error": "Register does not exist.",
        }

    if not register.is_finalized:
        logger.info(
            "Attendance notification skipped. Register %s is not finalized.",
            register_id,
        )
        return {
            "success": False,
            "register_id": register_id,
            "error": "Register is not finalized.",
        }

    absent_records = (
        AttendanceRecord.objects.select_related("student", "register")
        .filter(
            register=register,
            status="absent",
        )
        .order_by("student__last_name", "student__first_name", "student__id")
    )

    total_absent = absent_records.count()

    if total_absent == 0:
        logger.info(
            "No absent students found for finalized register %s.",
            register_id,
        )
        return {
            "success": True,
            "register_id": register_id,
            "total_absent": 0,
            "message": "No absent students to notify.",
        }

    results: List[Dict] = []

    for record in absent_records:
        student = record.student
        student_name = _student_full_name(student)

        guardian = _get_primary_guardian(student)

        if not guardian:
            results.append({
                "student_id": student.id,
                "student_name": student_name,
                "success": False,
                "error": "No primary guardian found.",
            })
            logger.warning(
                "No primary guardian found for absent student %s.",
                student.id,
            )
            continue

        guardian_name = _guardian_full_name(guardian)

        subject, text_body, html_body = _build_absence_message(
            guardian_name=guardian_name,
            student_name=student_name,
            class_name=register.class_name,
            date=register.date,
            absence_reason=record.absence_reason,
        )

        sms_body = (
            f"Dear {guardian_name}, your ward {student_name} was marked absent "
            f"on {register.date} for {register.class_name}. "
            "If this is incorrect, kindly contact the school office."
        )

        email_result = None
        sms_result = None

        if guardian.email:
            email_result = _send_brevo_email(
                to_email=guardian.email,
                to_name=guardian_name,
                subject=subject,
                text_body=text_body,
                html_body=html_body,
            )
        else:
            email_result = {
                "success": False,
                "channel": "email",
                "error": "Guardian email is missing.",
            }

        if guardian.primary_phone:
            sms_result = _send_twilio_sms(
                to_phone=guardian.primary_phone,
                sms_body=sms_body,
            )
        else:
            sms_result = {
                "success": False,
                "channel": "sms",
                "error": "Guardian primary phone is missing.",
            }

        student_result = {
            "student_id": student.id,
            "student_name": student_name,
            "guardian_id": guardian.id,
            "guardian_name": guardian_name,
            "guardian_email": guardian.email,
            "guardian_phone": guardian.primary_phone,
            "email": email_result,
            "sms": sms_result,
        }

        results.append(student_result)

        logger.info(
            "Attendance absence notification processed. "
            "register_id=%s student_id=%s guardian_id=%s email_success=%s sms_success=%s",
            register.id,
            student.id,
            guardian.id,
            email_result.get("success") if email_result else False,
            sms_result.get("success") if sms_result else False,
        )

    email_success_count = sum(
        1 for item in results
        if item.get("email", {}).get("success") is True
    )

    sms_success_count = sum(
        1 for item in results
        if item.get("sms", {}).get("success") is True
    )

    return {
        "success": True,
        "register_id": register.id,
        "class_name": register.class_name,
        "date": str(register.date),
        "total_absent": total_absent,
        "email_success_count": email_success_count,
        "sms_success_count": sms_success_count,
        "results": results,
    }

