from rest_framework import generics, permissions, serializers, status
from rest_framework.response import Response
from rest_framework.views import APIView
from rest_framework.decorators import api_view, permission_classes
from rest_framework.exceptions import ValidationError
from django.utils import timezone
from django.db.models import Sum, Q
from django.db import transaction
from django.urls import reverse
from django.shortcuts import get_object_or_404
from django.template import Template, Context
import os
import jwt
import logging

# Email imports
from sib_api_v3_sdk import Configuration, ApiClient, SendSmtpEmail
from sib_api_v3_sdk.api.transactional_emails_api import TransactionalEmailsApi
from sib_api_v3_sdk.rest import ApiException
from django.conf import settings

# Twilio import
from twilio.rest import Client as TwilioClient

from .models import (
    BillingTemplate, BillingItem, StudentBill, CustomCharge, PaymentReceipt, BillingItemLog
)
from .serializers import (
    BillingTemplateSerializer, BillingItemSerializer, StudentBillSerializer,
    StudentBillCreateSerializer, PaymentReceiptSerializer,
    BillingItemLogSerializer, StudentBillSummarySerializer
)
from .models import StudentBillLog as BillLog
from .serializers import StudentBillLogSerializer as BillLogSerializer

from django.contrib.auth import get_user_model
User = get_user_model()

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Template loader helper (shared by email and SMS)
# ---------------------------------------------------------------------------
TEMPLATES_DIR = os.path.join(os.path.dirname(__file__), 'email_templates')
SMS_TEMPLATES_DIR = os.path.join(os.path.dirname(__file__), 'sms_templates')


def _render_email_template(template_filename: str, context: dict) -> str:
    """
    Load an HTML email template from the email_templates/ directory and
    render it by performing simple string substitution using {{ key }} placeholders.
    """
    template_path = os.path.join(TEMPLATES_DIR, template_filename)
    with open(template_path, 'r', encoding='utf-8') as f:
        html = f.read()
    for key, value in context.items():
        html = html.replace('{{ ' + key + ' }}', str(value))
    return html


def _render_sms_template(template_filename: str, context: dict) -> str:
    """
    Load a plain-text SMS template from the sms_templates/ directory and
    render it by performing simple string substitution using {{ key }} placeholders.
    """
    template_path = os.path.join(SMS_TEMPLATES_DIR, template_filename)
    with open(template_path, 'r', encoding='utf-8') as f:
        text = f.read()
    for key, value in context.items():
        text = text.replace('{{ ' + key + ' }}', str(value))
    return text.strip()


