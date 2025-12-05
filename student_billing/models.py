from django.db import models
from django.utils import timezone
from django.core.validators import MinValueValidator
from decimal import Decimal
from authapp.models import CustomUser
from django.db.models.signals import post_save, post_delete
from django.dispatch import receiver
from django.core.files.base import ContentFile
import logging

logger = logging.getLogger(__name__)


class BillingTemplate(models.Model):
    """Template for billing items per class and term"""
    TERM_CHOICES = (
        ('first', 'First Term'),
        ('second', 'Second Term'),
        ('third', 'Third Term'),
    )

    academic_year = models.CharField(
        max_length=9,
        help_text="Academic year e.g., '2023-2024'"
    )
    class_name = models.CharField(
        max_length=10,
        choices=CustomUser.CLASS_CHOICES,
        help_text="Class this billing applies to"
    )
    term = models.CharField(
        max_length=10,
        choices=TERM_CHOICES,
        help_text="Academic term"
    )
    created_date = models.DateTimeField(default=timezone.now)
    created_by = models.ForeignKey(
        CustomUser,
        on_delete=models.CASCADE,
        related_name='created_billing_templates'
    )
    due_date = models.DateField(help_text="Payment due date")

    class Meta:
        unique_together = ['academic_year', 'class_name', 'term']

    def __str__(self):
        return f"{self.class_name} - {self.get_term_display()} ({self.academic_year})"


class BillingItem(models.Model):
    """Individual billing items (fees) within a template"""
    billing_template = models.ForeignKey(
        BillingTemplate,
        on_delete=models.CASCADE,
        related_name='billing_items'
    )
    item_name = models.CharField(max_length=100)
    category = models.CharField(max_length=50, help_text="Category name (user-defined)")
    amount = models.DecimalField(
        max_digits=10,
        decimal_places=2,
        validators=[MinValueValidator(Decimal('0.00'))]
    )
    created_date = models.DateTimeField(default=timezone.now)
    created_by = models.ForeignKey(
        CustomUser,
        on_delete=models.CASCADE,
        related_name='created_billing_items'
    )

    class Meta:
        ordering = ['category', 'item_name']

    def __str__(self):
        return f"{self.item_name} - GHS {self.amount}"

    def save(self, *args, **kwargs):
        is_update = self.pk is not None
        old_values = {}
        
        if is_update:
            old_item = BillingItem.objects.get(pk=self.pk)
            old_values = {
                'item_name': old_item.item_name,
                'category': old_item.category,
                'amount': str(old_item.amount)
            }
        
        super().save(*args, **kwargs)
        
        if is_update:
            self.create_change_logs(old_values)

    def create_change_logs(self, old_values):
        """Create logs for billing item changes"""
        current_values = {
            'item_name': self.item_name,
            'category': self.category,
            'amount': str(self.amount)
        }

        user = getattr(self, '_current_user', self.created_by)
        
        for field_name, new_value in current_values.items():
            old_value = old_values.get(field_name, '')
            if old_value != new_value:
                BillingItemLog.objects.create(
                    billing_item=self,
                    field_name=field_name,
                    old_value=old_value,
                    new_value=new_value,
                    user_first_name=user.first_name,
                    user_last_name=user.last_name,
                    user_email=user.email
                )


class BillingItemLog(models.Model):
    """Log every change made to a BillingItem"""
    billing_item = models.ForeignKey(BillingItem, on_delete=models.CASCADE, related_name="logs")
    field_name = models.CharField(max_length=100)
    old_value = models.TextField(null=True, blank=True)
    new_value = models.TextField(null=True, blank=True)
    user_first_name = models.CharField(max_length=50)
    user_last_name = models.CharField(max_length=50)
    user_email = models.EmailField()
    timestamp = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ['-timestamp']

    def __str__(self):
        return f"{self.billing_item.item_name} | {self.field_name} changed"


