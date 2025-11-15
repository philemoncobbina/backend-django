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
import jwt
import logging

# Email imports
from sib_api_v3_sdk import Configuration, ApiClient, SendSmtpEmail
from sib_api_v3_sdk.api.transactional_emails_api import TransactionalEmailsApi
from sib_api_v3_sdk.rest import ApiException
from django.conf import settings

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

class BillingEmailService:
    """Service class to handle billing-related email notifications"""
    
    @staticmethod
    def send_bill_published_email(student_bill):
        """
        Send email notification when a bill status changes to PUBLISHED
        Works for any transition: DRAFT->PUBLISHED, SCHEDULED->PUBLISHED, or direct PUBLISHED creation
        """
        try:
            student = student_bill.student
            billing_template = student_bill.billing_template
            
            # Build the email content
            email_body = f"""
            <html>
            <body style="font-family: Arial, sans-serif; color: #333; line-height: 1.6;">
                <div style="max-width: 600px; margin: 0 auto; padding: 20px; border: 1px solid #ddd; border-radius: 10px;">
                    <div style="text-align: center; background-color: #4CAF50; padding: 15px; border-radius: 10px 10px 0 0;">
                        <h2 style="color: white; margin: 0;">School Fee Bill Published</h2>
                    </div>
                    
                    <div style="padding: 20px;">
                        <p>Dear {student.first_name} {student.last_name},</p>
                        
                        <p>Your school fee bill for <strong>{billing_template.class_name} - {billing_template.get_term_display()} Term ({billing_template.academic_year})</strong> has been published and is now available for payment.</p>
                        
                        <div style="background-color: #f8f9fa; padding: 15px; border-radius: 5px; margin: 20px 0;">
                            <h3 style="color: #4CAF50; margin-top: 0;">Bill Details:</h3>
                            <table style="width: 100%; border-collapse: collapse;">
                                <tr>
                                    <td style="padding: 8px; border-bottom: 1px solid #ddd;"><strong>Bill Number:</strong></td>
                                    <td style="padding: 8px; border-bottom: 1px solid #ddd;">{student_bill.bill_number}</td>
                                </tr>
                                <tr>
                                    <td style="padding: 8px; border-bottom: 1px solid #ddd;"><strong>Class:</strong></td>
                                    <td style="padding: 8px; border-bottom: 1px solid #ddd;">{billing_template.class_name}</td>
                                </tr>
                                <tr>
                                    <td style="padding: 8px; border-bottom: 1px solid #ddd;"><strong>Term:</strong></td>
                                    <td style="padding: 8px; border-bottom: 1px solid #ddd;">{billing_template.get_term_display()}</td>
                                </tr>
                                <tr>
                                    <td style="padding: 8px; border-bottom: 1px solid #ddd;"><strong>Academic Year:</strong></td>
                                    <td style="padding: 8px; border-bottom: 1px solid #ddd;">{billing_template.academic_year}</td>
                                </tr>
                                <tr>
                                    <td style="padding: 8px; border-bottom: 1px solid #ddd;"><strong>Total Amount Due:</strong></td>
                                    <td style="padding: 8px; border-bottom: 1px solid #ddd;">GHS {student_bill.total_amount_due:,.2f}</td>
                                </tr>
                                <tr>
                                    <td style="padding: 8px; border-bottom: 1px solid #ddd;"><strong>Previous Arrears:</strong></td>
                                    <td style="padding: 8px; border-bottom: 1px solid #ddd;">GHS {student_bill.previous_arrears:,.2f}</td>
                                </tr>
                                <tr>
                                    <td style="padding: 8px; border-bottom: 1px solid #ddd;"><strong>Total Balance Due:</strong></td>
                                    <td style="padding: 8px; border-bottom: 1px solid #ddd; font-weight: bold; color: #e74c3c;">GHS {student_bill.balance_due:,.2f}</td>
                                </tr>
                                <tr>
                                    <td style="padding: 8px; border-bottom: 1px solid #ddd;"><strong>Due Date:</strong></td>
                                    <td style="padding: 8px; border-bottom: 1px solid #ddd;">{student_bill.due_date.strftime('%B %d, %Y')}</td>
                                </tr>
                            </table>
                        </div>
                        
                        <p>Please log in to your student portal to view the complete bill details and make payments.</p>
                        
                        <div style="text-align: center; margin: 25px 0;">
                            <a href="http://localhost:5173/student-dashboard/bills" 
                               style="display: inline-block; padding: 12px 25px; background-color: #4CAF50; color: white; 
                                      text-decoration: none; border-radius: 5px; font-weight: bold;">
                                View Your Bills
                            </a>
                        </div>
                        
                        <p><strong>Payment Methods Available:</strong></p>
                        <ul>
                            <li>Cash</li>
                            <li>Bank Transfer</li>
                            <li>Mobile Money</li>
                            <li>Cheque</li>
                        </ul>
                        
                        <p>If you have any questions or concerns regarding your bill, please contact the school administration.</p>
                        
                        <div style="margin-top: 30px; padding-top: 20px; border-top: 1px solid #ddd;">
                            <p style="margin: 0; color: #666;">Best regards,<br>
                            <strong>School Administration</strong><br>
                            Finance Department</p>
                        </div>
                    </div>
                </div>
            </body>
            </html>
            """
            
            # Send email using Brevo (Sendinblue)
            configuration = Configuration()
            configuration.api_key['api-key'] = settings.BREVO_API_KEY
            
            api_instance = TransactionalEmailsApi(ApiClient(configuration))
            
            send_smtp_email = SendSmtpEmail(
                to=[{"email": student.email, "name": f"{student.first_name} {student.last_name}"}],
                sender={"name": "School Finance Department", "email": settings.DEFAULT_FROM_EMAIL},
                subject=f"School Fee Bill Published - {student_bill.bill_number}",
                html_content=email_body
            )
            
            api_response = api_instance.send_transac_email(send_smtp_email)
            logger.info(f"Bill published email sent to {student.email} for bill {student_bill.bill_number}: {api_response}")
            return True
            
        except ApiException as e:
            logger.error(f"Exception when sending bill published email to {student.email}: {e}")
            return False
        except Exception as e:
            logger.error(f"Unexpected error when sending bill published email: {e}")
            return False
    
    @staticmethod
    def send_payment_receipt_email(payment_receipt):
        """
        Send email notification when a payment is made
        """
        try:
            student_bill = payment_receipt.student_bill
            student = student_bill.student
            
            email_body = f"""
            <html>
            <body style="font-family: Arial, sans-serif; color: #333; line-height: 1.6;">
                <div style="max-width: 600px; margin: 0 auto; padding: 20px; border: 1px solid #ddd; border-radius: 10px;">
                    <div style="text-align: center; background-color: #2196F3; padding: 15px; border-radius: 10px 10px 0 0;">
                        <h2 style="color: white; margin: 0;">Payment Receipt</h2>
                    </div>
                    
                    <div style="padding: 20px;">
                        <p>Dear {student.first_name} {student.last_name},</p>
                        
                        <p>Thank you for your payment. Here are your payment details:</p>
                        
                        <div style="background-color: #f8f9fa; padding: 15px; border-radius: 5px; margin: 20px 0;">
                            <h3 style="color: #2196F3; margin-top: 0;">Payment Details:</h3>
                            <table style="width: 100%; border-collapse: collapse;">
                                <tr>
                                    <td style="padding: 8px; border-bottom: 1px solid #ddd;"><strong>Receipt Number:</strong></td>
                                    <td style="padding: 8px; border-bottom: 1px solid #ddd;">{payment_receipt.receipt_number}</td>
                                </tr>
                                <tr>
                                    <td style="padding: 8px; border-bottom: 1px solid #ddd;"><strong>Amount Paid:</strong></td>
                                    <td style="padding: 8px; border-bottom: 1px solid #ddd; font-weight: bold; color: #27ae60;">GHS {payment_receipt.amount_paid:,.2f}</td>
                                </tr>
                                <tr>
                                    <td style="padding: 8px; border-bottom: 1px solid #ddd;"><strong>Payment Method:</strong></td>
                                    <td style="padding: 8px; border-bottom: 1px solid #ddd;">{payment_receipt.get_payment_method_display()}</td>
                                </tr>
                                <tr>
                                    <td style="padding: 8px; border-bottom: 1px solid #ddd;"><strong>Payment Date:</strong></td>
                                    <td style="padding: 8px; border-bottom: 1px solid #ddd;">{payment_receipt.payment_date.strftime('%B %d, %Y at %I:%M %p')}</td>
                                </tr>
                                <tr>
                                    <td style="padding: 8px; border-bottom: 1px solid #ddd;"><strong>Applied to Bill:</strong></td>
                                    <td style="padding: 8px; border-bottom: 1px solid #ddd;">{student_bill.bill_number}</td>
                                </tr>
                            </table>
                        </div>
                        
                        <div style="background-color: #e8f5e8; padding: 15px; border-radius: 5px; margin: 20px 0;">
                            <h3 style="color: #27ae60; margin-top: 0;">Current Balance:</h3>
                            <p style="font-size: 18px; font-weight: bold; text-align: center; color: #e74c3c;">
                                GHS {student_bill.balance_due:,.2f}
                            </p>
                        </div>
                        
                        <p>You can view your updated bill and payment history by logging into your student portal.</p>
                        
                        <div style="text-align: center; margin: 25px 0;">
                            <a href="http://localhost:5173/student-dashboard/bills" 
                               style="display: inline-block; padding: 12px 25px; background-color: #2196F3; color: white; 
                                      text-decoration: none; border-radius: 5px; font-weight: bold;">
                                View Your Bills
                            </a>
                        </div>
                        
                        <div style="margin-top: 30px; padding-top: 20px; border-top: 1px solid #ddd;">
                            <p style="margin: 0; color: #666;">Best regards,<br>
                            <strong>School Administration</strong><br>
                            Finance Department</p>
                        </div>
                    </div>
                </div>
            </body>
            </html>
            """
            
            # Send email using Brevo (Sendinblue)
            configuration = Configuration()
            configuration.api_key['api-key'] = settings.BREVO_API_KEY
            
            api_instance = TransactionalEmailsApi(ApiClient(configuration))
            
            send_smtp_email = SendSmtpEmail(
                to=[{"email": student.email, "name": f"{student.first_name} {student.last_name}"}],
                sender={"name": "School Finance Department", "email": settings.DEFAULT_FROM_EMAIL},
                subject=f"Payment Receipt - {payment_receipt.receipt_number}",
                html_content=email_body
            )
            
            api_response = api_instance.send_transac_email(send_smtp_email)
            logger.info(f"Payment receipt email sent to {student.email} for receipt {payment_receipt.receipt_number}: {api_response}")
            return True
            
        except ApiException as e:
            logger.error(f"Exception when sending payment receipt email to {student.email}: {e}")
            return False
        except Exception as e:
            logger.error(f"Unexpected error when sending payment receipt email: {e}")
            return False

