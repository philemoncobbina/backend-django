import logging
from datetime import timedelta
from typing import Callable, Dict, List, Optional, Set, Tuple

import requests
from celery import shared_task
from django.conf import settings
from django.db.models import Q
from django.utils import timezone
from django.utils.html import escape

from authapp.models import ParentGuardian
from .models import (
    AttendanceNotificationLog,
    AttendanceRecord,
    ClassAttendanceRegister,
)

logger = logging.getLogger(__name__)


BREVO_EMAIL_URL = "https://api.brevo.com/v3/smtp/email"

CHANNEL_EMAIL = AttendanceNotificationLog.CHANNEL_EMAIL
CHANNEL_SMS = AttendanceNotificationLog.CHANNEL_SMS

# If a worker claims a notification (status=pending) and then dies before it
# records the outcome, the claim is treated as abandoned after this long and
# another run is allowed to pick it up. Must be comfortably longer than one
# provider call (both use a 20s timeout).
PENDING_CLAIM_TIMEOUT = timedelta(minutes=10)


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


# ---------------------------------------------------------------------------
# De-duplication helpers
#
# A register can be finalized many times (finalize -> principal reopens ->
# correction -> finalize again). Each finalize queues this task, so the task
# itself must guarantee that a guardian is told about a given absence only
# once per register, per channel.
# ---------------------------------------------------------------------------


def _skipped_result(channel: str, reason: str) -> Dict:
    """
    Result for a notification that was deliberately not sent.

    `success` is False so it is never counted as a fresh send; the
    `skipped` flag distinguishes it from a real failure.
    """
    return {
        "success": False,
        "skipped": True,
        "channel": channel,
        "reason": reason,
    }


def _claim_notification(
    *,
    register: ClassAttendanceRegister,
    student,
    channel: str,
    recipient: str,
) -> Optional[AttendanceNotificationLog]:
    """
    Atomically claims the right to send one (register, student, channel)
    notification. Returns the log row if THIS run should send it, or None if
    it must not (already sent, or another worker is sending it right now).

    - No row yet            -> create it as pending and send.
    - Row is 'sent'         -> never send again.
    - Row is 'failed'       -> re-claim and retry.
    - Row is 'pending'      -> someone else is mid-send; only re-claim if the
                               claim is stale (worker likely crashed).

    The unique constraint on the model makes get_or_create race-safe, and the
    conditional UPDATE below makes re-claiming race-safe: only one caller can
    flip the row back to pending.
    """
    log, created = AttendanceNotificationLog.objects.get_or_create(
        register=register,
        student=student,
        channel=channel,
        defaults={
            "status": AttendanceNotificationLog.STATUS_PENDING,
            "recipient": recipient,
        },
    )

    if created:
        return log

    if log.status == AttendanceNotificationLog.STATUS_SENT:
        return None

    now = timezone.now()
    claimable = Q(status=AttendanceNotificationLog.STATUS_FAILED) | Q(
        status=AttendanceNotificationLog.STATUS_PENDING,
        updated_at__lt=now - PENDING_CLAIM_TIMEOUT,
    )

    claimed = (
        AttendanceNotificationLog.objects.filter(pk=log.pk)
        .filter(claimable)
        .update(
            status=AttendanceNotificationLog.STATUS_PENDING,
            recipient=recipient,
            updated_at=now,  # .update() bypasses auto_now
        )
    )

    if not claimed:
        return None

    log.refresh_from_db()
    return log


def _record_outcome(log: AttendanceNotificationLog, result: Dict) -> None:
    if result.get("success") is True:
        log.status = AttendanceNotificationLog.STATUS_SENT
        log.sent_at = timezone.now()
        log.error = ""
    else:
        log.status = AttendanceNotificationLog.STATUS_FAILED
        log.error = str(result.get("error", ""))[:1000]

    log.save(update_fields=["status", "sent_at", "error", "updated_at"])


def _send_once(
    *,
    register: ClassAttendanceRegister,
    student,
    channel: str,
    recipient: str,
    send: Callable[[], Dict],
) -> Dict:
    """
    Runs `send()` only if this (register, student, channel) has not already
    been delivered, and records the outcome so later runs skip it.
    """
    log = _claim_notification(
        register=register,
        student=student,
        channel=channel,
        recipient=recipient,
    )

    if log is None:
        return _skipped_result(
            channel,
            "Already notified for this register (or being sent by another worker).",
        )

    try:
        result = send()
    except Exception as exc:  # never leave the claim stuck in 'pending'
        logger.exception(
            "Unexpected error sending %s notification. register_id=%s student_id=%s",
            channel,
            register.id,
            student.id,
        )
        result = {"success": False, "channel": channel, "error": str(exc)}

    _record_outcome(log, result)
    return result


