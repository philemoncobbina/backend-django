from decimal import Decimal, ROUND_DOWN
import logging
import uuid

from django.core.files.base import ContentFile
from django.core.validators import MinValueValidator
from django.db import models, transaction
from django.db.models import Sum, F
from django.utils import timezone

from authapp.models import CustomUser

logger = logging.getLogger(__name__)

TWOPLACES = Decimal("0.01")


def _decimal_str(value):
    return str(Decimal(str(value)).quantize(TWOPLACES, rounding=ROUND_DOWN))


def _actor_or_fallback(instance, fallback=None):
    actor = getattr(instance, "_current_user", None)
    if actor:
        return actor
    if fallback:
        return fallback
    created_by = getattr(instance, "created_by", None)
    if created_by:
        return created_by
    return None


def _actor_kwargs(user):
    if not user:
        return {
            "user_first_name": "",
            "user_last_name": "",
            "user_email": "",
        }
    return {
        "user_first_name": user.first_name,
        "user_last_name": user.last_name,
        "user_email": user.email,
    }


class BillingTemplate(models.Model):
    TERM_CHOICES = (
        ("first", "First Term"),
        ("second", "Second Term"),
        ("third", "Third Term"),
    )

    academic_year = models.CharField(max_length=9, help_text="Academic year e.g., '2024-2025'")
    class_name = models.CharField(
        max_length=10,
        choices=CustomUser.CLASS_CHOICES,
        help_text="Class this billing applies to",
    )
    term = models.CharField(max_length=10, choices=TERM_CHOICES, help_text="Academic term")
    created_date = models.DateTimeField(default=timezone.now)
    created_by = models.ForeignKey(
        CustomUser,
        on_delete=models.CASCADE,
        related_name="created_billing_templates",
    )
    due_date = models.DateField(help_text="Payment due date")

    class Meta:
        unique_together = ["academic_year", "class_name", "term"]

    def __str__(self):
        return f"{self.class_name} - {self.get_term_display()} ({self.academic_year})"

    def save(self, *args, **kwargs):
        is_update = self.pk is not None
        old_due_date = None

        if is_update:
            try:
                old_obj = BillingTemplate.objects.get(pk=self.pk)
                old_due_date = old_obj.due_date
            except BillingTemplate.DoesNotExist:
                pass

        super().save(*args, **kwargs)

        actor = _actor_or_fallback(self, self.created_by)

        if is_update and old_due_date and old_due_date != self.due_date:
            self.propagate_due_date_change(old_due_date=old_due_date, actor=actor)

    def propagate_due_date_change(self, old_due_date, actor=None):
        actor = actor or self.created_by
        bills = (
            StudentBill.objects
            .filter(billing_template=self)
            .order_by("student_id", "generated_date")
        )

        for bill in bills:
            if bill.due_date == self.due_date:
                continue

            old_value = str(bill.due_date) if bill.due_date else ""
            new_value = str(self.due_date)

            StudentBill.objects.filter(pk=bill.pk).update(due_date=self.due_date)
            bill.due_date = self.due_date

            StudentBillLog.objects.create(
                bill=bill,
                field_name="due_date",
                old_value=old_value,
                new_value=new_value,
                **_actor_kwargs(actor),
            )

            bill.apply_financial_recalculation(
                actor=actor,
                cascade_subsequent=False,
                queue_pdf=True,
            )

    def recalculate_linked_bills(self, actor=None):
        actor = actor or self.created_by
        bills = (
            StudentBill.objects
            .filter(billing_template=self)
            .select_related("student", "billing_template")
            .order_by("student_id", "generated_date")
        )

        for bill in bills:
            bill.apply_financial_recalculation(
                actor=actor,
                cascade_subsequent=False,
                queue_pdf=True,
            )


