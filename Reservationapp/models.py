from django.db import models
from django.utils import timezone
import datetime
from django.core.exceptions import ValidationError
from django.utils.dateparse import parse_datetime
from authapp.models import CustomUser

class Reservation(models.Model):
    STATUS_CHOICES = [
        ('Pending', 'Pending'),
        ('Confirmed', 'Confirmed'),
        ('Cancelled', 'Cancelled'),
    ]

    DEPARTMENTS = [
        ('Finance Department', 'Finance Department'),
        ('Admissions Department', 'Admissions Department'),
        ('Student Affairs', 'Student Affairs'),
        ('Human Resource Department', 'Human Resource Department'),
        ('Academics Department', 'Academics Department'),
    ]

    full_name = models.CharField(max_length=100)
    email = models.EmailField()
    phone = models.CharField(max_length=20)
    booking_date = models.DateField()
    booking_time = models.TimeField()
    department = models.CharField(max_length=50, choices=DEPARTMENTS)
    message = models.TextField(blank=True, null=True)
    status = models.CharField(max_length=10, choices=STATUS_CHOICES, default='Pending')
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def clean(self):
        """
        Custom validation logic to ensure the booking date and time are valid.
        """
        # Attempt to parse booking_date if it's in string format with timezone
        if isinstance(self.booking_date, str):
            parsed_datetime = parse_datetime(self.booking_date)
            if parsed_datetime:
                self.booking_date = parsed_datetime.date()
            else:
                raise ValidationError("Invalid date format provided.")

        # Ensure booking date is not in the past
        if self.booking_date < timezone.now().date():
            raise ValidationError("Booking date cannot be in the past.")

        # Ensure booking time is within business hours: 9 AM to 4 PM
        if self.booking_time < datetime.time(9, 0) or self.booking_time > datetime.time(16, 0):
            raise ValidationError("Booking time must be between 9 AM and 4 PM.")

    def save(self, *args, **kwargs):
        self.clean()  # Call custom validation
        super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.full_name} - {self.booking_date} at {self.booking_time}"

    class Meta:
        verbose_name = "Reservation"
        verbose_name_plural = "Reservations"
        ordering = ['-created_at']



class ReservationLog(models.Model):
    reservation = models.ForeignKey('Reservation', on_delete=models.CASCADE)
    user = models.ForeignKey(CustomUser, on_delete=models.SET_NULL, null=True)
    user_email = models.EmailField(max_length=255, blank=True)  # Track the email separately
    changed_fields = models.TextField()  # Store the fields that were changed
    timestamp = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        # Display the user's email if it exists, otherwise show 'Unknown user'
        user_email = self.user_email if self.user_email else 'Unknown user'
        log_str = f"Log for {self.reservation} by {user_email} at {self.timestamp}"

        # Print the details to check for any errors or debugging purposes
        print(f"ReservationLog: {log_str}")

        return log_str