# ---------------------------------------------------------------------------
# SMS service
# ---------------------------------------------------------------------------
class BillingSMSService:
    """Service class to handle billing-related SMS notifications via Twilio."""

    @staticmethod
    def _send(to_phone: str, body: str) -> bool:
        """Low-level helper: dispatch an SMS via Twilio."""
        try:
            client = TwilioClient(settings.TWILIO_ACCOUNT_SID, settings.TWILIO_AUTH_TOKEN)
            message = client.messages.create(
                body=body,
                from_=settings.TWILIO_PHONE_NUMBER,
                to=to_phone,
            )
            logger.info(f"SMS sent to {to_phone} | SID: {message.sid}")
            return True
        except Exception as e:
            logger.error(f"Twilio error sending SMS to {to_phone}: {e}")
            return False

    @staticmethod
    def _get_guardians_with_phone(student):
        """Return all ParentGuardian records for the student that have a primary_phone."""
        return student.guardians.exclude(primary_phone='').order_by(
            '-is_primary_contact', 'last_name'
        )

    # ------------------------------------------------------------------
    # Bill published
    # ------------------------------------------------------------------
    @staticmethod
    def send_bill_published_sms(student_bill) -> int:
        """
        Send SMS notification to every guardian when a bill is published.
        Returns the number of SMS messages successfully sent.
        """
        student = student_bill.student
        billing_template = student_bill.billing_template

        context = {
            'student_first_name': student.first_name,
            'student_last_name': student.last_name,
            'class_name': billing_template.class_name,
            'term_display': billing_template.get_term_display(),
            'academic_year': billing_template.academic_year,
            'bill_number': student_bill.bill_number,
            'total_amount_due': f"{student_bill.total_amount_due:,.2f}",
            'balance_due': f"{student_bill.balance_due:,.2f}",
            'due_date': student_bill.due_date.strftime('%B %d, %Y'),
        }

        guardians = BillingSMSService._get_guardians_with_phone(student)
        sent_count = 0

        for guardian in guardians:
            guardian_context = {**context, 'guardian_first_name': guardian.first_name}
            body = _render_sms_template('bill_published_sms.txt', guardian_context)
            sent = BillingSMSService._send(to_phone=guardian.primary_phone, body=body)
            if sent:
                sent_count += 1
            else:
                logger.warning(
                    f"Failed to send bill-published SMS to guardian "
                    f"{guardian.full_name} <{guardian.primary_phone}> "
                    f"for bill {student_bill.bill_number}"
                )

        logger.info(
            f"Bill published SMS for {student_bill.bill_number}: "
            f"{sent_count}/{guardians.count()} sent"
        )
        return sent_count

    # ------------------------------------------------------------------
    # Payment receipt
    # ------------------------------------------------------------------
    @staticmethod
    def send_payment_receipt_sms(payment_receipt) -> int:
        """
        Send SMS notification to every guardian when a payment is recorded.
        Returns the number of SMS messages successfully sent.
        """
        student_bill = payment_receipt.student_bill
        student = student_bill.student

        context = {
            'student_first_name': student.first_name,
            'student_last_name': student.last_name,
            'receipt_number': payment_receipt.receipt_number,
            'amount_paid': f"{payment_receipt.amount_paid:,.2f}",
            'payment_method_display': payment_receipt.get_payment_method_display(),
            'payment_date': payment_receipt.payment_date.strftime('%B %d, %Y at %I:%M %p'),
            'bill_number': student_bill.bill_number,
            'balance_due': f"{student_bill.balance_due:,.2f}",
        }

        guardians = BillingSMSService._get_guardians_with_phone(student)
        sent_count = 0

        for guardian in guardians:
            guardian_context = {**context, 'guardian_first_name': guardian.first_name}
            body = _render_sms_template('payment_receipt_sms.txt', guardian_context)
            sent = BillingSMSService._send(to_phone=guardian.primary_phone, body=body)
            if sent:
                sent_count += 1
            else:
                logger.warning(
                    f"Failed to send payment-receipt SMS to guardian "
                    f"{guardian.full_name} <{guardian.primary_phone}> "
                    f"for receipt {payment_receipt.receipt_number}"
                )

        logger.info(
            f"Payment receipt SMS for {payment_receipt.receipt_number}: "
            f"{sent_count}/{guardians.count()} sent"
        )
        return sent_count