class BillingItem(models.Model):
    billing_template = models.ForeignKey(
        BillingTemplate,
        on_delete=models.CASCADE,
        related_name="billing_items",
    )
    item_name = models.CharField(max_length=100)
    category = models.CharField(max_length=50, help_text="Category name (user-defined)")
    amount = models.DecimalField(
        max_digits=10,
        decimal_places=2,
        validators=[MinValueValidator(Decimal("0.00"))],
    )
    created_date = models.DateTimeField(default=timezone.now)
    created_by = models.ForeignKey(
        CustomUser,
        on_delete=models.CASCADE,
        related_name="created_billing_items",
    )

    class Meta:
        ordering = ["category", "item_name"]

    def __str__(self):
        return f"{self.item_name} - GHS {self.amount}"

    def save(self, *args, **kwargs):
        is_update = self.pk is not None
        old_values = {}

        if is_update:
            try:
                old_item = BillingItem.objects.get(pk=self.pk)
                old_values = {
                    "item_name": old_item.item_name,
                    "category": old_item.category,
                    "amount": _decimal_str(old_item.amount),
                }
            except BillingItem.DoesNotExist:
                is_update = False

        super().save(*args, **kwargs)

        actor = _actor_or_fallback(self, self.created_by)

        if is_update and old_values:
            self._create_change_logs(old_values)

        self.billing_template.recalculate_linked_bills(actor=actor)

    def delete(self, *args, **kwargs):
        template = self.billing_template
        actor = _actor_or_fallback(self, self.created_by)
        super().delete(*args, **kwargs)
        template.recalculate_linked_bills(actor=actor)

    def _create_change_logs(self, old_values):
        current_values = {
            "item_name": self.item_name,
            "category": self.category,
            "amount": _decimal_str(self.amount),
        }

        user = _actor_or_fallback(self, self.created_by)
        logs_to_create = []

        for field_name, new_value in current_values.items():
            old_value = old_values.get(field_name, "")
            if old_value != new_value:
                logs_to_create.append(
                    BillingItemLog(
                        billing_item=self,
                        field_name=field_name,
                        old_value=old_value,
                        new_value=new_value,
                        user_first_name=user.first_name if user else "",
                        user_last_name=user.last_name if user else "",
                        user_email=user.email if user else "",
                    )
                )

        if logs_to_create:
            BillingItemLog.objects.bulk_create(logs_to_create)

    def create_change_logs(self, old_values):
        return self._create_change_logs(old_values)


class BillingItemLog(models.Model):
    billing_item = models.ForeignKey(BillingItem, on_delete=models.CASCADE, related_name="logs")
    field_name = models.CharField(max_length=100)
    old_value = models.TextField(null=True, blank=True)
    new_value = models.TextField(null=True, blank=True)
    user_first_name = models.CharField(max_length=50)
    user_last_name = models.CharField(max_length=50)
    user_email = models.EmailField()
    timestamp = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ["-timestamp"]

    def __str__(self):
        return f"{self.billing_item.item_name} | {self.field_name} changed"


