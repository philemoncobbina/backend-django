from rest_framework import viewsets, status
from rest_framework.response import Response
from .models import Admission, AdmissionLog
from .serializers import AdmissionSerializer, AdmissionLogSerializer
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.decorators import action
from django.forms.models import model_to_dict
from django.db.models import Max
from sib_api_v3_sdk import Configuration, ApiClient, TransactionalEmailsApi, SendSmtpEmail
from sib_api_v3_sdk.rest import ApiException
import os
import threading
from django.template.loader import render_to_string
from django.conf import settings
import logging

logger = logging.getLogger(__name__)

class EmailThread(threading.Thread):
    def __init__(self, func, *args, **kwargs):
        self.func = func
        self.args = args
        self.kwargs = kwargs
        super().__init__()

    def run(self):
        try:
            self.func(*self.args, **self.kwargs)
        except Exception as e:
            logger.error(f"Error in email thread: {str(e)}")

class AdmissionViewSet(viewsets.ModelViewSet):
    serializer_class = AdmissionSerializer

    def get_queryset(self):
        return Admission.objects.all()

    def _get_api_instance(self):
        configuration = Configuration()
        configuration.api_key['api-key'] = os.getenv('BREVO_API_KEY')
        return TransactionalEmailsApi(ApiClient(configuration))

    def send_admission_confirmation_email(self, admission):
        api_instance = self._get_api_instance()
        
        html_content = render_to_string('email/admission_confirmation.html', {
            'admission_number': admission.admission_number
        })
        
        send_smtp_email = SendSmtpEmail(
            to=[{"email": admission.user_email}],
            sender={"name": "Admissions Office", "email": settings.DEFAULT_FROM_EMAIL},
            subject="Admission Application Received",
            html_content=html_content
        )

        try:
            api_response = api_instance.send_transac_email(send_smtp_email)
            logger.info(f"Confirmation email sent successfully for admission {admission.admission_number}")
        except ApiException as e:
            logger.error(f"Exception when sending confirmation email: {str(e)}")
            
    def send_approval_email(self, admission):
        api_instance = self._get_api_instance()
        
        html_content = render_to_string('email/admission_approval.html', {
            'admission_number': admission.admission_number
        })
        
        send_smtp_email = SendSmtpEmail(
            to=[{"email": admission.user_email}],
            sender={"name": "Admissions Office", "email": settings.DEFAULT_FROM_EMAIL},
            subject="Admission Application Approved",
            html_content=html_content
        )

        try:
            api_response = api_instance.send_transac_email(send_smtp_email)
            logger.info(f"Approval email sent successfully for admission {admission.admission_number}")
        except ApiException as e:
            logger.error(f"Exception when sending approval email: {str(e)}")

    @action(detail=False, methods=['get'], permission_classes=[IsAuthenticated])
    def my_admissions(self, request):
        """Fetch admissions for the authenticated user."""
        admissions = Admission.objects.filter(user=request.user)
        serializer = self.get_serializer(admissions, many=True)
        return Response(serializer.data)

    @action(detail=False, methods=['get'], permission_classes=[IsAuthenticated])
    def all_admissions(self, request):
        """Fetch all admissions for all users."""
        admissions = self.get_queryset()
        serializer = self.get_serializer(admissions, many=True)
        return Response(serializer.data)

    def create(self, request):
        try:
            serializer = self.get_serializer(data=request.data)
            serializer.is_valid(raise_exception=True)

            admission_number = self.generate_admission_number()
            
            admission = serializer.save(
                admission_number=admission_number,
                user=request.user,
                user_email=request.user.email if request.user.is_authenticated else None
            )

            # Start email thread
            EmailThread(
                self.send_admission_confirmation_email,
                admission
            ).start()
            
            return Response({
                'detail': 'Your application has been submitted successfully!',
                'data': serializer.data
            }, status=status.HTTP_201_CREATED)
            
        except Exception as e:
            logger.error(f"Error creating admission: {str(e)}")
            return Response(
                {'error': 'Failed to process your application. Please try again.'},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )

    def update(self, request, pk=None):
        try:
            admission = self.get_object()
            original_data = model_to_dict(admission)

            serializer = self.get_serializer(admission, data=request.data, partial=False)
            serializer.is_valid(raise_exception=True)
            updated_admission = serializer.save()

            # Check if status was changed to approved
            if 'status' in request.data and request.data['status'] == 'approved' and original_data['status'] != 'approved':
                EmailThread(
                    self.send_approval_email,
                    updated_admission
                ).start()

            # Log changes
            self.log_changes(original_data, updated_admission, request.user)

            return Response(serializer.data)
            
        except Exception as e:
            logger.error(f"Error updating admission: {str(e)}")
            return Response(
                {'error': 'Failed to update admission. Please try again.'},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )

    def partial_update(self, request, pk=None):
        try:
            admission = self.get_object()
            original_data = model_to_dict(admission)

            serializer = self.get_serializer(admission, data=request.data, partial=True)
            serializer.is_valid(raise_exception=True)
            updated_admission = serializer.save()

            # Check if status was changed to approved
            if 'status' in request.data and request.data['status'] == 'approved' and original_data['status'] != 'approved':
                EmailThread(
                    self.send_approval_email,
                    updated_admission
                ).start()

            # Log changes
            self.log_changes(original_data, updated_admission, request.user)

            return Response(serializer.data)
            
        except Exception as e:
            logger.error(f"Error updating admission: {str(e)}")
            return Response(
                {'error': 'Failed to update admission. Please try again.'},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )

    def generate_admission_number(self):
        try:
            last_admission = Admission.objects.aggregate(Max('admission_number'))
            last_number = last_admission['admission_number__max']

            if last_number:
                new_number = int(last_number[3:]) + 1
            else:
                new_number = 1

            return f"RCS{new_number:06d}"
        except Exception as e:
            logger.error(f"Error generating admission number: {str(e)}")
            raise

    def log_changes(self, original_data, updated_admission, user):
        updated_data = model_to_dict(updated_admission)
        changed_fields = self.get_changed_fields(original_data, updated_data)
        if changed_fields:
            AdmissionLog.objects.create(
                admission=updated_admission,
                user=user,
                user_email=user.email if user else "Anonymous",
                changed_fields=changed_fields
            )

    def get_changed_fields(self, original_data, updated_data):
        changed_fields = []
        for key, original_value in original_data.items():
            updated_value = updated_data.get(key)
            if original_value != updated_value:
                changed_fields.append(f"{key}: {original_value} -> {updated_value}")
        return ', '.join(changed_fields)

    def get_permissions(self):
        if self.action in ['update', 'partial_update']:
            self.permission_classes = [IsAuthenticated]
        else:
            self.permission_classes = [AllowAny]
        return super().get_permissions()


class AdmissionLogViewSet(viewsets.ReadOnlyModelViewSet):
    serializer_class = AdmissionLogSerializer

    def get_queryset(self):
        admission_id = self.kwargs['admission_id']
        return AdmissionLog.objects.filter(admission__id=admission_id)

    @action(detail=True, methods=['get'])
    def logs(self, request, pk=None):
        logs = self.get_queryset()
        serializer = self.get_serializer(logs, many=True)
        return Response(serializer.data)