# ---------------------------------------------------------------------------
# Email service
# ---------------------------------------------------------------------------
class BillingEmailService:
    """Service class to handle billing-related email notifications"""

    PORTAL_BILLS_URL = 'http://localhost:5173/student-dashboard/bills'

    @staticmethod
    def _send(to_email: str, to_name: str, subject: str, html_content: str) -> bool:
        """Low-level helper: dispatch a transactional email via Brevo."""
        try:
            configuration = Configuration()
            configuration.api_key['api-key'] = settings.BREVO_API_KEY

            api_instance = TransactionalEmailsApi(ApiClient(configuration))

            send_smtp_email = SendSmtpEmail(
                to=[{"email": to_email, "name": to_name}],
                sender={"name": "School Finance Department", "email": settings.DEFAULT_FROM_EMAIL},
                subject=subject,
                html_content=html_content,
            )

            api_response = api_instance.send_transac_email(send_smtp_email)
            logger.info(f"Email sent to {to_email} | subject: '{subject}' | response: {api_response}")
            return True

        except ApiException as e:
            logger.error(f"Brevo ApiException sending to {to_email}: {e}")
            return False
        except Exception as e:
            logger.error(f"Unexpected error sending email to {to_email}: {e}")
            return False

    @staticmethod
    def _get_guardians_with_email(student):
        """
        Return all ParentGuardian records for the student that have a non-empty email.
        Ordered so primary contact comes first (matches the model's Meta ordering).
        """
        return student.guardians.filter(email__isnull=False).exclude(email='').order_by(
            '-is_primary_contact', 'last_name'
        )

    # ------------------------------------------------------------------
    # Bill published
    # ------------------------------------------------------------------
    @staticmethod
    def send_bill_published_email(student_bill) -> bool:
        """
        Send email notification when a bill status changes to PUBLISHED.
        Sends to the student AND to every guardian who has an email address.
        Works for any transition: DRAFT->PUBLISHED, SCHEDULED->PUBLISHED,
        or direct PUBLISHED creation.
        """
        student = student_bill.student
        billing_template = student_bill.billing_template

        # Shared context values used by both student and guardian templates
        shared_context = {
            'student_first_name': student.first_name,
            'student_last_name': student.last_name,
            'class_name': billing_template.class_name,
            'term_display': billing_template.get_term_display(),
            'academic_year': billing_template.academic_year,
            'bill_number': student_bill.bill_number,
            'total_amount_due': f"{student_bill.total_amount_due:,.2f}",
            'previous_arrears': f"{student_bill.previous_arrears:,.2f}",
            'balance_due': f"{student_bill.balance_due:,.2f}",
            'due_date': student_bill.due_date.strftime('%B %d, %Y'),
            'portal_url': BillingEmailService.PORTAL_BILLS_URL,
        }

        subject = f"School Fee Bill Published - {student_bill.bill_number}"

        # ── 1. Email the student ──────────────────────────────────────────
        student_html = _render_email_template('bill_published_email.html', shared_context)
        student_email_sent = BillingEmailService._send(
            to_email=student.email,
            to_name=f"{student.first_name} {student.last_name}",
            subject=subject,
            html_content=student_html,
        )

        # ── 2. Email each guardian who has an email address ───────────────
        guardians = BillingEmailService._get_guardians_with_email(student)
        guardian_emails_sent = 0

        for guardian in guardians:
            guardian_context = {
                **shared_context,
                'guardian_first_name': guardian.first_name,
                'guardian_last_name': guardian.last_name,
            }
            guardian_html = _render_email_template(
                'guardian_bill_published_email.html', guardian_context
            )
            sent = BillingEmailService._send(
                to_email=guardian.email,
                to_name=guardian.full_name,
                subject=subject,
                html_content=guardian_html,
            )
            if sent:
                guardian_emails_sent += 1
            else:
                logger.warning(
                    f"Failed to send bill-published email to guardian "
                    f"{guardian.full_name} <{guardian.email}> "
                    f"for bill {student_bill.bill_number}"
                )

        logger.info(
            f"Bill published notifications for {student_bill.bill_number}: "
            f"student={'sent' if student_email_sent else 'failed'}, "
            f"guardians={guardian_emails_sent}/{guardians.count()} sent"
        )

        # Return True only if the student email succeeded (guardian emails are best-effort)
        return student_email_sent

    # ------------------------------------------------------------------
    # Payment receipt
    # ------------------------------------------------------------------
    @staticmethod
    def send_payment_receipt_email(payment_receipt) -> bool:
        """
        Send email notification when a payment is made.
        Sends to the student AND to every guardian who has an email address.
        """
        student_bill = payment_receipt.student_bill
        student = student_bill.student

        # Shared context values used by both student and guardian templates
        shared_context = {
            'student_first_name': student.first_name,
            'student_last_name': student.last_name,
            'receipt_number': payment_receipt.receipt_number,
            'amount_paid': f"{payment_receipt.amount_paid:,.2f}",
            'payment_method_display': payment_receipt.get_payment_method_display(),
            'payment_date': payment_receipt.payment_date.strftime('%B %d, %Y at %I:%M %p'),
            'bill_number': student_bill.bill_number,
            'balance_due': f"{student_bill.balance_due:,.2f}",
            'portal_url': BillingEmailService.PORTAL_BILLS_URL,
        }

        subject = f"Payment Receipt - {payment_receipt.receipt_number}"

        # ── 1. Email the student ──────────────────────────────────────────
        student_html = _render_email_template('payment_receipt_email.html', shared_context)
        student_email_sent = BillingEmailService._send(
            to_email=student.email,
            to_name=f"{student.first_name} {student.last_name}",
            subject=subject,
            html_content=student_html,
        )

        # ── 2. Email each guardian who has an email address ───────────────
        guardians = BillingEmailService._get_guardians_with_email(student)
        guardian_emails_sent = 0

        for guardian in guardians:
            guardian_context = {
                **shared_context,
                'guardian_first_name': guardian.first_name,
                'guardian_last_name': guardian.last_name,
            }
            guardian_html = _render_email_template(
                'guardian_payment_receipt_email.html', guardian_context
            )
            sent = BillingEmailService._send(
                to_email=guardian.email,
                to_name=guardian.full_name,
                subject=subject,
                html_content=guardian_html,
            )
            if sent:
                guardian_emails_sent += 1
            else:
                logger.warning(
                    f"Failed to send payment-receipt email to guardian "
                    f"{guardian.full_name} <{guardian.email}> "
                    f"for receipt {payment_receipt.receipt_number}"
                )

        logger.info(
            f"Payment receipt notifications for {payment_receipt.receipt_number}: "
            f"student={'sent' if student_email_sent else 'failed'}, "
            f"guardians={guardian_emails_sent}/{guardians.count()} sent"
        )

        # Return True only if the student email succeeded (guardian emails are best-effort)
        return student_email_sent


