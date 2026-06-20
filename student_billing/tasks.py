"""
billing/tasks.py
"""

import logging

from celery import shared_task
from django.utils import timezone

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# PDF generation tasks
# ---------------------------------------------------------------------------

@shared_task(
    bind=True,
    autoretry_for=(Exception,),
    retry_backoff=True,
    retry_kwargs={"max_retries": 3},
    name="billing.tasks.generate_bill_pdf_task",
)
def generate_bill_pdf_task(self, student_bill_id: int) -> dict:
    """
    Asynchronously generate (or regenerate) the PDF for a student bill and
    save it to the bill's ``pdf_file`` field.

    IMPORTANT:
    This task must NEVER call StudentBill.save() for the whole bill object,
    because that can re-trigger business logic and overwrite financial fields.

    Therefore:
      - save=False is used on the FileField
      - the DB row is updated directly for pdf_file only
    """
    from django.core.files.base import ContentFile
    from .models import StudentBill
    from .pdf_generator import generate_bill_pdf

    try:
        bill = StudentBill.objects.select_related(
            "student", "billing_template"
        ).prefetch_related(
            "billing_template__billing_items",
            "custom_charges",
            "payment_receipts",
        ).get(pk=student_bill_id)
    except StudentBill.DoesNotExist:
        logger.error(
            f"[generate_bill_pdf_task] StudentBill {student_bill_id} not found – skipping."
        )
        return {"success": False, "reason": "bill_not_found"}

    try:
        pdf_bytes = generate_bill_pdf(bill)

        filename = (
            f"bill_{bill.bill_number}_"
            f"{bill.billing_template.class_name}_"
            f"{bill.billing_template.get_term_display()}.pdf"
        ).replace(" ", "_")

        # CRITICAL FIX:
        # save=False prevents StudentBill.save() from being called again.
        bill.pdf_file.save(filename, ContentFile(pdf_bytes), save=False)

        # Update ONLY the pdf_file column directly.
        StudentBill.objects.filter(pk=bill.pk).update(pdf_file=bill.pdf_file.name)

        logger.info(
            f"[generate_bill_pdf_task] PDF generated for bill {bill.bill_number} "
            f"→ {bill.pdf_file.name}"
        )
        return {"success": True, "bill_number": bill.bill_number, "pdf": bill.pdf_file.name}

    except Exception as exc:
        logger.error(
            f"[generate_bill_pdf_task] Failed to generate PDF for bill "
            f"{student_bill_id}: {exc}"
        )
        raise


@shared_task(
    bind=True,
    autoretry_for=(Exception,),
    retry_backoff=True,
    retry_kwargs={"max_retries": 3},
    name="billing.tasks.generate_payment_receipt_pdf_task",
)
def generate_payment_receipt_pdf_task(self, payment_receipt_id: int) -> dict:
    """
    Regenerate the parent bill PDF after a payment receipt is recorded,
    so the bill PDF shows the latest payment history.
    """
    from .models import PaymentReceipt

    try:
        receipt = PaymentReceipt.objects.select_related("student_bill").get(pk=payment_receipt_id)
    except PaymentReceipt.DoesNotExist:
        logger.error(
            f"[generate_payment_receipt_pdf_task] PaymentReceipt {payment_receipt_id} "
            f"not found – skipping."
        )
        return {"success": False, "reason": "receipt_not_found"}

    generate_bill_pdf_task.delay(receipt.student_bill_id)

    logger.info(
        f"[generate_payment_receipt_pdf_task] Queued bill PDF regen for "
        f"receipt {receipt.receipt_number} (bill id={receipt.student_bill_id})"
    )
    return {"success": True, "receipt_number": receipt.receipt_number}


# ---------------------------------------------------------------------------
# Email tasks
# ---------------------------------------------------------------------------