class StudentBill(models.Model):
    """Individual student bill generated from template"""
    STATUS_CHOICES = (
        ('DRAFT', 'Draft'),
        ('SCHEDULED', 'Scheduled'),
        ('PUBLISHED', 'Published'),
    )

    PAYMENT_STATUS_CHOICES = (
        ('pending', 'Pending'),
        ('partial', 'Partially Paid'),
        ('paid', 'Fully Paid'),
        ('overdue', 'Overdue'),
    )

    student = models.ForeignKey(
        CustomUser,
        on_delete=models.CASCADE,
        limit_choices_to={'role': 'student'},
        related_name='bills'
    )
    billing_template = models.ForeignKey(BillingTemplate, on_delete=models.CASCADE)
    bill_number = models.CharField(max_length=30, unique=True, blank=True)

    first_name = models.CharField(max_length=30, blank=True)
    last_name = models.CharField(max_length=30, blank=True)

    previous_arrears = models.DecimalField(
        max_digits=10,
        decimal_places=2,
        default=Decimal('0.00'),
        help_text="Outstanding balance from previous terms (auto-calculated)"
    )
    discount_amount = models.DecimalField(
        max_digits=10,
        decimal_places=2,
        default=Decimal('0.00'),
        help_text="Discount applied to this bill"
    )
    discount_reason = models.TextField(
        blank=True,
        null=True,
        help_text="Reason for applying discount"
    )
    discount_approved_by = models.CharField(
        max_length=100,
        blank=True,
        null=True,
        help_text="Person who approved the discount"
    )

    status = models.CharField(max_length=10, choices=STATUS_CHOICES, default='DRAFT')
    payment_status = models.CharField(max_length=10, choices=PAYMENT_STATUS_CHOICES, default='pending')
    generated_date = models.DateTimeField(default=timezone.now)
    scheduled_date = models.DateTimeField(null=True, blank=True)
    due_date = models.DateField()
    created_date = models.DateTimeField(default=timezone.now)
    created_by = models.ForeignKey(
        CustomUser,
        on_delete=models.CASCADE,
        related_name='created_student_bills'
    )

    total_amount_due = models.DecimalField(max_digits=10, decimal_places=2, default=Decimal('0.00'))
    total_paid = models.DecimalField(max_digits=10, decimal_places=2, default=Decimal('0.00'))

    notes = models.TextField(blank=True, null=True)
    
    # PDF file field
    pdf_file = models.FileField(
        upload_to='student_bills/%Y/%m/',
        blank=True,
        null=True,
        help_text="Auto-generated PDF bill"
    )

    class Meta:
        unique_together = ['student', 'billing_template']
        ordering = ['-generated_date']

    def __str__(self):
        return f"Bill #{self.bill_number} - {self.first_name} {self.last_name}"

    def save(self, *args, **kwargs):
        old_values = {}
        is_new = self.pk is None
        old_total_paid = Decimal('0.00')
        old_discount = Decimal('0.00')
        should_regenerate_pdf = is_new
        skip_pdf_generation = kwargs.pop('skip_pdf_generation', False)
        
        if not is_new:
            old_bill = StudentBill.objects.get(pk=self.pk)
            old_total_paid = old_bill.total_paid
            old_discount = old_bill.discount_amount
            old_values = {
                'discount_amount': str(old_bill.discount_amount),
                'discount_reason': old_bill.discount_reason or '',
                'discount_approved_by': old_bill.discount_approved_by or '',
                'status': old_bill.status,
                'payment_status': old_bill.payment_status,
                'total_paid': str(old_bill.total_paid),
                'notes': old_bill.notes or '',
            }
            
            # Check if ANY field that affects PDF changed
            if (old_bill.discount_amount != self.discount_amount or
                old_bill.discount_reason != self.discount_reason or
                old_bill.discount_approved_by != self.discount_approved_by or
                old_bill.status != self.status or
                old_bill.payment_status != self.payment_status or
                old_bill.notes != self.notes or
                old_bill.total_paid != self.total_paid or
                old_bill.total_amount_due != self.total_amount_due or
                old_bill.previous_arrears != self.previous_arrears):
                should_regenerate_pdf = True

        if not self.bill_number:
            self.bill_number = self.generate_unique_bill_number()

        if not self.due_date:
            self.due_date = self.billing_template.due_date

        if not self.first_name:
            self.first_name = self.student.first_name
        if not self.last_name:
            self.last_name = self.student.last_name

        if is_new:
            self.calculate_previous_arrears()
            base_amount = sum(
                item.amount for item in self.billing_template.billing_items.all()
            )
            self.total_amount_due = base_amount - self.discount_amount
            if self.total_amount_due < 0:
                self.total_amount_due = Decimal('0.00')
            
            super().save(*args, **kwargs)
            
            self.recalculate_amounts()
            StudentBill.objects.filter(pk=self.pk).update(
                total_amount_due=self.total_amount_due,
                previous_arrears=self.previous_arrears
            )
            
            # Generate PDF for new bill
            if not skip_pdf_generation:
                self.generate_pdf()
                logger.info(f"📄 NEW BILL - PDF generated for {self.bill_number}")
        else:
            self.recalculate_amounts()
            super().save(*args, **kwargs)

            if old_values:
                self.create_change_logs(old_values)
            
            # Regenerate PDF if needed
            if should_regenerate_pdf and not skip_pdf_generation:
                self.generate_pdf()
                logger.info(f"🔄 UPDATE - PDF regenerated for {self.bill_number}")
            
            # Recalculate subsequent bills if payment OR discount changed
            if old_total_paid != self.total_paid or old_discount != self.discount_amount:
                self.recalculate_all_subsequent_bills()

    def generate_pdf(self):
        """Generate or regenerate PDF for this bill"""
        try:
            from .pdf_generator import generate_bill_pdf
            
            logger.info(f"🔄 Starting PDF generation for bill {self.bill_number}")
            
            # Generate PDF content
            pdf_content = generate_bill_pdf(self)
            
            # Save PDF file
            filename = f"bill_{self.bill_number}.pdf"
            self.pdf_file.save(filename, ContentFile(pdf_content), save=False)
            
            # Update only the pdf_file field to avoid recursion
            StudentBill.objects.filter(pk=self.pk).update(pdf_file=self.pdf_file)
            
            logger.info(f"✅ PDF generated successfully for bill {self.bill_number} - {filename}")
            return True
            
        except Exception as e:
            logger.error(f"❌ Failed to generate PDF for bill {self.bill_number}: {str(e)}")
            import traceback
            logger.error(traceback.format_exc())
            return False

    def generate_unique_bill_number(self):
        """Generate a unique bill number with better uniqueness handling"""
        year = self.billing_template.academic_year.replace('-', '')
        term_map = {'first': '1', 'second': '2', 'third': '3'}
        term_code = term_map.get(self.billing_template.term, '1')
        class_code = self.billing_template.class_name[:3].upper()
        
        existing_count = StudentBill.objects.filter(
            billing_template=self.billing_template
        ).count()
        
        max_retries = 10
        for attempt in range(max_retries):
            sequence_number = existing_count + attempt + 1
            bill_number = f"BILL{year}{term_code}{class_code}{sequence_number:04d}"
            
            if not StudentBill.objects.filter(bill_number=bill_number).exists():
                return bill_number
        
        timestamp = int(timezone.now().timestamp())
        return f"BILL{year}{term_code}{class_code}{timestamp}"

    def recalculate_amounts(self):
        """Recalculate both previous arrears and total amount due"""
        self.calculate_previous_arrears()
        self.calculate_total_amount()
        self.update_payment_status()

    def calculate_previous_arrears(self):
        """Calculate total unpaid balance from ALL previous bills"""
        previous_bills = StudentBill.objects.filter(
            student=self.student,
            generated_date__lt=self.generated_date or timezone.now()
        ).exclude(pk=self.pk).order_by('generated_date')
        
        total_arrears = Decimal('0.00')
        
        for bill in previous_bills:
            bill_balance = bill.total_amount_due - bill.total_paid
            total_arrears += bill_balance
        
        self.previous_arrears = total_arrears

    def calculate_total_amount(self):
        """Calculate total amount for THIS bill only (excluding arrears)"""
        # Always get fresh billing items from database
        base_amount = sum(
            item.amount for item in self.billing_template.billing_items.all()
        )
        
        custom_charges_total = sum(
            charge.amount for charge in self.custom_charges.all()
        )
        
        self.total_amount_due = (
            base_amount +
            custom_charges_total -
            self.discount_amount
        )
        
        if self.total_amount_due < 0:
            self.total_amount_due = Decimal('0.00')

    def update_payment_status(self):
        """Update payment status based on current payment amounts"""
        if self.total_paid <= 0:
            self.payment_status = 'pending'
        elif self.total_paid >= self.total_amount_due:
            self.payment_status = 'paid'
        else:
            self.payment_status = 'partial'
        
        if self.due_date < timezone.now().date() and self.payment_status != 'paid':
            self.payment_status = 'overdue'

    def recalculate_all_subsequent_bills(self):
        """Recalculate ALL bills that come after this one chronologically"""
        subsequent_bills = StudentBill.objects.filter(
            student=self.student,
            generated_date__gt=self.generated_date
        ).exclude(pk=self.pk).order_by('generated_date')
        
        for bill in subsequent_bills:
            bill.recalculate_amounts()
            bill.save(update_fields=['previous_arrears', 'total_amount_due', 'payment_status'], skip_pdf_generation=False)

    def create_change_logs(self, old_values):
        """Create logs for field changes"""
        current_values = {
            'discount_amount': str(self.discount_amount),
            'discount_reason': self.discount_reason or '',
            'discount_approved_by': self.discount_approved_by or '',
            'status': self.status,
            'payment_status': self.payment_status,
            'total_paid': str(self.total_paid),
            'notes': self.notes or '',
        }

        user = getattr(self, '_current_user', self.created_by)
        
        for field_name, new_value in current_values.items():
            old_value = old_values.get(field_name, '')
            if old_value != new_value:
                StudentBillLog.objects.create(
                    bill=self,
                    field_name=field_name,
                    old_value=old_value,
                    new_value=new_value,
                    user_first_name=user.first_name,
                    user_last_name=user.last_name,
                    user_email=user.email
                )

    @property
    def balance_due(self):
        """Total balance student owes (previous arrears + current bill balance)"""
        current_bill_balance = self.total_amount_due - self.total_paid
        return self.previous_arrears + current_bill_balance

    @property
    def is_overdue(self):
        return self.due_date < timezone.now().date() and self.payment_status != 'paid'
    
    @property
    def pdf_url(self):
        """Get PDF URL if available"""
        if self.pdf_file:
            return self.pdf_file.url
        return None