def auto_publish_bills(queryset):
    """Auto-publish scheduled bills that are due and send email + SMS notifications"""
    now = timezone.now()
    bills_to_update = queryset.filter(
        status__in=['DRAFT', 'SCHEDULED'],
        scheduled_date__isnull=False,
        scheduled_date__lte=now
    )

    for bill in bills_to_update:
        bill.status = 'PUBLISHED'
        bill.save(update_fields=['status'])

        BillingEmailService.send_bill_published_email(bill)
        BillingSMSService.send_bill_published_sms(bill)

    return queryset

# --------------------
# Billing Templates
# --------------------
class BillingTemplateListCreateView(generics.ListCreateAPIView):
    queryset = BillingTemplate.objects.all()
    serializer_class = BillingTemplateSerializer
    permission_classes = [permissions.IsAuthenticated]

    def perform_create(self, serializer):
        serializer.save(created_by=self.request.user)

class BillingTemplateDetailView(generics.RetrieveUpdateDestroyAPIView):
    queryset = BillingTemplate.objects.all()
    serializer_class = BillingTemplateSerializer
    permission_classes = [permissions.IsAuthenticated]

# --------------------
# Billing Items
# --------------------
class BillingItemListCreateView(generics.ListCreateAPIView):
    queryset = BillingItem.objects.all()
    serializer_class = BillingItemSerializer
    permission_classes = [permissions.IsAuthenticated]

    def perform_create(self, serializer):
        serializer.save(created_by=self.request.user)

class BillingItemDetailView(generics.RetrieveUpdateDestroyAPIView):
    queryset = BillingItem.objects.all()
    serializer_class = BillingItemSerializer
    permission_classes = [permissions.IsAuthenticated]

    def perform_update(self, serializer):
        """Set user context when updating billing items for logging"""
        instance = serializer.save()

    def perform_destroy(self, instance):
        """Handle deletion of billing items and recalculate related bills"""
        BillingItemLog.objects.create(
            billing_item=instance,
            field_name='billing_item_status',
            old_value='EXISTS',
            new_value='DELETED',
            user_first_name=self.request.user.first_name,
            user_last_name=self.request.user.last_name,
            user_email=self.request.user.email
        )
        instance.delete()

# --------------------
# Billing Item Logs
# --------------------
class BillingItemLogListView(generics.ListAPIView):
    """View logs for a specific billing item"""
    serializer_class = BillingItemLogSerializer
    permission_classes = [permissions.IsAuthenticated]

    def get_queryset(self):
        billing_item_id = self.kwargs['billing_item_id']
        return BillingItemLog.objects.filter(billing_item_id=billing_item_id).order_by('-timestamp')

# --------------------
# Payment Receipts
# --------------------
class PaymentReceiptListCreateView(generics.ListCreateAPIView):
    serializer_class = PaymentReceiptSerializer
    permission_classes = [permissions.IsAuthenticated]

    def get_queryset(self):
        bill_id = self.kwargs.get('bill_id')
        if bill_id:
            return PaymentReceipt.objects.filter(student_bill_id=bill_id)
        return PaymentReceipt.objects.all()

    def perform_create(self, serializer):
        """Create payment receipt, send email + SMS, and log PDF regeneration"""
        receipt = serializer.save()

        bill = receipt.student_bill
        if bill.pdf_file:
            logger.info(f"✅ Payment receipt {receipt.receipt_number} added - Bill {bill.bill_number} PDF regenerated: {bill.pdf_file.name}")
        else:
            logger.warning(f"⚠️ Payment receipt {receipt.receipt_number} added but bill {bill.bill_number} PDF may not exist")

        BillingEmailService.send_payment_receipt_email(receipt)
        BillingSMSService.send_payment_receipt_sms(receipt)