def auto_publish_bills(queryset):
    """Auto-publish scheduled bills that are due and send email notifications"""
    now = timezone.now()
    bills_to_update = queryset.filter(
        status__in=['DRAFT', 'SCHEDULED'],
        scheduled_date__isnull=False,
        scheduled_date__lte=now
    )
    
    for bill in bills_to_update:
        old_status = bill.status
        bill.status = 'PUBLISHED'
        bill.save(update_fields=['status'])
        
        # Send email notification when auto-publishing (from DRAFT or SCHEDULED to PUBLISHED)
        BillingEmailService.send_bill_published_email(bill)
    
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
        # Create log for billing item deletion
        BillingItemLog.objects.create(
            billing_item=instance,
            field_name='billing_item_status',
            old_value='EXISTS',
            new_value='DELETED',
            user_first_name=self.request.user.first_name,
            user_last_name=self.request.user.last_name,
            user_email=self.request.user.email
        )
        
        # The post_delete signal will handle recalculation
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
        """Set user context when creating payment receipts and send email"""
        receipt = serializer.save()
        
        # Send payment receipt email
        BillingEmailService.send_payment_receipt_email(receipt)

class PaymentReceiptDetailView(generics.RetrieveUpdateDestroyAPIView):
    queryset = PaymentReceipt.objects.all()
    serializer_class = PaymentReceiptSerializer
    permission_classes = [permissions.IsAuthenticated]

    def perform_update(self, serializer):
        """Set user context when updating payment receipts for logging"""
        receipt = serializer.save()

    def perform_destroy(self, instance):
        """Set user context and log deletion before deleting payment receipt"""
        # Set user context for deletion logging
        instance._current_user = self.request.user
        
        # The post_delete signal will handle logging and recalculation
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
                
                return Response({
                    'success': False,
                    'message': message
                }, status=status.HTTP_400_BAD_REQUEST)
            
            return Response({
                'success': False,
                'message': 'Please check your input data for errors'
            }, status=status.HTTP_400_BAD_REQUEST)
            
        except Exception as e:
            error_msg = str(e).lower()
            
            if 'unique' in error_msg:
                return Response({
                    'success': False,
                    'message': 'A bill already exists for this student and term combination'
                }, status=status.HTTP_400_BAD_REQUEST)
            elif 'foreign key' in error_msg:
                return Response({
                    'success': False,
                    'message': 'Invalid student or billing template selected'
                }, status=status.HTTP_400_BAD_REQUEST)
            else:
                return Response({
                    'success': False,
                    'message': 'Unable to create bill. Please try again or contact support.'
                }, status=status.HTTP_400_BAD_REQUEST)

    def perform_create(self, serializer):
        bill = serializer.save()
        bill._current_user = self.request.user
        
        # Send email if bill is created directly as PUBLISHED
        if bill.status == 'PUBLISHED':
            BillingEmailService.send_bill_published_email(bill)