class CustomCharge(models.Model):
    """Custom charges that can be added to individual student bills"""
    student_bill = models.ForeignKey(
        StudentBill, 
        on_delete=models.CASCADE, 
        related_name='custom_charges'
    )
    charge_name = models.CharField(max_length=100)
    description = models.TextField(blank=True, null=True)
    amount = models.DecimalField(
        max_digits=10,
        decimal_places=2,
        validators=[MinValueValidator(Decimal('0.00'))]
    )
    created_date = models.DateTimeField(default=timezone.now)
    created_by = models.ForeignKey(
        CustomUser,
        on_delete=models.CASCADE,
        related_name='created_custom_charges'
    )

    class Meta:
        ordering = ['charge_name']

    def __str__(self):
        return f"{self.charge_name} - GHS {self.amount}"

    def save(self, *args, **kwargs):
        """Save custom charge and recalculate related bill + regenerate PDF"""
        is_new = self.pk is None
        super().save(*args, **kwargs)
        
        # Recalculate and save the bill (which will regenerate PDF)
        self.student_bill.recalculate_amounts()
        self.student_bill.save(update_fields=['total_amount_due', 'payment_status'], skip_pdf_generation=False)
        
        # Recalculate subsequent bills
        self.student_bill.recalculate_all_subsequent_bills()
        
        logger.info(f"{'📄 CUSTOM CHARGE ADDED' if is_new else '🔄 CUSTOM CHARGE UPDATED'} - '{self.charge_name}' - Bill {self.student_bill.bill_number} PDF regenerated")