class PaymentReceiptDetailView(generics.RetrieveUpdateDestroyAPIView):
    queryset = PaymentReceipt.objects.all()
    serializer_class = PaymentReceiptSerializer
    permission_classes = [permissions.IsAuthenticated]

    def perform_update(self, serializer):
        """Set user context when updating payment receipts for logging"""
        receipt = serializer.save()
        bill = receipt.student_bill
        if bill.pdf_file:
            logger.info(f"✅ Payment receipt {receipt.receipt_number} updated - Bill {bill.bill_number} PDF regenerated")

    def perform_destroy(self, instance):
        """Set user context and log deletion before deleting payment receipt"""
        instance._current_user = self.request.user
        instance.delete()

# --------------------
# Students
# --------------------
class StudentListView(generics.ListAPIView):
    """List students, optionally filtered by class_name"""
    permission_classes = [permissions.IsAuthenticated]

    def get_queryset(self):
        queryset = User.objects.filter(role='student')
        class_name = self.request.query_params.get('class_name', None)
        if class_name:
            queryset = queryset.filter(class_name__iexact=class_name)
        return queryset.order_by('first_name', 'last_name')

    def get_serializer_class(self):
        class StudentSerializer(serializers.ModelSerializer):
            class Meta:
                model = User
                fields = ['id', 'first_name', 'last_name', 'email', 'class_name']
        return StudentSerializer

# --------------------
# Student Bills (Admin/Staff)
# --------------------
class StudentBillListView(generics.ListAPIView):
    permission_classes = [permissions.IsAuthenticated]

    def get_queryset(self):
        user = self.request.user
        if user.role == 'student':
            queryset = StudentBill.objects.filter(student=user, status='PUBLISHED')
        else:
            queryset = StudentBill.objects.all()
        return auto_publish_bills(queryset)

    def get_serializer_class(self):
        summary = self.request.query_params.get('summary', 'false').lower() == 'true'
        if summary:
            return StudentBillSummarySerializer
        return StudentBillSerializer

class StudentBillCreateView(generics.CreateAPIView):
    serializer_class = StudentBillCreateSerializer
    permission_classes = [permissions.IsAuthenticated]

    def create(self, request, *args, **kwargs):
        try:
            response = super().create(request, *args, **kwargs)

            bill_id = response.data.get('id')
            if bill_id:
                try:
                    bill = StudentBill.objects.get(id=bill_id)
                    if bill.pdf_file:
                        logger.info(f"✅ Bill {bill.bill_number} created successfully with PDF: {bill.pdf_file.name}")
                    else:
                        logger.warning(f"⚠️ Bill {bill.bill_number} created but PDF generation may have failed")
                except StudentBill.DoesNotExist:
                    logger.error(f"❌ Bill with ID {bill_id} not found after creation")

            return Response({
                'success': True,
                'message': 'Bill created successfully',
                'data': response.data
            }, status=status.HTTP_201_CREATED)
        except ValidationError as e:
            error_detail = str(e.detail) if hasattr(e, 'detail') else str(e)

            if 'unique' in error_detail.lower() and ('student' in error_detail.lower() or 'billing_template' in error_detail.lower()):
                student_data = request.data.get('student')
                template_data = request.data.get('billing_template')

                try:
                    student = User.objects.get(id=student_data)
                    template = BillingTemplate.objects.get(id=template_data)
                    message = f"A bill already exists for {student.first_name} {student.last_name} for {template.class_name} - {template.get_term_display()} ({template.academic_year})"
                except (User.DoesNotExist, BillingTemplate.DoesNotExist):
                    message = "A bill already exists for this student and billing template combination"

                return Response({'success': False, 'message': message}, status=status.HTTP_400_BAD_REQUEST)

            return Response({'success': False, 'message': 'Please check your input data for errors'}, status=status.HTTP_400_BAD_REQUEST)

        except Exception as e:
            error_msg = str(e).lower()
            if 'unique' in error_msg:
                return Response({'success': False, 'message': 'A bill already exists for this student and term combination'}, status=status.HTTP_400_BAD_REQUEST)
            elif 'foreign key' in error_msg:
                return Response({'success': False, 'message': 'Invalid student or billing template selected'}, status=status.HTTP_400_BAD_REQUEST)
            else:
                return Response({'success': False, 'message': 'Unable to create bill. Please try again or contact support.'}, status=status.HTTP_400_BAD_REQUEST)

    def perform_create(self, serializer):
        bill = serializer.save()
        bill._current_user = self.request.user

        # Send email + SMS if bill is created directly as PUBLISHED
        if bill.status == 'PUBLISHED':
            BillingEmailService.send_bill_published_email(bill)
            BillingSMSService.send_bill_published_sms(bill)

