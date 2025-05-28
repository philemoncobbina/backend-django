from rest_framework import serializers
from .models import Reservation
from django.utils import timezone
import datetime  # Ensure datetime module is imported
from .models import ReservationLog
class ReservationSerializer(serializers.ModelSerializer):
    class Meta:
        model = Reservation
        fields = '__all__'

    def validate_booking_date(self, value):
        # Check if the date is in the past
        if value < timezone.now().date():
            raise serializers.ValidationError("Booking date cannot be in the past.")
        
        # Check if the date is a weekday (Monday to Friday)
        if value.weekday() > 4:  # 0 = Monday, 1 = Tuesday, ..., 4 = Friday, 5 = Saturday, 6 = Sunday
            raise serializers.ValidationError("Booking date must be a weekday (Monday to Friday).")
        
        return value

    def validate_booking_time(self, value):
        # Ensure booking time is within business hours: 9 AM to 4 PM
        if value < datetime.time(9, 0) or value > datetime.time(16, 0):
            raise serializers.ValidationError("Booking time must be between 9 AM and 4 PM.")
        
        return value

class ReservationLogSerializer(serializers.ModelSerializer):
    class Meta:
        model = ReservationLog
        # Specify the fields you want to serialize
        fields = '__all__'
        # Optionally, you can include related fields as well, e.g., user details or reservation details if needed
        # fields = '__all__'  # If you want to serialize all fields