def _already_sent_channels(register: ClassAttendanceRegister) -> Dict[int, Set[str]]:
    """
    One query: {student_id: {channels already delivered}} for this register.
    Lets the loop skip fully-notified students without extra per-student
    queries or building messages.
    """
    sent: Dict[int, Set[str]] = {}

    rows = AttendanceNotificationLog.objects.filter(
        register=register,
        status=AttendanceNotificationLog.STATUS_SENT,
    ).values_list("student_id", "channel")

    for student_id, channel in rows:
        sent.setdefault(student_id, set()).add(channel)

    return sent


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

    Idempotent per (register, student, channel): every delivery is recorded in
    AttendanceNotificationLog, so a guardian who has already been told about
    an absence is NOT contacted again when the register is corrected and
    re-finalized, or when this task is re-run manually.

    What this means in practice:
    - Student still absent after a correction   -> no repeat message.
    - Student newly marked absent in a correction -> notified (first time).
    - Student changed to present                -> nothing is sent.
    - A previous send failed                    -> only that channel is retried.
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

    already_sent = _already_sent_channels(register)

    results: List[Dict] = []

    for record in absent_records:
        student = record.student
        student_name = _student_full_name(student)
        sent_channels = already_sent.get(student.id, set())

        # Fast path: both channels already delivered on an earlier finalize.
        if CHANNEL_EMAIL in sent_channels and CHANNEL_SMS in sent_channels:
            results.append({
                "student_id": student.id,
                "student_name": student_name,
                "email": _skipped_result(CHANNEL_EMAIL, "Already notified."),
                "sms": _skipped_result(CHANNEL_SMS, "Already notified."),
            })
            logger.info(
                "Attendance notification skipped (already sent). "
                "register_id=%s student_id=%s",
                register.id,
                student.id,
            )
            continue

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

        # ----- Email -----
        if CHANNEL_EMAIL in sent_channels:
            email_result = _skipped_result(CHANNEL_EMAIL, "Already notified.")
        elif guardian.email:
            email_result = _send_once(
                register=register,
                student=student,
                channel=CHANNEL_EMAIL,
                recipient=guardian.email,
                send=lambda: _send_brevo_email(
                    to_email=guardian.email,
                    to_name=guardian_name,
                    subject=subject,
                    text_body=text_body,
                    html_body=html_body,
                ),
            )
        else:
            email_result = {
                "success": False,
                "channel": "email",
                "error": "Guardian email is missing.",
            }

        # ----- SMS -----
        if CHANNEL_SMS in sent_channels:
            sms_result = _skipped_result(CHANNEL_SMS, "Already notified.")
        elif guardian.primary_phone:
            sms_result = _send_once(
                register=register,
                student=student,
                channel=CHANNEL_SMS,
                recipient=guardian.primary_phone,
                send=lambda: _send_twilio_sms(
                    to_phone=guardian.primary_phone,
                    sms_body=sms_body,
                ),
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
            "register_id=%s student_id=%s guardian_id=%s "
            "email_success=%s email_skipped=%s sms_success=%s sms_skipped=%s",
            register.id,
            student.id,
            guardian.id,
            email_result.get("success"),
            email_result.get("skipped", False),
            sms_result.get("success"),
            sms_result.get("skipped", False),
        )

    email_success_count = sum(
        1 for item in results
        if item.get("email", {}).get("success") is True
    )

    sms_success_count = sum(
        1 for item in results
        if item.get("sms", {}).get("success") is True
    )

    email_skipped_count = sum(
        1 for item in results
        if item.get("email", {}).get("skipped") is True
    )

    sms_skipped_count = sum(
        1 for item in results
        if item.get("sms", {}).get("skipped") is True
    )

    return {
        "success": True,
        "register_id": register.id,
        "class_name": register.class_name,
        "date": str(register.date),
        "total_absent": total_absent,
        "email_success_count": email_success_count,
        "sms_success_count": sms_success_count,
        "email_skipped_count": email_skipped_count,
        "sms_skipped_count": sms_skipped_count,
        "results": results,
    }