class StudentBillDetailView(generics.RetrieveUpdateDestroyAPIView):
    queryset = StudentBill.objects.all()
    serializer_class = StudentBillSerializer
    permission_classes = [permissions.IsAuthenticated]

    def get_object(self):
        obj = super().get_object()
        if obj.status in ['DRAFT', 'SCHEDULED'] and obj.scheduled_date:
            if obj.scheduled_date <= timezone.now():
                obj.status = 'PUBLISHED'
                obj._current_user = self.request.user
                obj.save(update_fields=['status'])

                BillingEmailService.send_bill_published_email(obj)
                BillingSMSService.send_bill_published_sms(obj)
        return obj

    def perform_update(self, serializer):
        """Handle email + SMS notifications when status changes to PUBLISHED"""
        old_instance = StudentBill.objects.get(pk=serializer.instance.pk)
        old_status = old_instance.status

        bill = serializer.save()

        if bill.pdf_file:
            logger.info(f"✅ Bill {bill.bill_number} updated successfully - PDF regenerated: {bill.pdf_file.name}")
        else:
            logger.warning(f"⚠️ Bill {bill.bill_number} updated but PDF may not exist")

        if old_status != 'PUBLISHED' and bill.status == 'PUBLISHED':
            BillingEmailService.send_bill_published_email(bill)
            BillingSMSService.send_bill_published_sms(bill)

    @transaction.atomic
    def perform_destroy(self, instance):
        """Add logging before deleting and recalculate subsequent bills"""
        BillLog.objects.create(
            bill=instance,
            field_name='bill_status',
            old_value='EXISTS',
            new_value='DELETED',
            user_first_name=self.request.user.first_name,
            user_last_name=self.request.user.last_name,
            user_email=self.request.user.email
        )

        student = instance.student
        generated_date = instance.generated_date

        if instance.pdf_file:
            logger.info(f"🗑️ Deleting bill {instance.bill_number} - PDF file {instance.pdf_file.name} will be removed")

        instance.delete()

        subsequent_bills = StudentBill.objects.filter(
            student=student,
            generated_date__gt=generated_date
        ).order_by('generated_date')

        for bill in subsequent_bills:
            bill.recalculate_amounts()
            StudentBill.objects.filter(pk=bill.pk).update(
                previous_arrears=bill.previous_arrears,
                total_amount_due=bill.total_amount_due,
                payment_status=bill.payment_status
            )

# --------------------
# Publish Bill View
# --------------------
class PublishBillView(APIView):
    """API endpoint to manually publish a scheduled or draft bill"""
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request, bill_id):
        try:
            bill = StudentBill.objects.get(id=bill_id)

            if bill.status == 'PUBLISHED':
                return Response({'success': False, 'message': 'Bill is already published'}, status=status.HTTP_400_BAD_REQUEST)

            if bill.status not in ['DRAFT', 'SCHEDULED']:
                return Response({'success': False, 'message': f'Cannot publish bill with status: {bill.status}'}, status=status.HTTP_400_BAD_REQUEST)

            bill.status = 'PUBLISHED'
            bill._current_user = request.user
            bill.save(update_fields=['status'])

            if bill.pdf_file:
                logger.info(f"✅ Bill {bill.bill_number} published - PDF available: {bill.pdf_file.name}")

            email_sent = BillingEmailService.send_bill_published_email(bill)
            sms_sent = BillingSMSService.send_bill_published_sms(bill)

            return Response({
                'success': True,
                'message': (
                    f'Bill published successfully. '
                    f'Email {"sent" if email_sent else "failed"}. '
                    f'SMS sent to {sms_sent} guardian(s).'
                ),
                'bill_number': bill.bill_number,
                'status': bill.status
            })

        except StudentBill.DoesNotExist:
            return Response({'success': False, 'message': 'Bill not found'}, status=status.HTTP_404_NOT_FOUND)
        except Exception as e:
            logger.error(f"Error publishing bill {bill_id}: {e}")
            return Response({'success': False, 'message': 'Error publishing bill'}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

# --------------------
# Bulk Publish Bills View
# --------------------
class BulkPublishBillsView(APIView):
    """API endpoint to bulk publish multiple scheduled or draft bills"""
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request):
        bill_ids = request.data.get('bill_ids', [])

        if not bill_ids:
            return Response({'success': False, 'message': 'No bill IDs provided'}, status=status.HTTP_400_BAD_REQUEST)

        published_bills = []
        failed_bills = []

        for bill_id in bill_ids:
            try:
                bill = StudentBill.objects.get(id=bill_id)

                if bill.status in ['DRAFT', 'SCHEDULED']:
                    bill.status = 'PUBLISHED'
                    bill._current_user = request.user
                    bill.save(update_fields=['status'])

                    email_sent = BillingEmailService.send_bill_published_email(bill)
                    sms_sent = BillingSMSService.send_bill_published_sms(bill)

                    published_bills.append({
                        'id': bill.id,
                        'bill_number': bill.bill_number,
                        'email_sent': email_sent,
                        'sms_sent': sms_sent,
                        'pdf_available': bool(bill.pdf_file)
                    })
                elif bill.status == 'PUBLISHED':
                    failed_bills.append({'id': bill.id, 'bill_number': bill.bill_number, 'reason': 'Bill is already published'})
                else:
                    failed_bills.append({'id': bill.id, 'bill_number': bill.bill_number, 'reason': f'Cannot publish bill with status: {bill.status}'})

            except StudentBill.DoesNotExist:
                failed_bills.append({'id': bill_id, 'reason': 'Bill not found'})
            except Exception as e:
                failed_bills.append({'id': bill_id, 'reason': str(e)})

        return Response({
            'success': True,
            'message': f'Successfully published {len(published_bills)} bills',
            'published_bills': published_bills,
            'failed_bills': failed_bills
        })