class StudentBill(models.Model):
    STATUS_CHOICES = (
        ("DRAFT", "Draft"),
        ("SCHEDULED", "Scheduled"),
        ("PUBLISHED", "Published"),
    )

    PAYMENT_STATUS_CHOICES = (
        ("pending", "Pending"),
        ("partial", "Partially Paid"),
        ("paid", "Fully Paid"),
        ("overpaid", "Overpaid (Credit)"),
        ("overdue", "Overdue"),
    )

    student = models.ForeignKey(
        CustomUser,
        on_delete=models.CASCADE,
        limit_choices_to={"role": "student"},
        related_name="bills",
    )
    billing_template = models.ForeignKey(BillingTemplate, on_delete=models.CASCADE)
    bill_number = models.CharField(max_length=30, unique=True, blank=True)

    first_name = models.CharField(max_length=30, blank=True)
    last_name = models.CharField(max_length=30, blank=True)

    previous_arrears = models.DecimalField(
        max_digits=10,
        decimal_places=2,
        default=Decimal("0.00"),
        help_text="Net balance carried forward from previous terms. Negative value means the student has a credit.",
    )
    discount_amount = models.DecimalField(
        max_digits=10,
        decimal_places=2,
        default=Decimal("0.00"),
        help_text="Discount applied to this bill",
    )
    discount_reason = models.TextField(blank=True, null=True)
    discount_approved_by = models.CharField(max_length=100, blank=True, null=True)

    status = models.CharField(max_length=10, choices=STATUS_CHOICES, default="DRAFT")
    payment_status = models.CharField(max_length=10, choices=PAYMENT_STATUS_CHOICES, default="pending")

    generated_date = models.DateTimeField(default=timezone.now)
    scheduled_date = models.DateTimeField(null=True, blank=True)
    due_date = models.DateField()
    created_date = models.DateTimeField(default=timezone.now)
    created_by = models.ForeignKey(
        CustomUser,
        on_delete=models.CASCADE,
        related_name="created_student_bills",
    )

    total_amount_due = models.DecimalField(max_digits=10, decimal_places=2, default=Decimal("0.00"))
    total_paid = models.DecimalField(max_digits=10, decimal_places=2, default=Decimal("0.00"))
    notes = models.TextField(blank=True, null=True)

    pdf_file = models.FileField(
        upload_to="student_bills/%Y/%m/",
        blank=True,
        null=True,
        help_text="Auto-generated PDF bill",
    )

    class Meta:
        unique_together = ["student", "billing_template"]
        ordering = ["-generated_date"]
        indexes = [
            models.Index(fields=["student", "generated_date"]),
            models.Index(fields=["billing_template"]),
            models.Index(fields=["student", "status"]),
        ]

    def __str__(self):
        return f"Bill #{self.bill_number} - {self.first_name} {self.last_name}"

    def save(self, *args, **kwargs):
        is_new = self.pk is None
        old_values = {}
        old_bill = None
        skip_pdf_generation = kwargs.pop("skip_pdf_generation", False)

        if not is_new:
            try:
                old_bill = StudentBill.objects.get(pk=self.pk)
                old_values = {
                    "discount_amount": _decimal_str(old_bill.discount_amount),
                    "discount_reason": old_bill.discount_reason or "",
                    "discount_approved_by": old_bill.discount_approved_by or "",
                    "status": old_bill.status,
                    "notes": old_bill.notes or "",
                }
            except StudentBill.DoesNotExist:
                old_bill = None

        if not self.bill_number:
            self.bill_number = self._generate_unique_bill_number()
        if not self.due_date:
            self.due_date = self.billing_template.due_date
        if not self.first_name:
            self.first_name = self.student.first_name
        if not self.last_name:
            self.last_name = self.student.last_name

        if is_new:
            self._refresh_non_receipt_financials_in_memory()
            self.update_payment_status()
        else:
            self._restore_persisted_financials(old_bill=old_bill)
            self.previous_arrears = self._calculate_previous_arrears_from_db()
            self.total_amount_due = self._calculate_total_amount_from_db()
            self.update_payment_status()

        super().save(*args, **kwargs)

        if not is_new and old_values:
            self._create_user_edit_logs(old_values)

        if is_new and not skip_pdf_generation:
            self._queue_pdf_generation()

        if not is_new and old_bill:
            discount_changed = old_bill.discount_amount != self.discount_amount
            non_financial_visible_changed = any([
                (old_bill.discount_reason or "") != (self.discount_reason or ""),
                (old_bill.discount_approved_by or "") != (self.discount_approved_by or ""),
                old_bill.status != self.status,
                (old_bill.notes or "") != (self.notes or ""),
            ])

            if discount_changed:
                self.apply_financial_recalculation(
                    actor=_actor_or_fallback(self, self.created_by),
                    cascade_subsequent=True,
                    queue_pdf=not skip_pdf_generation,
                )
            elif non_financial_visible_changed and not skip_pdf_generation:
                self._queue_pdf_generation()

    def _restore_persisted_financials(self, old_bill=None):
        if old_bill is None and self.pk:
            try:
                old_bill = StudentBill.objects.get(pk=self.pk)
            except StudentBill.DoesNotExist:
                old_bill = None

        if old_bill:
            self.previous_arrears = old_bill.previous_arrears
            self.total_amount_due = old_bill.total_amount_due
            self.total_paid = old_bill.total_paid
            self.payment_status = old_bill.payment_status

    def _queue_pdf_generation(self):
        try:
            from .tasks import generate_bill_pdf_task
            generate_bill_pdf_task.delay(self.pk)
        except Exception as e:
            logger.error(f"Failed to queue PDF generation for bill {self.bill_number}: {e}")

    def generate_pdf(self):
        try:
            from .pdf_generator import generate_bill_pdf

            pdf_content = generate_bill_pdf(self)
            filename = f"bill_{self.bill_number}.pdf"
            self.pdf_file.save(filename, ContentFile(pdf_content), save=False)
            StudentBill.objects.filter(pk=self.pk).update(pdf_file=self.pdf_file.name)
            return True
        except Exception as e:
            logger.error(f"Failed to generate PDF for bill {self.bill_number}: {e}")
            return False

    def _generate_unique_bill_number(self):
        year = self.billing_template.academic_year.replace("-", "")
        term_map = {"first": "1", "second": "2", "third": "3"}
        term_code = term_map.get(self.billing_template.term, "1")
        class_code = self.billing_template.class_name[:3].upper()

        while True:
            suffix = uuid.uuid4().hex[:8].upper()
            candidate = f"BILL{year}{term_code}{class_code}{suffix}"
            if len(candidate) > 30:
                candidate = candidate[:30]
            if not StudentBill.objects.filter(bill_number=candidate).exists():
                return candidate

    def _sum_receipts_from_db(self, exclude_receipt_pk=None):
        qs = PaymentReceipt.objects.filter(student_bill=self)
        if exclude_receipt_pk is not None:
            qs = qs.exclude(pk=exclude_receipt_pk)

        raw_total = qs.aggregate(total=Sum("amount_paid"))["total"]
        return (
            Decimal(str(raw_total)).quantize(TWOPLACES, rounding=ROUND_DOWN)
            if raw_total is not None
            else Decimal("0.00")
        )

    def _calculate_previous_arrears_from_db(self):
        generated_date = self.generated_date or timezone.now()

        result = (
            StudentBill.objects
            .filter(student=self.student, generated_date__lt=generated_date)
            .exclude(pk=self.pk)
            .aggregate(arrears=Sum(F("total_amount_due") - F("total_paid")))
        )
        value = result["arrears"] or Decimal("0.00")
        return Decimal(str(value)).quantize(TWOPLACES, rounding=ROUND_DOWN)

    def _calculate_total_amount_from_db(self):
        base_amount = (
            self.billing_template.billing_items.aggregate(total=Sum("amount"))["total"]
            or Decimal("0.00")
        )

        custom_charges_total = (
            self.custom_charges.aggregate(total=Sum("amount"))["total"]
            or Decimal("0.00")
        )

        total = (base_amount + custom_charges_total - self.discount_amount).quantize(
            TWOPLACES, rounding=ROUND_DOWN
        )
        return max(total, Decimal("0.00"))

    def _refresh_non_receipt_financials_in_memory(self):
        if self.pk:
            self.previous_arrears = self._calculate_previous_arrears_from_db()
            self.total_amount_due = self._calculate_total_amount_from_db()
        else:
            base_amount = (
                self.billing_template.billing_items.aggregate(total=Sum("amount"))["total"]
                or Decimal("0.00")
            )
            total = (base_amount - self.discount_amount).quantize(TWOPLACES, rounding=ROUND_DOWN)
            self.total_amount_due = max(total, Decimal("0.00"))
            self.previous_arrears = Decimal("0.00")

    def update_payment_status(self):
        current_balance = self.balance_due

        if current_balance < Decimal("0.00"):
            self.payment_status = "overpaid"
        elif current_balance == Decimal("0.00"):
            self.payment_status = "paid"
        else:
            if self.due_date and self.due_date < timezone.now().date():
                self.payment_status = "overdue"
            elif self.total_paid > Decimal("0.00"):
                self.payment_status = "partial"
            else:
                self.payment_status = "pending"

    def apply_financial_recalculation(
        self,
        actor=None,
        cascade_subsequent=True,
        queue_pdf=True,
        exclude_receipt_pk=None,
    ):
        actor = actor or _actor_or_fallback(self, self.created_by)

        with transaction.atomic():
            locked_bill = StudentBill.objects.select_for_update().get(pk=self.pk)

            old_values = {
                "previous_arrears": _decimal_str(locked_bill.previous_arrears),
                "total_amount_due": _decimal_str(locked_bill.total_amount_due),
                "total_paid": _decimal_str(locked_bill.total_paid),
                "payment_status": locked_bill.payment_status,
            }

            locked_bill.total_paid = locked_bill._sum_receipts_from_db(
                exclude_receipt_pk=exclude_receipt_pk
            )
            locked_bill.previous_arrears = locked_bill._calculate_previous_arrears_from_db()
            locked_bill.total_amount_due = locked_bill._calculate_total_amount_from_db()
            locked_bill.update_payment_status()

            new_values = {
                "previous_arrears": _decimal_str(locked_bill.previous_arrears),
                "total_amount_due": _decimal_str(locked_bill.total_amount_due),
                "total_paid": _decimal_str(locked_bill.total_paid),
                "payment_status": locked_bill.payment_status,
            }

            StudentBill.objects.filter(pk=locked_bill.pk).update(
                previous_arrears=locked_bill.previous_arrears,
                total_amount_due=locked_bill.total_amount_due,
                total_paid=locked_bill.total_paid,
                payment_status=locked_bill.payment_status,
            )

            self.previous_arrears = locked_bill.previous_arrears
            self.total_amount_due = locked_bill.total_amount_due
            self.total_paid = locked_bill.total_paid
            self.payment_status = locked_bill.payment_status

            logs_to_create = []
            for field_name, old_value in old_values.items():
                new_value = new_values[field_name]
                if old_value != new_value:
                    logs_to_create.append(
                        StudentBillLog(
                            bill=locked_bill,
                            field_name=field_name,
                            old_value=old_value,
                            new_value=new_value,
                            **_actor_kwargs(actor),
                        )
                    )

            if logs_to_create:
                StudentBillLog.objects.bulk_create(logs_to_create)

            if queue_pdf and logs_to_create:
                locked_bill._queue_pdf_generation()

        if cascade_subsequent:
            self.recalculate_all_subsequent_bills(actor=actor)

    def recalculate_all_subsequent_bills(self, actor=None):
        subsequent_bills = (
            StudentBill.objects
            .filter(student=self.student, generated_date__gt=self.generated_date)
            .exclude(pk=self.pk)
            .order_by("generated_date")
        )

        for bill in subsequent_bills:
            bill.apply_financial_recalculation(
                actor=actor,
                cascade_subsequent=False,
                queue_pdf=True,
            )

    def _create_user_edit_logs(self, old_values):
        current_values = {
            "discount_amount": _decimal_str(self.discount_amount),
            "discount_reason": self.discount_reason or "",
            "discount_approved_by": self.discount_approved_by or "",
            "status": self.status,
            "notes": self.notes or "",
        }

        user = _actor_or_fallback(self, self.created_by)
        logs_to_create = []

        for field_name, new_value in current_values.items():
            old_value = old_values.get(field_name, "")
            if old_value != new_value:
                logs_to_create.append(
                    StudentBillLog(
                        bill=self,
                        field_name=field_name,
                        old_value=old_value,
                        new_value=new_value,
                        **_actor_kwargs(user),
                    )
                )

        if logs_to_create:
            StudentBillLog.objects.bulk_create(logs_to_create)

    def create_change_logs(self, old_values, update_fields=None):
        return self._create_user_edit_logs(old_values)

    @property
    def balance_due(self):
        return (self.previous_arrears + (self.total_amount_due - self.total_paid)).quantize(
            TWOPLACES, rounding=ROUND_DOWN
        )

    @property
    def current_bill_balance(self):
        return (self.total_amount_due - self.total_paid).quantize(
            TWOPLACES, rounding=ROUND_DOWN
        )

    @property
    def credit_balance(self):
        bd = self.balance_due
        return abs(bd) if bd < Decimal("0.00") else Decimal("0.00")

    @property
    def is_overdue(self):
        return (
            self.payment_status not in ("paid", "overpaid")
            and self.due_date < timezone.now().date()
        )

    @property
    def pdf_url(self):
        if self.pdf_file:
            return self.pdf_file.url
        return None