@shared_task(
    bind=True,
    autoretry_for=(Exception,),
    retry_backoff=True,
    retry_kwargs={"max_retries": 3},
    name="billing.tasks.send_bill_published_email_task",
)
def send_bill_published_email_task(self, student_bill_id: int) -> dict:
    from .models import StudentBill
    from .views import BillingEmailService

    try:
        bill = StudentBill.objects.select_related(
            "student", "billing_template"
        ).get(pk=student_bill_id)
    except StudentBill.DoesNotExist:
        logger.error(
            f"[send_bill_published_email_task] StudentBill {student_bill_id} not found – skipping."
        )
        return {"success": False, "reason": "bill_not_found"}

    success = BillingEmailService.send_bill_published_email(bill)
    logger.info(
        f"[send_bill_published_email_task] bill={bill.bill_number} "
        f"email_sent={success}"
    )
    return {"success": success, "bill_number": bill.bill_number}


@shared_task(
    bind=True,
    autoretry_for=(Exception,),
    retry_backoff=True,
    retry_kwargs={"max_retries": 3},
    name="billing.tasks.send_payment_receipt_email_task",
)
def send_payment_receipt_email_task(self, payment_receipt_id: int) -> dict:
    from .models import PaymentReceipt
    from .views import BillingEmailService

    try:
        receipt = PaymentReceipt.objects.select_related(
            "student_bill__student", "student_bill__billing_template"
        ).get(pk=payment_receipt_id)
    except PaymentReceipt.DoesNotExist:
        logger.error(
            f"[send_payment_receipt_email_task] PaymentReceipt {payment_receipt_id} not found – skipping."
        )
        return {"success": False, "reason": "receipt_not_found"}

    success = BillingEmailService.send_payment_receipt_email(receipt)
    logger.info(
        f"[send_payment_receipt_email_task] receipt={receipt.receipt_number} "
        f"email_sent={success}"
    )
    return {"success": success, "receipt_number": receipt.receipt_number}


# ---------------------------------------------------------------------------
# SMS tasks
# ---------------------------------------------------------------------------

@shared_task(
    bind=True,
    autoretry_for=(Exception,),
    retry_backoff=True,
    retry_kwargs={"max_retries": 3},
    name="billing.tasks.send_bill_published_sms_task",
)
def send_bill_published_sms_task(self, student_bill_id: int) -> dict:
    from .models import StudentBill
    from .views import BillingSMSService

    try:
        bill = StudentBill.objects.select_related(
            "student", "billing_template"
        ).get(pk=student_bill_id)
    except StudentBill.DoesNotExist:
        logger.error(
            f"[send_bill_published_sms_task] StudentBill {student_bill_id} not found – skipping."
        )
        return {"success": False, "reason": "bill_not_found"}

    sent_count = BillingSMSService.send_bill_published_sms(bill)
    logger.info(
        f"[send_bill_published_sms_task] bill={bill.bill_number} "
        f"sms_sent_count={sent_count}"
    )
    return {"sent_count": sent_count, "bill_number": bill.bill_number}


@shared_task(
    bind=True,
    autoretry_for=(Exception,),
    retry_backoff=True,
    retry_kwargs={"max_retries": 3},
    name="billing.tasks.send_payment_receipt_sms_task",
)
def send_payment_receipt_sms_task(self, payment_receipt_id: int) -> dict:
    from .models import PaymentReceipt
    from .views import BillingSMSService

    try:
        receipt = PaymentReceipt.objects.select_related(
            "student_bill__student"
        ).get(pk=payment_receipt_id)
    except PaymentReceipt.DoesNotExist:
        logger.error(
            f"[send_payment_receipt_sms_task] PaymentReceipt {payment_receipt_id} not found – skipping."
        )
        return {"success": False, "reason": "receipt_not_found"}

    sent_count = BillingSMSService.send_payment_receipt_sms(receipt)
    logger.info(
        f"[send_payment_receipt_sms_task] receipt={receipt.receipt_number} "
        f"sms_sent_count={sent_count}"
    )
    return {"sent_count": sent_count, "receipt_number": receipt.receipt_number}


# ---------------------------------------------------------------------------
# Periodic task: auto-publish scheduled bills
# ---------------------------------------------------------------------------