# --------------------
# Student Bill Logs
# --------------------
class StudentBillLogListView(generics.ListAPIView):
    """View logs for a specific bill"""
    serializer_class = BillLogSerializer
    permission_classes = [permissions.IsAuthenticated]

    def get_queryset(self):
        bill_id = self.kwargs['bill_id']
        return BillLog.objects.filter(bill_id=bill_id).order_by('-timestamp')

# --------------------
# Bulk Operations for Balance Due Management
# --------------------
@api_view(['POST'])
@permission_classes([permissions.IsAuthenticated])
@transaction.atomic
def recalculate_student_balances(request):
    """Force recalculation of all student balances."""
    student_id = request.data.get('student_id')

    if student_id:
        bills = StudentBill.objects.filter(student_id=student_id).order_by('generated_date')
    else:
        bills = StudentBill.objects.all().order_by('student_id', 'generated_date')

    updated_count = 0
    for bill in bills:
        old_arrears = bill.previous_arrears
        old_amount = bill.total_amount_due

        bill.recalculate_amounts()

        if old_arrears != bill.previous_arrears or old_amount != bill.total_amount_due:
            StudentBill.objects.filter(pk=bill.pk).update(
                previous_arrears=bill.previous_arrears,
                total_amount_due=bill.total_amount_due,
                payment_status=bill.payment_status
            )
            updated_count += 1
            bill.generate_pdf()

    logger.info(f"✅ Recalculated {updated_count} bills with PDF regeneration")

    return Response({
        'message': f'Successfully recalculated {updated_count} bills',
        'updated_count': updated_count
    })

@api_view(['GET'])
@permission_classes([permissions.IsAuthenticated])
def student_balance_summary(request, student_id):
    """Get comprehensive balance summary for a student"""
    try:
        student = User.objects.get(id=student_id, role='student')
    except User.DoesNotExist:
        return Response({'error': 'Student not found'}, status=404)

    bills = StudentBill.objects.filter(
        student=student,
        status='PUBLISHED'
    ).order_by('generated_date')

    total_billed = bills.aggregate(Sum('total_amount_due'))['total_amount_due__sum'] or 0
    total_paid = bills.aggregate(Sum('total_paid'))['total_paid__sum'] or 0

    latest_bill = bills.last()
    current_outstanding = float(latest_bill.balance_due) if latest_bill else 0

    bills_data = []
    for bill in bills:
        bills_data.append({
            'id': bill.id,
            'bill_number': bill.bill_number,
            'class_term': f"{bill.billing_template.class_name} - {bill.billing_template.get_term_display()}",
            'academic_year': bill.billing_template.academic_year,
            'total_amount_due': float(bill.total_amount_due),
            'total_paid': float(bill.total_paid),
            'current_bill_balance': float(bill.total_amount_due - bill.total_paid),
            'previous_arrears': float(bill.previous_arrears),
            'balance_due_at_time': float(bill.balance_due),
            'payment_status': bill.payment_status,
            'due_date': bill.due_date,
            'is_overdue': bill.is_overdue,
            'pdf_url': request.build_absolute_uri(bill.pdf_file.url) if bill.pdf_file else None
        })

    return Response({
        'student': {
            'id': student.id,
            'name': f"{student.first_name} {student.last_name}",
            'current_class': student.class_name,
            'email': student.email
        },
        'summary': {
            'total_bills': len(bills_data),
            'total_billed': float(total_billed),
            'total_paid': float(total_paid),
            'current_outstanding_balance': current_outstanding,
            'paid_bills': len([b for b in bills_data if b['payment_status'] == 'paid']),
            'overdue_bills': len([b for b in bills_data if b['is_overdue']])
        },
        'bills': bills_data
    })

