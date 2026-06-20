from decimal import Decimal
import logging
import os

from django.conf import settings
from django.contrib.auth import get_user_model
from django.db import transaction
from django.db.models import Sum
from django.utils import timezone

from rest_framework import generics, permissions, serializers, status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.exceptions import ValidationError
from rest_framework.response import Response
from rest_framework.views import APIView

# Email imports
from sib_api_v3_sdk import Configuration, ApiClient, SendSmtpEmail
from sib_api_v3_sdk.api.transactional_emails_api import TransactionalEmailsApi
from sib_api_v3_sdk.rest import ApiException

# Twilio import
from twilio.rest import Client as TwilioClient

from .models import (
    BillingTemplate,
    BillingItem,
    StudentBill,
    PaymentReceipt,
    BillingItemLog,
    PaymentReceiptRequest,
    PaymentReceiptRequestLog,
    StudentBillLog as BillLog,
    _decimal_str,
)
from .serializers import (
    BillingTemplateSerializer,
    BillingItemSerializer,
    StudentBillSerializer,
    StudentBillCreateSerializer,
    PaymentReceiptSerializer,
    BillingItemLogSerializer,
    StudentBillSummarySerializer,
    PaymentReceiptRequestSerializer,
    PaymentReceiptRequestCreateSerializer,
    PaymentReceiptRequestReviewSerializer,
    PaymentReceiptRequestLogSerializer,
    StudentBillLogSerializer as BillLogSerializer,
)

# Celery tasks
from .tasks import (
    send_bill_published_email_task,
    send_bill_published_sms_task,
    send_payment_receipt_email_task,
    send_payment_receipt_sms_task,
    recalculate_student_balances_task,
    generate_bill_pdf_task,
    generate_payment_receipt_pdf_task,
)

User = get_user_model()

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# PERMISSIONS
# ---------------------------------------------------------------------------

class StaffOrAdminPermission(permissions.BasePermission):
    """Allows access only to authenticated non-student users (staff or admin)."""

    def has_permission(self, request, view):
        return (
            request.user
            and request.user.is_authenticated
            and request.user.role != "student"
        )


class StudentOnlyPermission(permissions.BasePermission):
    """Allows access only to authenticated students."""

    def has_permission(self, request, view):
        return (
            request.user
            and request.user.is_authenticated
            and request.user.role == "student"
        )


class IsAuthenticatedReadStaffWrite(permissions.BasePermission):
    """
    Safe methods → any authenticated user.
    Unsafe methods → staff/admin only.
    """

    def has_permission(self, request, view):
        if not request.user or not request.user.is_authenticated:
            return False
        if request.method in permissions.SAFE_METHODS:
            return True
        return request.user.role != "student"


# ---------------------------------------------------------------------------
# Template loader helper (shared by email and SMS)
# ---------------------------------------------------------------------------

TEMPLATES_DIR = os.path.join(os.path.dirname(__file__), "email_templates")
SMS_TEMPLATES_DIR = os.path.join(os.path.dirname(__file__), "sms_templates")


def _render_email_template(template_filename: str, context: dict) -> str:
    template_path = os.path.join(TEMPLATES_DIR, template_filename)
    with open(template_path, "r", encoding="utf-8") as f:
        html = f.read()
    for key, value in context.items():
        html = html.replace("{{ " + key + " }}", str(value))
    return html


def _render_sms_template(template_filename: str, context: dict) -> str:
    template_path = os.path.join(SMS_TEMPLATES_DIR, template_filename)
    with open(template_path, "r", encoding="utf-8") as f:
        text = f.read()
    for key, value in context.items():
        text = text.replace("{{ " + key + " }}", str(value))
    return text.strip()


# ---------------------------------------------------------------------------
# SMS service
# ---------------------------------------------------------------------------