@shared_task(name="billing.tasks.auto_publish_scheduled_bills_task")
def auto_publish_scheduled_bills_task() -> dict:
    from .models import StudentBill

    now = timezone.now()
    bills_to_publish = StudentBill.objects.filter(
        status__in=["DRAFT", "SCHEDULED"],
        scheduled_date__isnull=False,
        scheduled_date__lte=now,
    ).select_related("student", "billing_template")

    published_ids = []

    for bill in bills_to_publish:
        bill.status = "PUBLISHED"
        bill.save(update_fields=["status"])

        # Explicit publish action: queue bill PDF and notifications
        generate_bill_pdf_task.delay(bill.pk)
        send_bill_published_email_task.delay(bill.pk)
        send_bill_published_sms_task.delay(bill.pk)

        published_ids.append(bill.pk)
        logger.info(
            f"[auto_publish_scheduled_bills_task] Published bill {bill.bill_number} "
            f"(id={bill.pk}); PDF + notifications queued."
        )

    logger.info(
        f"[auto_publish_scheduled_bills_task] Done – published {len(published_ids)} bill(s)."
    )
    return {"published_count": len(published_ids), "published_bill_ids": published_ids}


# ---------------------------------------------------------------------------
# Bulk-publish helper task
# ---------------------------------------------------------------------------

@shared_task(
    bind=True,
    autoretry_for=(Exception,),
    retry_backoff=True,
    retry_kwargs={"max_retries": 2},
    name="billing.tasks.publish_single_bill_task",
)
def publish_single_bill_task(self, bill_id: int, user_id: int) -> dict:
    from django.contrib.auth import get_user_model
    from .models import StudentBill

    User = get_user_model()

    try:
        bill = StudentBill.objects.select_related("student", "billing_template").get(pk=bill_id)
    except StudentBill.DoesNotExist:
        return {"success": False, "bill_id": bill_id, "reason": "not_found"}

    if bill.status == "PUBLISHED":
        return {"success": False, "bill_id": bill_id, "reason": "already_published"}

    if bill.status not in ["DRAFT", "SCHEDULED"]:
        return {
            "success": False,
            "bill_id": bill_id,
            "reason": f"invalid_status:{bill.status}",
        }

    try:
        user = User.objects.get(pk=user_id)
    except User.DoesNotExist:
        user = None

    bill.status = "PUBLISHED"
    bill._current_user = user
    bill.save(update_fields=["status"])

    generate_bill_pdf_task.delay(bill.pk)
    send_bill_published_email_task.delay(bill.pk)
    send_bill_published_sms_task.delay(bill.pk)

    logger.info(
        f"[publish_single_bill_task] Published bill {bill.bill_number} "
        f"(id={bill.pk}); PDF + notifications queued."
    )
    return {
        "success": True,
        "bill_id": bill.pk,
        "bill_number": bill.bill_number,
    }


# ---------------------------------------------------------------------------
# Balance recalculation task
# ---------------------------------------------------------------------------

@shared_task(name="billing.tasks.recalculate_student_balances_task")
def recalculate_student_balances_task(student_id: int | None = None) -> dict:
    """
    Recalculate balances safely using the authoritative model method.
    """
    from .models import StudentBill

    if student_id:
        bills = StudentBill.objects.filter(student_id=student_id).order_by("generated_date")
    else:
        bills = StudentBill.objects.all().order_by("student_id", "generated_date")

    updated_count = 0

    for bill in bills:
        before = (
            _decimal_str(bill.previous_arrears),
            _decimal_str(bill.total_amount_due),
            _decimal_str(bill.total_paid),
            bill.payment_status,
        )

        bill.apply_financial_recalculation(
            actor=None,
            cascade_subsequent=False,
            queue_pdf=True,
        )

        bill.refresh_from_db(fields=[
            "previous_arrears",
            "total_amount_due",
            "total_paid",
            "payment_status",
        ])

        after = (
            _decimal_str(bill.previous_arrears),
            _decimal_str(bill.total_amount_due),
            _decimal_str(bill.total_paid),
            bill.payment_status,
        )

        if before != after:
            updated_count += 1

    logger.info(
        f"[recalculate_student_balances_task] "
        f"student_id={student_id or 'ALL'} – updated {updated_count} bill(s)."
    )
    return {"updated_count": updated_count}