# --------------------
# Student-Only Bill APIs
# --------------------
class StudentOnlyPermission(permissions.BasePermission):
    """Custom permission to only allow students to access their own bills."""
    def has_permission(self, request, view):
        return request.user.is_authenticated and request.user.role == 'student'


class StudentMyBillsView(generics.ListAPIView):
    """Simplified API for students to view all their published bills."""
    serializer_class = StudentBillSerializer
    permission_classes = [StudentOnlyPermission]

    def get_queryset(self):
        return StudentBill.objects.filter(
            student=self.request.user,
            status='PUBLISHED'
        ).order_by('-generated_date')

    def list(self, request, *args, **kwargs):
        queryset = self.get_queryset()
        serializer = self.get_serializer(queryset, many=True)

        total_outstanding = 0
        total_paid = 0
        current_balance = 0

        if serializer.data:
            current_balance = float(serializer.data[0].get('total_outstanding', 0))
            total_paid = sum(float(bill.get('total_paid', 0)) for bill in serializer.data)
            total_outstanding = current_balance

        return Response({
            'student': f"{request.user.first_name} {request.user.last_name}",
            'current_class': request.user.class_name,
            'summary': {
                'total_bills': len(serializer.data),
                'total_outstanding_balance': total_outstanding,
                'total_paid': total_paid,
                'paid_bills': len([b for b in serializer.data if b.get('payment_status') == 'paid']),
                'overdue_bills': len([b for b in serializer.data if b.get('is_overdue', False)])
            },
            'bills': serializer.data
        })


class StudentCurrentClassBillsView(generics.ListAPIView):
    """API for students to view bills for their current class only."""
    serializer_class = StudentBillSerializer
    permission_classes = [StudentOnlyPermission]

    def get_queryset(self):
        return StudentBill.objects.filter(
            student=self.request.user,
            billing_template__class_name=self.request.user.class_name,
            status='PUBLISHED'
        ).order_by('-generated_date')


class StudentPreviousClassBillsView(generics.ListAPIView):
    """API for students to view bills from their previous classes."""
    serializer_class = StudentBillSerializer
    permission_classes = [StudentOnlyPermission]

    def get_queryset(self):
        return StudentBill.objects.filter(
            student=self.request.user,
            status='PUBLISHED'
        ).exclude(
            billing_template__class_name=self.request.user.class_name
        ).order_by('-generated_date')

# --------------------
# Advanced Payment Management
# --------------------
@api_view(['POST'])
@permission_classes([permissions.IsAuthenticated])
@transaction.atomic
def bulk_payment_update(request):
    """Update multiple payment receipts at once."""
    updates = request.data.get('updates', [])
    updated_receipts = []

    for update_data in updates:
        receipt_id = update_data.get('id')
        if not receipt_id:
            continue

        try:
            receipt = PaymentReceipt.objects.get(id=receipt_id)
            receipt._current_user = request.user

            for field, value in update_data.items():
                if field != 'id' and hasattr(receipt, field):
                    setattr(receipt, field, value)

            receipt.save()
            updated_receipts.append(receipt.id)
            logger.info(f"✅ Payment receipt {receipt.receipt_number} updated - Bill PDF regenerated")

        except PaymentReceipt.DoesNotExist:
            continue

    return Response({
        'message': f'Successfully updated {len(updated_receipts)} payment receipts',
        'updated_receipts': updated_receipts
    })