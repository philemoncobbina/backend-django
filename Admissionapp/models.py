from django.db import models
from django.conf import settings
from django.utils import timezone
from authapp.models import CustomUser

class Admission(models.Model):
    STATUS_CHOICES = [
        ('pending', 'Pending'),
        ('in_review', 'In Review'),
        ('approved', 'Approved'),
        ('rejected', 'Rejected'),
    ]

    user = models.ForeignKey(CustomUser, on_delete=models.SET_NULL, null=True)
    user_email = models.EmailField(max_length=255, blank=True)
    submit_date = models.DateTimeField(default=timezone.now)
    admission_number = models.CharField(max_length=10, unique=True)

    # Form 1: Name & Contact Information
    first_name = models.CharField(max_length=255)
    last_name = models.CharField(max_length=255)
    middle_name = models.CharField(max_length=255, blank=True, null=True)
    home_address = models.CharField(max_length=255)
    age = models.IntegerField()
    language_spoken = models.CharField(max_length=255)
    country_of_citizenship = models.CharField(max_length=255)
    gender = models.CharField(max_length=10)
    date_of_birth = models.DateField()

    # Form 2: Parent / Guardian Information
    parent_full_name = models.CharField(max_length=255)
    occupation = models.CharField(max_length=255)
    phone_number = models.CharField(max_length=15)
    email = models.EmailField()
    parent_home_address = models.CharField(max_length=255)

    # Form 3: Education History
    previous_school_name = models.CharField(max_length=255)
    previous_class = models.CharField(max_length=10)
    previous_school_address = models.CharField(max_length=255)
    start_date = models.DateField()
    end_date = models.DateField()

    # Form 4: Health Information
    emergency_contact = models.CharField(max_length=255)
    emergency_contact_number = models.CharField(max_length=15)
    medical_conditions = models.CharField(max_length=255)
    allergies = models.CharField(max_length=255)
    disabilities = models.CharField(max_length=3, choices=[("Yes", "Yes"), ("No", "No")])
    vaccinated = models.CharField(max_length=3, choices=[("Yes", "Yes"), ("No", "No")])

    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='pending')

    def __str__(self):
        return f"{self.first_name} {self.last_name} - {self.email}"

    class Meta:
        verbose_name = "Admission"
        verbose_name_plural = "Admissions"
        ordering = ['-submit_date']

    


class AdmissionLog(models.Model):
    admission = models.ForeignKey(Admission, on_delete=models.CASCADE)
    user = models.ForeignKey(CustomUser, on_delete=models.SET_NULL, null=True)
    user_email = models.EmailField(max_length=255, blank=True)
    changed_fields = models.TextField()  # Store the fields that were changed
    timestamp = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"Log for {self.admission.email} by {self.user_email} at {self.timestamp}"