class CustomCharge(models.Model):
    student_bill = models.ForeignKey(
        StudentBill,
        on_delete=models.CASCADE,
        related_name="custom_charges",
    )
    charge_name = models.CharField(max_length=100)
    description = models.TextField(blank=True, null=True)
    amount = models.DecimalField(
        max_digits=10,
        decimal_places=2,
        validators=[MinValueValidator(Decimal("0.00"))],
    )
    created_date = models.DateTimeField(default=timezone.now)
    created_by = models.ForeignKey(
        CustomUser,
        on_delete=models.CASCADE,
        related_name="created_custom_charges",
    )

    class Meta:
        ordering = ["charge_name"]

    def __str__(self):
        return f"{self.charge_name} - GHS {self.amount}"

    def save(self, *args, **kwargs):
        is_new = self.pk is None
        old_summary = None

        if not is_new:
            old_obj = CustomCharge.objects.get(pk=self.pk)
            old_summary = f"{old_obj.charge_name}"
            if old_obj.description:
                old_summary += f" ({old_obj.description})"
            old_summary += f": GHS {_decimal_str(old_obj.amount)}"

        super().save(*args, **kwargs)

        actor = _actor_or_fallback(self, self.created_by)

        new_summary = f"{self.charge_name}"
        if self.description:
            new_summary += f" ({self.description})"
        new_summary += f": GHS {_decimal_str(self.amount)}"

        if is_new:
            StudentBillLog.objects.create(
                bill=self.student_bill,
                field_name="custom_charge_added",
                old_value="",
                new_value=new_summary,
                **_actor_kwargs(actor),
            )
        elif old_summary != new_summary:
            StudentBillLog.objects.create(
                bill=self.student_bill,
                field_name="custom_charge_updated",
                old_value=old_summary,
                new_value=new_summary,
                **_actor_kwargs(actor),
            )

        self.student_bill.apply_financial_recalculation(
            actor=actor,
            cascade_subsequent=True,
            queue_pdf=True,
        )

    def delete(self, *args, **kwargs):
        bill = self.student_bill
        actor = _actor_or_fallback(self, self.created_by)

        old_summary = f"{self.charge_name}"
        if self.description:
            old_summary += f" ({self.description})"
        old_summary += f": GHS {_decimal_str(self.amount)}"

        super().delete(*args, **kwargs)

        StudentBillLog.objects.create(
            bill=bill,
            field_name="custom_charge_removed",
            old_value=old_summary,
            new_value="",
            **_actor_kwargs(actor),
        )

        bill.apply_financial_recalculation(
            actor=actor,
            cascade_subsequent=True,
            queue_pdf=True,
        )