class PaymentReceipt(models.Model):
    """Payment receipts for tracking individual payments made to student bills"""
    student_bill = models.ForeignKey(
        StudentBill, 
        on_delete=models.CASCADE, 
        related_name='payment_receipts'
    )
    receipt_number = models.CharField(max_length=50, unique=True)
    amount_paid = models.DecimalField(
        max_digits=10,
        decimal_places=2,
        validators=[MinValueValidator(Decimal('0.01'))]
    )
    payment_date = models.DateTimeField(default=timezone.now)
    payment_method = models.CharField(
        max_length=50, 
        choices=[
            ('cash', 'Cash'),
            ('bank_transfer', 'Bank Transfer'),
            ('mobile_money', 'Mobile Money'),
            ('cheque', 'Cheque'),
            ('other', 'Other')
        ],
        default='cash'
    )
    notes = models.TextField(blank=True, null=True)
    created_by = models.ForeignKey(
        CustomUser,
        on_delete=models.CASCADE,
        related_name='created_payments'
    )

    class Meta:
        ordering = ['-payment_date']

    def __str__(self):
        return f"Receipt #{self.receipt_number} - GHS {self.amount_paid}"

    def save(self, *args, **kwargs):
        is_new = self.pk is None
        old_values = {}
        old_amount = Decimal('0.00')
        
        if not is_new:
            old_receipt = PaymentReceipt.objects.get(pk=self.pk)
            old_amount = old_receipt.amount_paid
            old_values = {
                'receipt_number': old_receipt.receipt_number,
                'amount_paid': str(old_receipt.amount_paid),
                'payment_date': old_receipt.payment_date.isoformat(),
                'payment_method': old_receipt.payment_method,
                'notes': old_receipt.notes or '',
            }
        
        super().save(*args, **kwargs)
        
        if old_values:
            self.create_change_logs(old_values)
        elif is_new:
            user = getattr(self, '_current_user', self.created_by)
            StudentBillLog.objects.create(
                bill=self.student_bill,
                field_name='payment_receipt_added',
                old_value='',
                new_value=f'Receipt #{self.receipt_number}: GHS {self.amount_paid}',
                user_first_name=user.first_name,
                user_last_name=user.last_name,
                user_email=user.email
            )
        
        self.update_bill_total_paid_and_cascade()
        
        logger.info(f"{'📄 PAYMENT ADDED' if is_new else '🔄 PAYMENT UPDATED'} - Receipt {self.receipt_number} - Bill {self.student_bill.bill_number} PDF regenerated")

    def create_change_logs(self, old_values):
        """Create logs for payment receipt changes in StudentBillLog"""
        current_values = {
            'receipt_number': self.receipt_number,
            'amount_paid': str(self.amount_paid),
            'payment_date': self.payment_date.isoformat(),
            'payment_method': self.payment_method,
            'notes': self.notes or '',
        }

        user = getattr(self, '_current_user', self.created_by)
        
        for field_name, new_value in current_values.items():
            old_value = old_values.get(field_name, '')
            if old_value != new_value:
                StudentBillLog.objects.create(
                    bill=self.student_bill,
                    field_name=f'payment_receipt_{field_name}',
                    old_value=old_value,
                    new_value=new_value,
                    user_first_name=user.first_name,
                    user_last_name=user.last_name,
                    user_email=user.email
                )

    def update_bill_total_paid_and_cascade(self):
        """Update the student bill's total_paid amount and trigger recalculation (which regenerates PDF)"""
        total_paid = sum(
            receipt.amount_paid for receipt in self.student_bill.payment_receipts.all()
        )
        
        bill = self.student_bill
        bill.total_paid = total_paid
        bill.update_payment_status()
        bill.save(update_fields=['total_paid', 'payment_status'], skip_pdf_generation=False)
        
        bill.recalculate_all_subsequent_bills()