class BillingSMSService:
    """Service class to handle billing-related SMS notifications via Twilio."""

    @staticmethod
    def _send(to_phone: str, body: str) -> bool:
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
        return student.guardians.exclude(primary_phone="").order_by(
            "-is_primary_contact", "last_name"
        )

    @staticmethod
    def send_bill_published_sms(student_bill) -> int:
        student = student_bill.student
        billing_template = student_bill.billing_template

        context = {
            "student_first_name": student.first_name,
            "student_last_name": student.last_name,
            "class_name": billing_template.class_name,
            "term_display": billing_template.get_term_display(),
            "academic_year": billing_template.academic_year,
            "bill_number": student_bill.bill_number,
            "total_amount_due": f"{student_bill.total_amount_due:,.2f}",
            "balance_due": f"{student_bill.balance_due:,.2f}",
            "due_date": student_bill.due_date.strftime("%B %d, %Y"),
        }

        guardians = BillingSMSService._get_guardians_with_phone(student)
        sent_count = 0

        for guardian in guardians:
            guardian_context = {**context, "guardian_first_name": guardian.first_name}
            body = _render_sms_template("bill_published_sms.txt", guardian_context)
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

    @staticmethod
    def send_payment_receipt_sms(payment_receipt) -> int:
        student_bill = payment_receipt.student_bill
        student = student_bill.student

        context = {
            "student_first_name": student.first_name,
            "student_last_name": student.last_name,
            "receipt_number": payment_receipt.receipt_number,
            "amount_paid": f"{payment_receipt.amount_paid:,.2f}",
            "payment_method_display": payment_receipt.get_payment_method_display(),
            "payment_date": payment_receipt.payment_date.strftime("%B %d, %Y at %I:%M %p"),
            "bill_number": student_bill.bill_number,
            "balance_due": f"{student_bill.balance_due:,.2f}",
        }

        guardians = BillingSMSService._get_guardians_with_phone(student)
        sent_count = 0

        for guardian in guardians:
            guardian_context = {**context, "guardian_first_name": guardian.first_name}
            body = _render_sms_template("payment_receipt_sms.txt", guardian_context)
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
    """Service class to handle billing-related email notifications."""

    PORTAL_BILLS_URL = "http://localhost:5173/student-dashboard/bills"

    @staticmethod
    def _send(to_email: str, to_name: str, subject: str, html_content: str) -> bool:
        try:
            configuration = Configuration()
            configuration.api_key["api-key"] = settings.BREVO_API_KEY

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
        return student.guardians.filter(email__isnull=False).exclude(email="").order_by(
            "-is_primary_contact", "last_name"
        )

    @staticmethod
    def send_bill_published_email(student_bill) -> bool:
        student = student_bill.student
        billing_template = student_bill.billing_template

        shared_context = {
            "student_first_name": student.first_name,
            "student_last_name": student.last_name,
            "class_name": billing_template.class_name,
            "term_display": billing_template.get_term_display(),
            "academic_year": billing_template.academic_year,
            "bill_number": student_bill.bill_number,
            "total_amount_due": f"{student_bill.total_amount_due:,.2f}",
            "previous_arrears": f"{student_bill.previous_arrears:,.2f}",
            "balance_due": f"{student_bill.balance_due:,.2f}",
            "due_date": student_bill.due_date.strftime("%B %d, %Y"),
            "portal_url": BillingEmailService.PORTAL_BILLS_URL,
        }

        subject = f"School Fee Bill Published - {student_bill.bill_number}"

        student_html = _render_email_template("bill_published_email.html", shared_context)
        student_email_sent = BillingEmailService._send(
            to_email=student.email,
            to_name=f"{student.first_name} {student.last_name}",
            subject=subject,
            html_content=student_html,
        )

        guardians = BillingEmailService._get_guardians_with_email(student)
        guardian_emails_sent = 0

        for guardian in guardians:
            guardian_context = {
                **shared_context,
                "guardian_first_name": guardian.first_name,
                "guardian_last_name": guardian.last_name,
            }
            guardian_html = _render_email_template(
                "guardian_bill_published_email.html", guardian_context
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

        return student_email_sent

    @staticmethod
    def send_payment_receipt_email(payment_receipt) -> bool:
        student_bill = payment_receipt.student_bill
        student = student_bill.student

        shared_context = {
            "student_first_name": student.first_name,
            "student_last_name": student.last_name,
            "receipt_number": payment_receipt.receipt_number,
            "amount_paid": f"{payment_receipt.amount_paid:,.2f}",
            "payment_method_display": payment_receipt.get_payment_method_display(),
            "payment_date": payment_receipt.payment_date.strftime("%B %d, %Y at %I:%M %p"),
            "bill_number": student_bill.bill_number,
            "balance_due": f"{student_bill.balance_due:,.2f}",
            "portal_url": BillingEmailService.PORTAL_BILLS_URL,
        }

        subject = f"Payment Receipt - {payment_receipt.receipt_number}"

        student_html = _render_email_template("payment_receipt_email.html", shared_context)
        student_email_sent = BillingEmailService._send(
            to_email=student.email,
            to_name=f"{student.first_name} {student.last_name}",
            subject=subject,
            html_content=student_html,
        )

        guardians = BillingEmailService._get_guardians_with_email(student)
        guardian_emails_sent = 0

        for guardian in guardians:
            guardian_context = {
                **shared_context,
                "guardian_first_name": guardian.first_name,
                "guardian_last_name": guardian.last_name,
            }
            guardian_html = _render_email_template(
                "guardian_payment_receipt_email.html", guardian_context
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

        return student_email_sent


# ===========================================================================
# BILLING TEMPLATES
# ===========================================================================

class BillingTemplateListCreateView(generics.ListCreateAPIView):
    queryset = BillingTemplate.objects.all()
    serializer_class = BillingTemplateSerializer
    permission_classes = [IsAuthenticatedReadStaffWrite]

    def perform_create(self, serializer):
        serializer.save()


class BillingTemplateDetailView(generics.RetrieveUpdateDestroyAPIView):
    queryset = BillingTemplate.objects.all()
    serializer_class = BillingTemplateSerializer
    permission_classes = [IsAuthenticatedReadStaffWrite]


# ===========================================================================
# BILLING ITEMS
# ===========================================================================

class BillingItemListCreateView(generics.ListCreateAPIView):
    queryset = BillingItem.objects.all()
    serializer_class = BillingItemSerializer
    permission_classes = [IsAuthenticatedReadStaffWrite]

    def perform_create(self, serializer):
        serializer.save()


class BillingItemDetailView(generics.RetrieveUpdateDestroyAPIView):
    queryset = BillingItem.objects.all()
    serializer_class = BillingItemSerializer
    permission_classes = [IsAuthenticatedReadStaffWrite]

    def perform_update(self, serializer):
        item = serializer.save()
        logger.info(
            f"[BillingItemDetailView] Item '{item.item_name}' updated. "
            f"Bill recalculation/PDF queue handled by model logic."
        )

    def perform_destroy(self, instance):
        BillingItemLog.objects.create(
            billing_item=instance,
            field_name="billing_item_status",
            old_value="EXISTS",
            new_value="DELETED",
            user_first_name=self.request.user.first_name,
            user_last_name=self.request.user.last_name,
            user_email=self.request.user.email,
        )
        instance._current_user = self.request.user
        instance.delete()
        logger.info(
            f"[BillingItemDetailView] Item '{instance.item_name}' deleted. "
            f"Bill recalculation/PDF queue handled by model logic."
        )


# ===========================================================================
# BILLING ITEM LOGS
# ===========================================================================

class BillingItemLogListView(generics.ListAPIView):
    serializer_class = BillingItemLogSerializer
    permission_classes = [StaffOrAdminPermission]

    def get_queryset(self):
        billing_item_id = self.kwargs["billing_item_id"]
        return BillingItemLog.objects.filter(billing_item_id=billing_item_id).order_by("-timestamp")


# ===========================================================================
# PAYMENT RECEIPTS
# ===========================================================================

class PaymentReceiptListCreateView(generics.ListCreateAPIView):
    serializer_class = PaymentReceiptSerializer
    permission_classes = [StaffOrAdminPermission]

    def get_queryset(self):
        bill_id = self.kwargs.get("bill_id")
        qs = PaymentReceipt.objects.select_related("student_bill", "created_by")
        if bill_id:
            return qs.filter(student_bill_id=bill_id)
        return qs

    def perform_create(self, serializer):
        receipt = serializer.save()

        # Bill PDF is already queued by model logic.
        # Here we only queue receipt-PDF + notifications.
        generate_payment_receipt_pdf_task.delay(receipt.pk)
        send_payment_receipt_email_task.delay(receipt.pk)
        send_payment_receipt_sms_task.delay(receipt.pk)

        logger.info(
            f"✅ Payment receipt {receipt.receipt_number} created – "
            f"receipt PDF + notifications queued."
        )


class PaymentReceiptDetailView(generics.RetrieveUpdateDestroyAPIView):
    queryset = PaymentReceipt.objects.all()
    serializer_class = PaymentReceiptSerializer
    permission_classes = [StaffOrAdminPermission]

    def perform_update(self, serializer):
        receipt = serializer.save()
        generate_payment_receipt_pdf_task.delay(receipt.pk)
        logger.info(
            f"✅ Payment receipt {receipt.receipt_number} updated – "
            f"receipt PDF queued. Bill recalculation handled by model logic."
        )

    def perform_destroy(self, instance):
        instance._current_user = self.request.user
        instance.delete(actor=self.request.user)
        logger.info(
            f"✅ Payment receipt {instance.receipt_number} deleted – "
            f"bill recalculation handled by model logic."
        )


# ===========================================================================
# STUDENTS
# ===========================================================================

class StudentListView(generics.ListAPIView):
    permission_classes = [StaffOrAdminPermission]

    def get_queryset(self):
        queryset = User.objects.filter(role="student")
        class_name = self.request.query_params.get("class_name")
        if class_name:
            queryset = queryset.filter(class_name__iexact=class_name)
        return queryset.order_by("first_name", "last_name")

    def get_serializer_class(self):
        class StudentSerializer(serializers.ModelSerializer):
            class Meta:
                model = User
                fields = ["id", "first_name", "last_name", "email", "class_name"]

        return StudentSerializer


# ===========================================================================
# STUDENT BILLS (ADMIN/STAFF + AUTHENTICATED STUDENTS)
# ===========================================================================

class StudentBillListView(generics.ListAPIView):
    permission_classes = [permissions.IsAuthenticated]

    def get_queryset(self):
        user = self.request.user
        qs = StudentBill.objects.select_related("student", "billing_template")
        if user.role == "student":
            return qs.filter(student=user, status="PUBLISHED")
        return qs

    def get_serializer_class(self):
        summary = self.request.query_params.get("summary", "false").lower() == "true"
        if summary:
            return StudentBillSummarySerializer
        return StudentBillSerializer


class StudentBillCreateView(generics.CreateAPIView):
    serializer_class = StudentBillCreateSerializer
    permission_classes = [StaffOrAdminPermission]

    def create(self, request, *args, **kwargs):
        try:
            response = super().create(request, *args, **kwargs)

            return Response(
                {
                    "success": True,
                    "message": "Bill created successfully. PDF is being generated in the background.",
                    "data": response.data,
                },
                status=status.HTTP_201_CREATED,
            )

        except ValidationError as e:
            error_detail = str(e.detail) if hasattr(e, "detail") else str(e)

            if "unique" in error_detail.lower() and (
                "student" in error_detail.lower() or "billing_template" in error_detail.lower()
            ):
                student_data = request.data.get("student")
                template_data = request.data.get("billing_template")

                try:
                    student = User.objects.get(id=student_data)
                    template = BillingTemplate.objects.get(id=template_data)
                    message = (
                        f"A bill already exists for {student.first_name} {student.last_name} "
                        f"for {template.class_name} - {template.get_term_display()} "
                        f"({template.academic_year})"
                    )
                except (User.DoesNotExist, BillingTemplate.DoesNotExist):
                    message = "A bill already exists for this student and billing template combination"

                return Response(
                    {"success": False, "message": message},
                    status=status.HTTP_400_BAD_REQUEST,
                )

            return Response(
                {"success": False, "message": "Please check your input data for errors"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        except Exception as e:
            error_msg = str(e).lower()
            if "unique" in error_msg:
                return Response(
                    {"success": False, "message": "A bill already exists for this student and term combination"},
                    status=status.HTTP_400_BAD_REQUEST,
                )
            elif "foreign key" in error_msg:
                return Response(
                    {"success": False, "message": "Invalid student or billing template selected"},
                    status=status.HTTP_400_BAD_REQUEST,
                )
            else:
                logger.error(f"StudentBillCreateView error: {e}")
                return Response(
                    {"success": False, "message": "Unable to create bill. Please try again or contact support."},
                    status=status.HTTP_400_BAD_REQUEST,
                )

    def perform_create(self, serializer):
        bill = serializer.save()

        # Keep this explicit so staff can preview immediately after creation.
        generate_bill_pdf_task.delay(bill.pk)
        logger.info(f"📄 Bill {bill.bill_number} created – bill PDF generation queued.")

        if bill.status == "PUBLISHED":
            send_bill_published_email_task.delay(bill.pk)
            send_bill_published_sms_task.delay(bill.pk)
            logger.info(f"📧 Bill {bill.bill_number} is PUBLISHED – notifications queued.")


class StudentBillDetailView(generics.RetrieveUpdateDestroyAPIView):
    queryset = StudentBill.objects.all()
    serializer_class = StudentBillSerializer
    permission_classes = [StaffOrAdminPermission]

    def get_object(self):
        obj = super().get_object()
        if obj.status in ["DRAFT", "SCHEDULED"] and obj.scheduled_date:
            if obj.scheduled_date <= timezone.now():
                obj.status = "PUBLISHED"
                obj._current_user = self.request.user
                obj.save(update_fields=["status"])

                # Bill PDF queue is handled by model logic.
                send_bill_published_email_task.delay(obj.pk)
                send_bill_published_sms_task.delay(obj.pk)
        return obj

    def perform_update(self, serializer):
        old_instance = StudentBill.objects.get(pk=serializer.instance.pk)
        old_status = old_instance.status

        bill = serializer.save()

        # No manual bill PDF queue here. Model handles visible changes.
        if old_status != "PUBLISHED" and bill.status == "PUBLISHED":
            send_bill_published_email_task.delay(bill.pk)
            send_bill_published_sms_task.delay(bill.pk)

        logger.info(
            f"📄 Bill {bill.bill_number} updated – "
            f"bill PDF queue handled by model logic."
        )

    @transaction.atomic
    def perform_destroy(self, instance):
        BillLog.objects.create(
            bill=instance,
            field_name="bill_status",
            old_value="EXISTS",
            new_value="DELETED",
            user_first_name=self.request.user.first_name,
            user_last_name=self.request.user.last_name,
            user_email=self.request.user.email,
        )

        student = instance.student
        generated_date = instance.generated_date

        instance.delete()

        # Recalculate every subsequent bill using the authoritative method.
        subsequent_bills = StudentBill.objects.filter(
            student=student,
            generated_date__gt=generated_date
        ).order_by("generated_date")

        for bill in subsequent_bills:
            bill.apply_financial_recalculation(
                actor=self.request.user,
                cascade_subsequent=False,
                queue_pdf=True,
            )

        logger.info(
            f"🗑️ Bill deleted. Recalculated {subsequent_bills.count()} subsequent bill(s)."
        )


# ===========================================================================
# PUBLISH SINGLE BILL
# ===========================================================================

class PublishBillView(APIView):
    permission_classes = [StaffOrAdminPermission]

    def post(self, request, bill_id):
        try:
            bill = StudentBill.objects.get(id=bill_id)

            if bill.status == "PUBLISHED":
                return Response(
                    {"success": False, "message": "Bill is already published"},
                    status=status.HTTP_400_BAD_REQUEST,
                )

            if bill.status not in ["DRAFT", "SCHEDULED"]:
                return Response(
                    {"success": False, "message": f"Cannot publish bill with status: {bill.status}"},
                    status=status.HTTP_400_BAD_REQUEST,
                )

            bill.status = "PUBLISHED"
            bill._current_user = request.user
            bill.save(update_fields=["status"])

            # Keep explicit queue on publish action.
            generate_bill_pdf_task.delay(bill.pk)
            send_bill_published_email_task.delay(bill.pk)
            send_bill_published_sms_task.delay(bill.pk)

            logger.info(f"✅ Bill {bill.bill_number} published – PDF regen + notifications queued.")

            return Response(
                {
                    "success": True,
                    "message": (
                        "Bill published successfully. "
                        "PDF is being regenerated and notifications are being sent in the background."
                    ),
                    "bill_number": bill.bill_number,
                    "status": bill.status,
                }
            )

        except StudentBill.DoesNotExist:
            return Response(
                {"success": False, "message": "Bill not found"},
                status=status.HTTP_404_NOT_FOUND,
            )
        except Exception as e:
            logger.error(f"Error publishing bill {bill_id}: {e}")
            return Response(
                {"success": False, "message": "Error publishing bill"},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )


# ===========================================================================
# BULK PUBLISH BILLS
# ===========================================================================

class BulkPublishBillsView(APIView):
    permission_classes = [StaffOrAdminPermission]

    def post(self, request):
        bill_ids = request.data.get("bill_ids", [])

        if not bill_ids:
            return Response(
                {"success": False, "message": "No bill IDs provided"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        published_bills = []
        failed_bills = []

        for bill_id in bill_ids:
            try:
                bill = StudentBill.objects.get(id=bill_id)

                if bill.status in ["DRAFT", "SCHEDULED"]:
                    bill.status = "PUBLISHED"
                    bill._current_user = request.user
                    bill.save(update_fields=["status"])

                    generate_bill_pdf_task.delay(bill.pk)
                    send_bill_published_email_task.delay(bill.pk)
                    send_bill_published_sms_task.delay(bill.pk)

                    published_bills.append({
                        "id": bill.id,
                        "bill_number": bill.bill_number,
                        "pdf_queued": True,
                        "notifications_queued": True,
                    })

                elif bill.status == "PUBLISHED":
                    failed_bills.append({
                        "id": bill.id,
                        "bill_number": bill.bill_number,
                        "reason": "Bill is already published",
                    })
                else:
                    failed_bills.append({
                        "id": bill.id,
                        "bill_number": bill.bill_number,
                        "reason": f"Cannot publish bill with status: {bill.status}",
                    })

            except StudentBill.DoesNotExist:
                failed_bills.append({"id": bill_id, "reason": "Bill not found"})
            except Exception as e:
                failed_bills.append({"id": bill_id, "reason": str(e)})

        return Response(
            {
                "success": True,
                "message": (
                    f"Successfully published {len(published_bills)} bills. "
                    "PDFs are being generated and notifications are being sent in the background."
                ),
                "published_bills": published_bills,
                "failed_bills": failed_bills,
            }
        )


# ===========================================================================
# STUDENT BILL LOGS
# ===========================================================================

class StudentBillLogListView(generics.ListAPIView):
    serializer_class = BillLogSerializer
    permission_classes = [StaffOrAdminPermission]

    def get_queryset(self):
        bill_id = self.kwargs["bill_id"]
        return BillLog.objects.filter(bill_id=bill_id).order_by("-timestamp")


# ===========================================================================
# BALANCE RE-CALCULATION OPERATIONS
# ===========================================================================

@api_view(["POST"])
@permission_classes([StaffOrAdminPermission])
def recalculate_student_balances(request):
    student_id = request.data.get("student_id")

    task = recalculate_student_balances_task.delay(student_id)

    logger.info(
        f"✅ Balance recalculation task queued "
        f"(task_id={task.id}, student_id={student_id or 'ALL'})"
    )

    return Response(
        {
            "message": "Balance recalculation has been queued and will complete in the background.",
            "task_id": task.id,
            "student_id": student_id or "all",
        }
    )


@api_view(["GET"])
@permission_classes([StaffOrAdminPermission])
def student_balance_summary(request, student_id):
    try:
        student = User.objects.get(id=student_id, role="student")
    except User.DoesNotExist:
        return Response({"error": "Student not found"}, status=404)

    bills = (
        StudentBill.objects
        .filter(student=student, status="PUBLISHED")
        .select_related("billing_template")
        .order_by("generated_date")
    )

    total_billed = bills.aggregate(Sum("total_amount_due"))["total_amount_due__sum"] or Decimal("0.00")
    total_paid = bills.aggregate(Sum("total_paid"))["total_paid__sum"] or Decimal("0.00")

    latest_bill = bills.last()
    current_outstanding = _decimal_str(latest_bill.balance_due) if latest_bill else "0.00"

    bills_data = []
    for bill in bills:
        bills_data.append({
            "id": bill.id,
            "bill_number": bill.bill_number,
            "class_term": f"{bill.billing_template.class_name} - {bill.billing_template.get_term_display()}",
            "academic_year": bill.billing_template.academic_year,
            "total_amount_due": _decimal_str(bill.total_amount_due),
            "total_paid": _decimal_str(bill.total_paid),
            "current_bill_balance": _decimal_str(bill.current_bill_balance),
            "previous_arrears": _decimal_str(bill.previous_arrears),
            "balance_due_at_time": _decimal_str(bill.balance_due),
            "payment_status": bill.payment_status,
            "due_date": bill.due_date,
            "is_overdue": bill.is_overdue,
            "pdf_url": request.build_absolute_uri(bill.pdf_file.url) if bill.pdf_file else None,
        })

    return Response(
        {
            "student": {
                "id": student.id,
                "name": f"{student.first_name} {student.last_name}",
                "current_class": student.class_name,
                "email": student.email,
            },
            "summary": {
                "total_bills": len(bills_data),
                "total_billed": _decimal_str(total_billed),
                "total_paid": _decimal_str(total_paid),
                "current_outstanding_balance": current_outstanding,
                "paid_bills": len([b for b in bills_data if b["payment_status"] == "paid"]),
                "overdue_bills": len([b for b in bills_data if b["is_overdue"]]),
            },
            "bills": bills_data,
        }
    )


# ===========================================================================
# STUDENT-ONLY BILL APIS
# ===========================================================================

class StudentMyBillsView(generics.ListAPIView):
    serializer_class = StudentBillSerializer
    permission_classes = [StudentOnlyPermission]

    def get_queryset(self):
        return (
            StudentBill.objects
            .filter(student=self.request.user, status="PUBLISHED")
            .select_related("billing_template")
            .order_by("-generated_date")
        )

    def list(self, request, *args, **kwargs):
        queryset = self.get_queryset()
        serializer = self.get_serializer(queryset, many=True)

        total_outstanding = "0.00"
        total_paid = Decimal("0.00")

        if serializer.data:
            total_outstanding = serializer.data[0].get("balance_due", "0.00")
            total_paid = sum(
                Decimal(str(bill.get("total_paid", "0.00")))
                for bill in serializer.data
            )

        return Response(
            {
                "student": f"{request.user.first_name} {request.user.last_name}",
                "current_class": request.user.class_name,
                "summary": {
                    "total_bills": len(serializer.data),
                    "total_outstanding_balance": total_outstanding,
                    "total_paid": _decimal_str(total_paid),
                    "paid_bills": len([b for b in serializer.data if b.get("payment_status") == "paid"]),
                    "overdue_bills": len([b for b in serializer.data if b.get("is_overdue", False)]),
                },
                "bills": serializer.data,
            }
        )


class StudentCurrentClassBillsView(generics.ListAPIView):
    serializer_class = StudentBillSerializer
    permission_classes = [StudentOnlyPermission]

    def get_queryset(self):
        return (
            StudentBill.objects
            .filter(
                student=self.request.user,
                billing_template__class_name=self.request.user.class_name,
                status="PUBLISHED",
            )
            .select_related("billing_template")
            .order_by("-generated_date")
        )


class StudentPreviousClassBillsView(generics.ListAPIView):
    serializer_class = StudentBillSerializer
    permission_classes = [StudentOnlyPermission]

    def get_queryset(self):
        return (
            StudentBill.objects
            .filter(student=self.request.user, status="PUBLISHED")
            .exclude(billing_template__class_name=self.request.user.class_name)
            .select_related("billing_template")
            .order_by("-generated_date")
        )


# ===========================================================================
# ADVANCED PAYMENT MANAGEMENT
# ===========================================================================

@api_view(["POST"])
@permission_classes([StaffOrAdminPermission])
@transaction.atomic
def bulk_payment_update(request):
    updates = request.data.get("updates", [])
    updated_receipts = []

    for update_data in updates:
        receipt_id = update_data.get("id")
        if not receipt_id:
            continue

        try:
            receipt = PaymentReceipt.objects.get(id=receipt_id)
            receipt._current_user = request.user

            for field, value in update_data.items():
                if field != "id" and hasattr(receipt, field):
                    setattr(receipt, field, value)

            receipt.save()
            updated_receipts.append(receipt.id)

            logger.info(f"✅ Payment receipt {receipt.receipt_number} updated.")

        except PaymentReceipt.DoesNotExist:
            continue

    return Response(
        {
            "message": (
                f"Successfully updated {len(updated_receipts)} payment receipts. "
                "Bill recalculation was handled automatically."
            ),
            "updated_receipts": updated_receipts,
        }
    )


# ===========================================================================
# PAYMENT RECEIPT REQUEST VIEWS
# ===========================================================================

class PaymentReceiptRequestListCreateView(generics.ListCreateAPIView):
    """
    GET  — Students see only their own requests.
           Staff/admin see all requests, with optional ?status= filter.
    POST — Students submit a new payment request.
    """
    permission_classes = [permissions.IsAuthenticated]

    def get_serializer_class(self):
        if self.request.method == "POST":
            return PaymentReceiptRequestCreateSerializer
        return PaymentReceiptRequestSerializer

    def get_queryset(self):
        user = self.request.user
        if user.role == "student":
            qs = PaymentReceiptRequest.objects.filter(submitted_by=user)
        else:
            qs = PaymentReceiptRequest.objects.select_related(
                "student_bill", "submitted_by", "reviewed_by", "generated_receipt"
            ).all()

        status_filter = self.request.query_params.get("status")
        if status_filter:
            qs = qs.filter(status=status_filter)

        return qs.order_by("-submitted_at")

    def create(self, request, *args, **kwargs):
        if request.user.role != "student":
            return Response(
                {"success": False, "message": "Only students can submit payment requests."},
                status=status.HTTP_403_FORBIDDEN,
            )

        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        receipt_request = serializer.save()

        return Response(
            {
                "success": True,
                "message": (
                    "Your payment request has been submitted and is pending review "
                    "by the school finance team."
                ),
                "data": PaymentReceiptRequestSerializer(
                    receipt_request, context={"request": request}
                ).data,
            },
            status=status.HTTP_201_CREATED,
        )


class PaymentReceiptRequestDetailView(generics.RetrieveAPIView):
    """
    GET — Retrieve a single receipt request.
          Students can only see their own; staff see all.
    """
    serializer_class = PaymentReceiptRequestSerializer
    permission_classes = [permissions.IsAuthenticated]

    def get_queryset(self):
        user = self.request.user
        if user.role == "student":
            return PaymentReceiptRequest.objects.filter(submitted_by=user)
        return PaymentReceiptRequest.objects.all()


class PaymentReceiptRequestReviewView(APIView):
    """
    PATCH — Staff/admin only. Change the status of a receipt request.

    When status → accepted:
        - A PaymentReceipt is auto-generated.
        - Bill totals are updated via model logic.
        - Bill PDF is queued via model logic.
        - Receipt PDF + notifications are queued here.

    When status changes FROM accepted → anything else:
        - The auto-generated PaymentReceipt is deleted.
        - Bill totals are recalculated via model logic.
        - Bill PDF is queued via model logic.
    """
    permission_classes = [StaffOrAdminPermission]

    def patch(self, request, pk):
        try:
            receipt_request = PaymentReceiptRequest.objects.get(pk=pk)
        except PaymentReceiptRequest.DoesNotExist:
            return Response(
                {"success": False, "message": "Payment request not found."},
                status=status.HTTP_404_NOT_FOUND,
            )

        serializer = PaymentReceiptRequestReviewSerializer(
            receipt_request,
            data=request.data,
            partial=True,
            context={"request": request},
        )

        if not serializer.is_valid():
            return Response(
                {"success": False, "errors": serializer.errors},
                status=status.HTTP_400_BAD_REQUEST,
            )

        was_accepted_before = receipt_request.status == PaymentReceiptRequest.STATUS_ACCEPTED

        try:
            with transaction.atomic():
                updated_request = serializer.save()
                updated_request.refresh_from_db()
        except Exception as e:
            logger.error(f"❌ Error reviewing receipt request {pk}: {e}")
            return Response(
                {"success": False, "message": "An error occurred while processing the request."},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )

        new_status = updated_request.status
        response_data = {
            "success": True,
            "status": new_status,
            "data": PaymentReceiptRequestSerializer(
                updated_request, context={"request": request}
            ).data,
        }

        if new_status == PaymentReceiptRequest.STATUS_ACCEPTED:
            receipt = updated_request.generated_receipt

            if receipt is None:
                logger.error(
                    f"❌ Receipt request {pk} was accepted but generated_receipt is still None "
                    f"after refresh_from_db()."
                )
                return Response(
                    {
                        "success": False,
                        "message": (
                            "Payment request was accepted but the receipt could not be confirmed. "
                            "Please check the billing records manually."
                        ),
                    },
                    status=status.HTTP_500_INTERNAL_SERVER_ERROR,
                )

            generate_payment_receipt_pdf_task.delay(receipt.pk)
            send_payment_receipt_email_task.delay(receipt.pk)
            send_payment_receipt_sms_task.delay(receipt.pk)

            response_data["message"] = (
                f"Payment request accepted. Receipt #{receipt.receipt_number} "
                f"has been generated and the bill has been updated."
            )
            logger.info(
                f"📧 Receipt PDF + notifications queued for auto-generated receipt "
                f"#{receipt.receipt_number} from request #{pk}"
            )

        elif was_accepted_before:
            response_data["message"] = (
                f"Payment request revoked. The associated receipt has been deleted "
                f"and the bill balance has been recalculated. "
                f"New status: '{new_status}'."
            )

        elif new_status == PaymentReceiptRequest.STATUS_REJECTED:
            response_data["message"] = "Payment request rejected."

        elif new_status == PaymentReceiptRequest.STATUS_UNDER_REVIEW:
            response_data["message"] = "Payment request marked as under review."

        else:
            response_data["message"] = f"Payment request status updated to '{new_status}'."

        return Response(response_data, status=status.HTTP_200_OK)


class PaymentReceiptRequestLogListView(generics.ListAPIView):
    """
    GET — Retrieve all audit log entries for a specific receipt request.
    Staff/admin only.
    """
    serializer_class = PaymentReceiptRequestLogSerializer
    permission_classes = [StaffOrAdminPermission]

    def get_queryset(self):
        request_id = self.kwargs["request_id"]
        return PaymentReceiptRequestLog.objects.filter(
            receipt_request_id=request_id
        ).order_by("-timestamp")