class PaymentReceipt(models.Model):
    student_bill = models.ForeignKey(
        StudentBill,
        on_delete=models.CASCADE,
        related_name="payment_receipts",
    )
    receipt_number = models.CharField(max_length=50, unique=True, blank=True)
    amount_paid = models.DecimalField(
        max_digits=10,
        decimal_places=2,
        validators=[MinValueValidator(Decimal("0.01"))],
    )
    payment_date = models.DateTimeField(default=timezone.now)
    payment_method = models.CharField(
        max_length=50,
        choices=[
            ("cash", "Cash"),
            ("bank_transfer", "Bank Transfer"),
            ("mobile_money", "Mobile Money"),
            ("cheque", "Cheque"),
            ("other", "Other"),
        ],
        default="cash",
    )
    notes = models.TextField(blank=True, null=True)
    created_by = models.ForeignKey(
        CustomUser,
        on_delete=models.CASCADE,
        related_name="created_payments",
    )

    class Meta:
        ordering = ["-payment_date"]
        indexes = [models.Index(fields=["student_bill"])]

    def __str__(self):
        return f"Receipt #{self.receipt_number} - GHS {self.amount_paid}"

    def _generate_unique_receipt_number(self):
        date_str = timezone.now().strftime("%Y%m%d")
        bill_ref = self.student_bill.bill_number[-7:] if self.student_bill.bill_number else "UNKNWN"

        while True:
            suffix = uuid.uuid4().hex[:6].upper()
            candidate = f"RCT-{date_str}-{bill_ref}-{suffix}"
            if not PaymentReceipt.objects.filter(receipt_number=candidate).exists():
                return candidate

    def generate_unique_receipt_number(self):
        return self._generate_unique_receipt_number()

    def save(self, *args, **kwargs):
        is_new = self.pk is None
        old_values = {}

        if not self.receipt_number:
            self.receipt_number = self._generate_unique_receipt_number()

        if not is_new:
            try:
                old_receipt = PaymentReceipt.objects.get(pk=self.pk)
                old_values = {
                    "receipt_number": old_receipt.receipt_number,
                    "amount_paid": _decimal_str(old_receipt.amount_paid),
                    "payment_date": old_receipt.payment_date.isoformat(),
                    "payment_method": old_receipt.payment_method,
                    "notes": old_receipt.notes or "",
                }
            except PaymentReceipt.DoesNotExist:
                pass

        super().save(*args, **kwargs)

        actor = _actor_or_fallback(self, self.created_by)

        if is_new:
            StudentBillLog.objects.create(
                bill=self.student_bill,
                field_name="payment_receipt_added",
                old_value="",
                new_value=f"Receipt #{self.receipt_number}: GHS {_decimal_str(self.amount_paid)}",
                **_actor_kwargs(actor),
            )
        elif old_values:
            self._create_change_logs(old_values)

        self.student_bill.apply_financial_recalculation(
            actor=actor,
            cascade_subsequent=True,
            queue_pdf=True,
        )

    def delete(self, *args, **kwargs):
        actor = kwargs.pop("actor", None) or _actor_or_fallback(self, self.created_by)
        skip_bill_recalc = kwargs.pop("skip_bill_recalc", False)

        bill = self.student_bill
        old_summary = f"Receipt #{self.receipt_number}: GHS {_decimal_str(self.amount_paid)}"

        super().delete(*args, **kwargs)

        StudentBillLog.objects.create(
            bill=bill,
            field_name="payment_receipt_removed",
            old_value=old_summary,
            new_value="",
            **_actor_kwargs(actor),
        )

        if not skip_bill_recalc:
            bill.apply_financial_recalculation(
                actor=actor,
                cascade_subsequent=True,
                queue_pdf=True,
            )

    def _create_change_logs(self, old_values):
        current_values = {
            "receipt_number": self.receipt_number,
            "amount_paid": _decimal_str(self.amount_paid),
            "payment_date": self.payment_date.isoformat(),
            "payment_method": self.payment_method,
            "notes": self.notes or "",
        }

        user = _actor_or_fallback(self, self.created_by)
        logs_to_create = []

        for field_name, new_value in current_values.items():
            old_value = old_values.get(field_name, "")
            if old_value != new_value:
                logs_to_create.append(
                    StudentBillLog(
                        bill=self.student_bill,
                        field_name=f"payment_receipt_{field_name}",
                        old_value=old_value,
                        new_value=new_value,
                        **_actor_kwargs(user),
                    )
                )

        if logs_to_create:
            StudentBillLog.objects.bulk_create(logs_to_create)

    def create_change_logs(self, old_values):
        return self._create_change_logs(old_values)


