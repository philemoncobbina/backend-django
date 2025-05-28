from rest_framework import viewsets, status
from rest_framework.response import Response
from rest_framework.decorators import action
from .models import Reservation, ReservationLog
from .serializers import ReservationSerializer, ReservationLogSerializer
from datetime import time
from django.utils import timezone
from sib_api_v3_sdk import Configuration, ApiClient, SendSmtpEmail
from sib_api_v3_sdk.api.transactional_emails_api import TransactionalEmailsApi
from django.conf import settings
from sib_api_v3_sdk.rest import ApiException
from rest_framework.permissions import IsAuthenticated, AllowAny
from pathlib import Path
import os
from dotenv import load_dotenv



class ReservationViewSet(viewsets.ModelViewSet):
    queryset = Reservation.objects.all()
    serializer_class = ReservationSerializer

    def create(self, request):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        # Check if the department is already booked at the selected time
        booking_date = serializer.validated_data['booking_date']
        booking_time = serializer.validated_data['booking_time']

        # Ensure the booking time is within business hours and on a weekday
        if not self.is_within_business_hours(booking_date, booking_time):
            return Response({'detail': 'Booking must be on a weekday between 9 AM and 4 PM.'}, status=status.HTTP_400_BAD_REQUEST)

        # Save the reservation without conflict checks during creation
        reservation = serializer.save()

        # Send confirmation email
        self.send_confirmation_email(reservation)

        return Response({'detail': 'Reservation submitted successfully!', 'data': serializer.data}, status=status.HTTP_201_CREATED)

    def send_confirmation_email(self, reservation):
        configuration = Configuration()
        configuration.api_key['api-key'] = os.getenv('BREVO_API_KEY')
        
        api_instance = TransactionalEmailsApi(ApiClient(configuration))
        
        send_smtp_email = SendSmtpEmail(
            to=[{"email": reservation.email}],
            sender={"name": "Your Company", "email": settings.DEFAULT_FROM_EMAIL},
            subject="Your Reservation Confirmation",
            html_content=f"""
            <html>
            <body>
                <p>Dear {reservation.full_name},</p>
                <p>Your reservation for {reservation.booking_date} at {reservation.booking_time} has been received and is currently pending.</p>
                <p>Thank you for choosing our services!</p>
                <p>Best regards,<br>Your Company</p>
            </body>
            </html>
            """
        )

        try:
            api_response = api_instance.send_transac_email(send_smtp_email)
            print("Email sent successfully: %s\n" % api_response)
        except ApiException as e:
            print("Exception when sending email: %s\n" % e)

    def send_status_confirmation_email(self, reservation):
        """
        Send email notification when reservation status is changed to 'Confirmed'
        """
        configuration = Configuration()
        configuration.api_key['api-key'] = os.getenv('BREVO_API_KEY')
        
        api_instance = TransactionalEmailsApi(ApiClient(configuration))
        
        send_smtp_email = SendSmtpEmail(
            to=[{"email": reservation.email}],
            sender={"name": "Your Company", "email": settings.DEFAULT_FROM_EMAIL},
            subject="Your Reservation Has Been Confirmed",
            html_content=f"""
            <html>
            <body>
                <h2>Reservation Confirmation</h2>
                <p>Dear {reservation.full_name},</p>
                <p>We are pleased to inform you that your reservation has been <strong>confirmed</strong>.</p>
                
                <h3>Appointment Details:</h3>
                <ul>
                    <li><strong>Date:</strong> {reservation.booking_date.strftime('%A, %B %d, %Y')}</li>
                    <li><strong>Time:</strong> {reservation.booking_time.strftime('%I:%M %p')}</li>
                    <li><strong>Department:</strong> {reservation.department}</li>
                    
                    
                </ul>
                
                <p>Please arrive 10 minutes before your scheduled appointment time.</p>
                <p>If you need to cancel or reschedule, please contact us at least 24 hours in advance.</p>
                
                <p>Thank you for choosing our services!</p>
                <p>Best regards,<br>Your Company</p>
            </body>
            </html>
            """
        )

        try:
            api_response = api_instance.send_transac_email(send_smtp_email)
            print("Status confirmation email sent successfully: %s\n" % api_response)
        except ApiException as e:
            print("Exception when sending status confirmation email: %s\n" % e)

    def is_within_business_hours(self, booking_date, booking_time):
        """
        Validate that the reservation is on a weekday and within business hours (9 AM to 4 PM).
        """
        # Check if the booking is for a past date
        if booking_date < timezone.now().date():
            return False
        # Check if the booking time is within business hours
        if booking_time < time(9, 0) or booking_time >= time(16, 0):
            return False
        # Check if the booking date is a weekday (Monday to Friday)
        if booking_date.weekday() >= 5:  # 5 = Saturday, 6 = Sunday
            return False
        return True

    def get_permissions(self):
        if self.action in ['update', 'partial_update']:
            self.permission_classes = [IsAuthenticated]
        else:
            self.permission_classes = [AllowAny]
        return super().get_permissions()
    
    
    

    def update(self, request, pk=None):
        print("Starting update process")
        
        reservation = self.get_object()
        print(f"Fetched reservation: {reservation}")

        original_data = ReservationSerializer(reservation).data  # Capture original data before update
        print(f"Original data: {original_data}")

        serializer = self.get_serializer(reservation, data=request.data, partial=False)
        print(f"Request data: {request.data}")
        
        serializer.is_valid(raise_exception=True)
        print("Data validation passed")

        booking_date = serializer.validated_data.get('booking_date', reservation.booking_date)
        booking_time = serializer.validated_data.get('booking_time', reservation.booking_time)
        department = serializer.validated_data.get('department', reservation.department)
        new_status = serializer.validated_data.get('status', reservation.status)

        print(f"Validated booking date: {booking_date}, booking time: {booking_time}, department: {department}, status: {new_status}")

        if not self.is_within_business_hours(booking_date, booking_time):
            print("Booking not within business hours or not a weekday")
            return Response({'detail': 'Booking updates must be on a weekday between 9 AM and 4 PM.'}, status=status.HTTP_400_BAD_REQUEST)

        if self.is_conflicting_reservation(booking_date, booking_time, department, reservation.id):
            print("Conflicting reservation found")
            return Response(
                {'detail': 'There is already a confirmed booking at this date and time for the same department.'},
                status=status.HTTP_409_CONFLICT
            )

        print("No conflicts found, proceeding with update")
        reservation.last_modified_by = request.user
        updated_reservation = serializer.save()
        print(f"Reservation updated: {serializer.data}")

        # Track changes
        updated_data = serializer.data
        changed_fields = self.get_changed_fields(original_data, updated_data)
        print(f"Changed fields: {changed_fields}")

        ReservationLog.objects.create(
            reservation=reservation,
            user=request.user,
            user_email=request.user.email,
            changed_fields=changed_fields
        )
        print("ReservationLog created")
        
        # Check if status was changed to 'Confirmed' and send confirmation email
        if new_status == 'Confirmed' and original_data.get('status') != 'Confirmed':
            print("Status changed to Confirmed, sending confirmation email")
            self.send_status_confirmation_email(updated_reservation)
        
        print("Update process completed successfully")
        return Response(serializer.data)

    def partial_update(self, request, pk=None):
        print("Starting partial update process")
        
        reservation = self.get_object()
        print(f"Fetched reservation: {reservation}")

        original_data = ReservationSerializer(reservation).data  # Capture original data before update
        print(f"Original data: {original_data}")

        serializer = self.get_serializer(reservation, data=request.data, partial=True)
        print(f"Request data: {request.data}")
        
        serializer.is_valid(raise_exception=True)
        print("Data validation passed")

        booking_date = serializer.validated_data.get('booking_date', reservation.booking_date)
        booking_time = serializer.validated_data.get('booking_time', reservation.booking_time)
        department = serializer.validated_data.get('department', reservation.department)
        new_status = serializer.validated_data.get('status', reservation.status)

        print(f"Validated booking date: {booking_date}, booking time: {booking_time}, department: {department}, status: {new_status}")

        if not self.is_within_business_hours(booking_date, booking_time):
            print("Booking not within business hours or not a weekday")
            return Response({'detail': 'Booking updates must be on a weekday between 9 AM and 4 PM.'}, status=status.HTTP_400_BAD_REQUEST)

        if self.is_conflicting_reservation(booking_date, booking_time, department, reservation.id):
            print("Conflicting reservation found")
            return Response(
                {'detail': 'There is already a confirmed booking at this date and time for the same department.'},
                status=status.HTTP_409_CONFLICT
            )

        print("No conflicts found, proceeding with partial update")
        reservation.last_modified_by = request.user
        updated_reservation = serializer.save()
        print(f"Reservation partially updated: {serializer.data}")

        # Track changes
        updated_data = serializer.data
        changed_fields = self.get_changed_fields(original_data, updated_data)
        print(f"Changed fields: {changed_fields}")

        ReservationLog.objects.create(
            reservation=reservation,
            user=request.user,
            user_email=request.user.email,
            changed_fields=changed_fields
        )
        print("ReservationLog created")
        # Example log output inside the update/partial_update method:
        print(f"ReservationLog created for {request.user.email}")

        # Check if status was changed to 'Confirmed' and send confirmation email
        if new_status == 'Confirmed' and original_data.get('status') != 'Confirmed':
            print("Status changed to Confirmed, sending confirmation email")
            self.send_status_confirmation_email(updated_reservation)

        print("Partial update process completed successfully")
        return Response(serializer.data)

    def get_changed_fields(self, original_data, updated_data):
        """
        Compare original and updated data and return a list of fields that changed.
        """
        print(f"Comparing original and updated data")
        changed_fields = []
        for key, original_value in original_data.items():
            updated_value = updated_data.get(key)
            if original_value != updated_value:
                changed_fields.append(f"{key}: {original_value} -> {updated_value}")
                print(f"Field changed: {key} from {original_value} to {updated_value}")
        return ', '.join(changed_fields)  # Return a string representation of the changes

    def is_conflicting_reservation(self, booking_date, booking_time, department, current_reservation_id=None):
        """
        Check if there is a conflicting confirmed reservation with the same date, time, and department.
        Exclude the current reservation being updated.
        """
        return Reservation.objects.filter(
            booking_date=booking_date,
            booking_time=booking_time,
            department=department,
            status='Confirmed'
        ).exclude(id=current_reservation_id).exists()

from rest_framework import generics

class ReservationLogListView(generics.ListAPIView):
    serializer_class = ReservationLogSerializer

    def get_queryset(self):
        reservation_id = self.kwargs['reservation_id']
        return ReservationLog.objects.filter(reservation_id=reservation_id)