class StudentBillLog(models.Model):
    """Log every change made to a StudentBill, CustomCharges, and PaymentReceipts"""
    bill = models.ForeignKey(StudentBill, on_delete=models.CASCADE, related_name="logs")
    field_name = models.CharField(max_length=100)
    old_value = models.TextField(null=True, blank=True)
    new_value = models.TextField(null=True, blank=True)
    user_first_name = models.CharField(max_length=50)
    user_last_name = models.CharField(max_length=50)
    user_email = models.EmailField()
    timestamp = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ['-timestamp']

    def __str__(self):
        return f"{self.bill.bill_number} | {self.field_name} changed"


# ========================================
# SIGNAL HANDLERS - PDF REGENERATION
# ========================================

@receiver(post_save, sender=BillingItem)
def billing_item_saved(sender, instance, created, **kwargs):
    """
    CRITICAL: Recalculate and regenerate PDFs for ALL bills when billing items change
    This ensures template changes cascade to all related bills
    """
    from django.db import transaction
    from itertools import groupby
    
    with transaction.atomic():
        bills_to_update = StudentBill.objects.select_for_update().filter(
            billing_template=instance.billing_template
        ).order_by('student_id', 'generated_date')
        
        updated_count = 0
        for student_id, student_bills in groupby(bills_to_update, key=lambda x: x.student_id):
            for bill in student_bills:
                bill.recalculate_amounts()
                bill.save(update_fields=['total_amount_due', 'previous_arrears', 'payment_status'], skip_pdf_generation=False)
                updated_count += 1
        
        logger.info(f"{'📄 BILLING ITEM CREATED' if created else '🔄 BILLING ITEM UPDATED'} - {updated_count} bills recalculated with PDF regeneration")