class StudentBillLog(models.Model):
    bill = models.ForeignKey(StudentBill, on_delete=models.CASCADE, related_name="logs")
    field_name = models.CharField(max_length=100)
    old_value = models.TextField(null=True, blank=True)
    new_value = models.TextField(null=True, blank=True)
    user_first_name = models.CharField(max_length=50)
    user_last_name = models.CharField(max_length=50)
    user_email = models.EmailField()
    timestamp = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ["-timestamp"]

    def __str__(self):
        return f"{self.bill.bill_number} | {self.field_name} changed"


class PaymentReceiptRequest(models.Model):
    STATUS_PENDING = "pending"
    STATUS_UNDER_REVIEW = "under_review"
    STATUS_REJECTED = "rejected"
    STATUS_ACCEPTED = "accepted"

    STATUS_CHOICES = (
        (STATUS_PENDING, "Pending"),
        (STATUS_UNDER_REVIEW, "Under Review"),
        (STATUS_REJECTED, "Rejected"),
        (STATUS_ACCEPTED, "Accepted"),
    )

    PAYMENT_METHOD_CHOICES = [
        ("cash", "Cash"),
        ("bank_transfer", "Bank Transfer"),
        ("mobile_money", "Mobile Money"),
        ("cheque", "Cheque"),
        ("other", "Other"),
    ]

    student_bill = models.ForeignKey(
        StudentBill,
        on_delete=models.CASCADE,
        related_name="receipt_requests",
    )
    submitted_by = models.ForeignKey(
        CustomUser,
        on_delete=models.CASCADE,
        related_name="submitted_receipt_requests",
        limit_choices_to={"role": "student"},
    )

    amount = models.DecimalField(
        max_digits=10,
        decimal_places=2,
        validators=[MinValueValidator(Decimal("0.01"))],
        help_text="Amount the student claims to have paid",
    )
    payment_method = models.CharField(
        max_length=50,
        choices=PAYMENT_METHOD_CHOICES,
        default="cash",
    )
    payment_reference = models.CharField(
        max_length=200,
        help_text="Transaction ID, cheque number, or any payment reference",
    )
    phone_number = models.CharField(
        max_length=20,
        blank=True,
        null=True,
        help_text="Phone number used for the payment",
    )
    proof_of_payment = models.FileField(
        upload_to="payment_proofs/%Y/%m/",
        help_text="Image or PDF proof of payment",
    )

    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default=STATUS_PENDING)
    reviewed_by = models.ForeignKey(
        CustomUser,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="reviewed_receipt_requests",
    )
    review_comment = models.TextField(blank=True, null=True)
    reviewed_at = models.DateTimeField(null=True, blank=True)

    generated_receipt = models.OneToOneField(
        PaymentReceipt,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="source_request",
    )

    submitted_at = models.DateTimeField(default=timezone.now)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-submitted_at"]
        indexes = [models.Index(fields=["submitted_by", "status"])]

    def __str__(self):
        return (
            f"Request by {self.submitted_by.first_name} {self.submitted_by.last_name} "
            f"| Bill #{self.student_bill.bill_number} | {self.get_status_display()}"
        )

    def save(self, *args, **kwargs):
        is_new = self.pk is None
        old_values = {}

        if not is_new:
            try:
                old_obj = PaymentReceiptRequest.objects.get(pk=self.pk)
                old_values = {
                    "amount": _decimal_str(old_obj.amount),
                    "payment_method": old_obj.payment_method,
                    "payment_reference": old_obj.payment_reference,
                    "phone_number": old_obj.phone_number or "",
                    "status": old_obj.status,
                    "review_comment": old_obj.review_comment or "",
                }
            except PaymentReceiptRequest.DoesNotExist:
                pass

        super().save(*args, **kwargs)

        actor = _actor_or_fallback(self, self.submitted_by)

        if is_new:
            PaymentReceiptRequestLog.objects.create(
                receipt_request=self,
                action="submitted",
                old_status="",
                new_status=self.status,
                comment="Payment request submitted by student.",
                actor_first_name=actor.first_name if actor else "",
                actor_last_name=actor.last_name if actor else "",
                actor_email=actor.email if actor else "",
            )
        elif old_values:
            current_values = {
                "amount": _decimal_str(self.amount),
                "payment_method": self.payment_method,
                "payment_reference": self.payment_reference,
                "phone_number": self.phone_number or "",
                "status": self.status,
                "review_comment": self.review_comment or "",
            }

            for field_name, new_value in current_values.items():
                old_value = old_values.get(field_name, "")
                if old_value == new_value:
                    continue

                if field_name == "status":
                    action_map = {
                        self.STATUS_UNDER_REVIEW: "marked_under_review",
                        self.STATUS_REJECTED: "rejected",
                        self.STATUS_ACCEPTED: "accepted",
                        self.STATUS_PENDING: "reset_to_pending",
                    }
                    action = action_map.get(self.status, "status_changed")

                    PaymentReceiptRequestLog.objects.create(
                        receipt_request=self,
                        action=action,
                        old_status=old_value,
                        new_status=new_value,
                        comment=self.review_comment or f"Status changed from {old_value} to {new_value}",
                        actor_first_name=actor.first_name if actor else "",
                        actor_last_name=actor.last_name if actor else "",
                        actor_email=actor.email if actor else "",
                    )
                else:
                    PaymentReceiptRequestLog.objects.create(
                        receipt_request=self,
                        action=f"{field_name}_changed",
                        old_status=self.status,
                        new_status=self.status,
                        comment=f'{field_name} changed from "{old_value}" to "{new_value}"',
                        actor_first_name=actor.first_name if actor else "",
                        actor_last_name=actor.last_name if actor else "",
                        actor_email=actor.email if actor else "",
                    )

    def accept_and_generate_receipt(self, reviewed_by_user):
        with transaction.atomic():
            locked_self = PaymentReceiptRequest.objects.select_for_update().get(pk=self.pk)

            if locked_self.status == self.STATUS_ACCEPTED:
                self.status = locked_self.status
                self.generated_receipt_id = locked_self.generated_receipt_id
                self.generated_receipt = locked_self.generated_receipt
                return locked_self.generated_receipt

            self._current_user = reviewed_by_user
            self.reviewed_by = reviewed_by_user
            self.reviewed_at = timezone.now()

            notes = f"Auto-generated from payment request. Reference: {self.payment_reference}"
            if self.phone_number:
                notes += f" | Phone: {self.phone_number}"

            receipt = PaymentReceipt(
                student_bill=self.student_bill,
                amount_paid=self.amount,
                payment_method=self.payment_method,
                notes=notes,
                created_by=reviewed_by_user,
            )
            receipt._current_user = reviewed_by_user
            receipt.save()

            self.student_bill.refresh_from_db(fields=[
                "previous_arrears",
                "total_amount_due",
                "total_paid",
                "payment_status",
            ])

            self.status = self.STATUS_ACCEPTED
            self.generated_receipt = receipt
            self.save()

            return receipt

    def revoke_and_delete_receipt(self, revoked_by_user, new_status, comment=""):
        if self.status != self.STATUS_ACCEPTED:
            return

        receipt_to_delete = self.generated_receipt
        receipt_number = receipt_to_delete.receipt_number if receipt_to_delete else None

        with transaction.atomic():
            PaymentReceiptRequest.objects.filter(pk=self.pk).update(
                generated_receipt=None,
                status=new_status,
                review_comment=comment,
                reviewed_by=revoked_by_user,
                reviewed_at=timezone.now(),
            )

            self.generated_receipt = None
            self.status = new_status
            self.review_comment = comment
            self.reviewed_by = revoked_by_user
            self.reviewed_at = timezone.now()

            if receipt_to_delete is not None:
                receipt_to_delete._current_user = revoked_by_user
                receipt_to_delete.delete(actor=revoked_by_user, skip_bill_recalc=True)

            self.student_bill.apply_financial_recalculation(
                actor=revoked_by_user,
                cascade_subsequent=True,
                queue_pdf=True,
            )

            self.student_bill.refresh_from_db(fields=[
                "previous_arrears",
                "total_amount_due",
                "total_paid",
                "payment_status",
            ])

            if receipt_number is not None:
                PaymentReceiptRequestLog.objects.create(
                    receipt_request=self,
                    action="revoked",
                    old_status=self.STATUS_ACCEPTED,
                    new_status=new_status,
                    comment=f"Receipt #{receipt_number} deleted. {comment}",
                    actor_first_name=revoked_by_user.first_name,
                    actor_last_name=revoked_by_user.last_name,
                    actor_email=revoked_by_user.email,
                )