class StudentBillDetailView(generics.RetrieveUpdateDestroyAPIView):
    queryset = StudentBill.objects.all()
    serializer_class = StudentBillSerializer
    permission_classes = [permissions.IsAuthenticated]

    def get_object(self):
        obj = super().get_object()
        if obj.status in ['DRAFT', 'SCHEDULED'] and obj.scheduled_date:
            if obj.scheduled_date <= timezone.now():
                old_status = obj.status
                obj.status = 'PUBLISHED'
                obj._current_user = self.request.user
                obj.save(update_fields=['status'])
                
                # Send email notification when auto-publishing
                BillingEmailService.send_bill_published_email(obj)
        return obj

    def perform_update(self, serializer):
        """Set user context for logging when updating and handle email notifications"""
        old_instance = StudentBill.objects.get(pk=serializer.instance.pk)
        old_status = old_instance.status
        
        bill = serializer.save()
        
        # Send email if status changed to PUBLISHED (from any previous status)
        if old_status != 'PUBLISHED' and bill.status == 'PUBLISHED':
            BillingEmailService.send_bill_published_email(bill)

    @transaction.atomic
    def perform_destroy(self, instance):
        """Add logging before deleting and recalculate subsequent bills"""
        # Create log entry for deletion
        BillLog.objects.create(
            bill=instance,
            field_name='bill_status',
            old_value='EXISTS',
            new_value='DELETED',
            user_first_name=self.request.user.first_name,
            user_last_name=self.request.user.last_name,
            user_email=self.request.user.email
        )
        
        # Get student and generated_date before deletion for recalculation
        student = instance.student
        generated_date = instance.generated_date
        
        instance.delete()
        
        # Recalculate subsequent bills for this student
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
# Publish Bill View (Specific endpoint for publishing bills)
# --------------------
class PublishBillView(APIView):
    """API endpoint to manually publish a scheduled or draft bill"""
    permission_classes = [permissions.IsAuthenticated]
    
    def post(self, request, bill_id):
        try:
            bill = StudentBill.objects.get(id=bill_id)
            
            if bill.status == 'PUBLISHED':
                return Response({
                    'success': False,
                    'message': 'Bill is already published'
                }, status=status.HTTP_400_BAD_REQUEST)
            
            if bill.status not in ['DRAFT', 'SCHEDULED']:
                return Response({
                    'success': False,
                    'message': f'Cannot publish bill with status: {bill.status}'
                }, status=status.HTTP_400_BAD_REQUEST)
            
            old_status = bill.status
            bill.status = 'PUBLISHED'
            bill._current_user = request.user
            bill.save(update_fields=['status'])
            
            # Send email notification
            email_sent = BillingEmailService.send_bill_published_email(bill)
            
            return Response({
                'success': True,
                'message': f'Bill published successfully. Email notification {"sent" if email_sent else "failed to send"}.',
                'bill_number': bill.bill_number,
                'status': bill.status
            })
            
        except StudentBill.DoesNotExist:
            return Response({
                'success': False,
                'message': 'Bill not found'
            }, status=status.HTTP_404_NOT_FOUND)
        except Exception as e:
            logger.error(f"Error publishing bill {bill_id}: {e}")
            return Response({
                'success': False,
                'message': 'Error publishing bill'
            }, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

# --------------------
# Bulk Publish Bills View
# --------------------
class BulkPublishBillsView(APIView):
    """API endpoint to bulk publish multiple scheduled or draft bills"""
    permission_classes = [permissions.IsAuthenticated]
    
    def post(self, request):
        bill_ids = request.data.get('bill_ids', [])
        
        if not bill_ids:
            return Response({
                'success': False,
                'message': 'No bill IDs provided'
            }, status=status.HTTP_400_BAD_REQUEST)
        
        published_bills = []
        failed_bills = []
        
        for bill_id in bill_ids:
            try:
                bill = StudentBill.objects.get(id=bill_id)
                
                if bill.status in ['DRAFT', 'SCHEDULED']:
                    old_status = bill.status
                    bill.status = 'PUBLISHED'
                    bill._current_user = request.user
                    bill.save(update_fields=['status'])
                    
                    # Send email notification
                    email_sent = BillingEmailService.send_bill_published_email(bill)
                    
                    published_bills.append({
                        'id': bill.id,
                        'bill_number': bill.bill_number,
                        'email_sent': email_sent
                    })
                elif bill.status == 'PUBLISHED':
                    failed_bills.append({
                        'id': bill.id,
                        'bill_number': bill.bill_number,
                        'reason': 'Bill is already published'
                    })
                else:
                    failed_bills.append({
                        'id': bill.id,
                        'bill_number': bill.bill_number,
                        'reason': f'Cannot publish bill with status: {bill.status}'
                    })
                    
            except StudentBill.DoesNotExist:
                failed_bills.append({
                    'id': bill_id,
                    'reason': 'Bill not found'
                })
            except Exception as e:
                failed_bills.append({
                    'id': bill_id,
                    'reason': str(e)
                })
        
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
    """View logs for a specific bill - includes custom charge and payment receipt logs"""
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
    """
    Force recalculation of all student balances.
    Useful for fixing any inconsistencies.
    """
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
    
    return Response({
        'message': f'Successfully recalculated {updated_count} bills',
        'updated_count': updated_count
    })

@api_view(['GET'])
@permission_classes([permissions.IsAuthenticated])
def student_balance_summary(request, student_id):
    """
    Get comprehensive balance summary for a student
    """
    try:
        student = User.objects.get(id=student_id, role='student')
    except User.DoesNotExist:
        return Response({'error': 'Student not found'}, status=404)
    
    bills = StudentBill.objects.filter(
        student=student, 
        status='PUBLISHED'
    ).order_by('generated_date')
    
    # Calculate totals
    total_billed = bills.aggregate(Sum('total_amount_due'))['total_amount_due__sum'] or 0
    total_paid = bills.aggregate(Sum('total_paid'))['total_paid__sum'] or 0
    
    # Get current outstanding (most recent bill's balance_due)
    latest_bill = bills.last()
    current_outstanding = float(latest_bill.balance_due) if latest_bill else 0
    
    # Bills breakdown
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
            'is_overdue': bill.is_overdue
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
# Student-Only Bill APIs (Simplified)
# --------------------
class StudentOnlyPermission(permissions.BasePermission):
    """Custom permission to only allow students to access their own bills."""
    def has_permission(self, request, view):
        return request.user.is_authenticated and request.user.role == 'student'


class StudentMyBillsView(generics.ListAPIView):
    """
    Simplified API for students to view all their published bills.
    """
    serializer_class = StudentBillSerializer
    permission_classes = [StudentOnlyPermission]

    def get_queryset(self):
        """Get all published bills for the current student"""
        return StudentBill.objects.filter(
            student=self.request.user,
            status='PUBLISHED'
        ).order_by('-generated_date')

    def list(self, request, *args, **kwargs):
        """Return bills with comprehensive summary"""
        queryset = self.get_queryset()
        serializer = self.get_serializer(queryset, many=True)
        
        # Calculate totals
        total_outstanding = 0
        total_paid = 0
        current_balance = 0
        
        if serializer.data:
            # Use 'total_outstanding' instead of 'balance_due'
            # This field exists in StudentBillSerializer as a SerializerMethodField
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
    """
    API for students to view bills for their current class only.
    """
    serializer_class = StudentBillSerializer
    permission_classes = [StudentOnlyPermission]

    def get_queryset(self):
        """Get published bills for student's current class only"""
        return StudentBill.objects.filter(
            student=self.request.user,
            billing_template__class_name=self.request.user.class_name,
            status='PUBLISHED'
        ).order_by('-generated_date')


class StudentPreviousClassBillsView(generics.ListAPIView):
    """
    API for students to view bills from their previous classes.
    """
    serializer_class = StudentBillSerializer
    permission_classes = [StudentOnlyPermission]

    def get_queryset(self):
        """Get published bills excluding current class"""
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
    """
    Update multiple payment receipts at once.
    Useful for bulk payment processing.
    """
    updates = request.data.get('updates', [])
    updated_receipts = []
    
    for update_data in updates:
        receipt_id = update_data.get('id')
        if not receipt_id:
            continue
            
        try:
            receipt = PaymentReceipt.objects.get(id=receipt_id)
            receipt._current_user = request.user
            
            # Update fields
            for field, value in update_data.items():
                if field != 'id' and hasattr(receipt, field):
                    setattr(receipt, field, value)
            
            receipt.save()  # This will trigger cascading updates
            updated_receipts.append(receipt.id)
            
        except PaymentReceipt.DoesNotExist:
            continue
    
    return Response({
        'message': f'Successfully updated {len(updated_receipts)} payment receipts',
        'updated_receipts': updated_receipts
    })