@receiver(post_delete, sender=BillingItem)
def billing_item_deleted(sender, instance, **kwargs):
    """
    CRITICAL: Recalculate and regenerate PDFs for ALL bills when billing items are deleted
    """
    from django.db import transaction
    from itertools import groupby
    
    with transaction.atomic():
        bills_to_update = StudentBill.objects.select_for_update().filter(
            billing_template=instance.billing_template
        ).order_by('student_id', 'generated_date')
        
        updated_count = 0
        for student_id, student_bills in groupby(bills_to_update, key=lambda x: x.student_id):
            for bill in student_bills:
                bill.recalculate_amounts()
                bill.save(update_fields=['total_amount_due', 'previous_arrears', 'payment_status'], skip_pdf_generation=False)
                updated_count += 1
        
        logger.info(f"🗑️ BILLING ITEM DELETED - {updated_count} bills recalculated with PDF regeneration")


@receiver(post_delete, sender=CustomCharge)
def custom_charge_deleted(sender, instance, **kwargs):
    """
    CRITICAL: Log deletion and recalculate bill + regenerate PDF when custom charge is deleted
    """
    try:
        if hasattr(instance, 'student_bill') and instance.student_bill:
            user = getattr(instance, '_current_user', None)
            
            if user is None:
                user = instance.created_by
            
            # Log deletion in StudentBillLog
            StudentBillLog.objects.create(
                bill=instance.student_bill,
                field_name='custom_charge_removed',
                old_value=f'{instance.charge_name}: GHS {instance.amount}',
                new_value='',
                user_first_name=user.first_name,
                user_last_name=user.last_name,
                user_email=user.email
            )
            
            # Recalculate and save bill (PDF regenerated automatically)
            bill = instance.student_bill
            bill.recalculate_amounts()
            bill.save(update_fields=['total_amount_due', 'payment_status'], skip_pdf_generation=False)
            
            # Recalculate subsequent bills
            bill.recalculate_all_subsequent_bills()
            
            logger.info(f"🗑️ CUSTOM CHARGE DELETED - '{instance.charge_name}' - Bill {bill.bill_number} PDF regenerated")
    except Exception as e:
        logger.error(f"Error in custom_charge_deleted signal: {e}")


@receiver(post_delete, sender=PaymentReceipt)
def payment_receipt_deleted(sender, instance, **kwargs):
    """
    CRITICAL: Update bill total_paid, log deletion, and regenerate PDF when payment receipt is deleted
    """
    try:
        if hasattr(instance, 'student_bill') and instance.student_bill:
            user = getattr(instance, '_current_user', None)
            
            if user is None:
                user = instance.created_by
            
            StudentBillLog.objects.create(
                bill=instance.student_bill,
                field_name='payment_receipt_removed',
                old_value=f'Receipt #{instance.receipt_number}: GHS {instance.amount_paid}',
                new_value='',
                user_first_name=user.first_name,
                user_last_name=user.last_name,
                user_email=user.email
            )
            
            total_paid = sum(
                receipt.amount_paid 
                for receipt in instance.student_bill.payment_receipts.exclude(pk=instance.pk)
            )
            
            bill = instance.student_bill
            bill.total_paid = total_paid
            bill.update_payment_status()
            bill.save(update_fields=['total_paid', 'payment_status'], skip_pdf_generation=False)
            
            bill.recalculate_all_subsequent_bills()
            
            logger.info(f"🗑️ PAYMENT DELETED - Receipt {instance.receipt_number} - Bill {bill.bill_number} PDF regenerated")
    except Exception as e:
        logger.error(f"Error in payment_receipt_deleted signal: {e}")


@receiver(post_save, sender=BillingTemplate)
def billing_template_saved(sender, instance, created, **kwargs):
    """
    CRITICAL: Regenerate PDFs for ALL bills when template due_date changes
    This ensures due date changes are reflected in all PDFs
    """
    if not created:
        from django.db import transaction
        from itertools import groupby
        
        with transaction.atomic():
            bills_to_update = StudentBill.objects.select_for_update().filter(
                billing_template=instance
            ).order_by('student_id', 'generated_date')
            
            updated_count = 0
            for student_id, student_bills in groupby(bills_to_update, key=lambda x: x.student_id):
                for bill in student_bills:
                    # Update due date if it changed
                    if bill.due_date != instance.due_date:
                        bill.due_date = instance.due_date
                        bill.save(update_fields=['due_date'], skip_pdf_generation=False)
                    else:
                        # Just regenerate PDF for template info changes
                        bill.generate_pdf()
                    updated_count += 1
            
            logger.info(f"🔄 BILLING TEMPLATE UPDATED - {updated_count} bills PDFs regenerated")