class PaymentReceiptRequestLog(models.Model):
    ACTION_CHOICES = (
        ("submitted", "Submitted"),
        ("marked_under_review", "Marked Under Review"),
        ("rejected", "Rejected"),
        ("accepted", "Accepted"),
        ("reset_to_pending", "Reset to Pending"),
        ("status_changed", "Status Changed"),
        ("revoked", "Revoked"),
        ("amount_changed", "Amount Changed"),
        ("payment_method_changed", "Payment Method Changed"),
        ("payment_reference_changed", "Payment Reference Changed"),
        ("phone_number_changed", "Phone Number Changed"),
        ("review_comment_changed", "Review Comment Changed"),
    )

    receipt_request = models.ForeignKey(
        PaymentReceiptRequest,
        on_delete=models.CASCADE,
        related_name="logs",
    )
    action = models.CharField(max_length=30, choices=ACTION_CHOICES)
    old_status = models.CharField(max_length=20, blank=True)
    new_status = models.CharField(max_length=20, blank=True)
    comment = models.TextField(blank=True)
    actor_first_name = models.CharField(max_length=50)
    actor_last_name = models.CharField(max_length=50)
    actor_email = models.EmailField()
    timestamp = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ["-timestamp"]

    def __str__(self):
        return (
            f"Request #{self.receipt_request_id} | {self.get_action_display()} "
            f"| {self.actor_first_name} {self.actor_last_name}"
        )