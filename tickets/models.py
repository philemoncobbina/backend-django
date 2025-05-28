from django.db import models
from authapp.models import CustomUser


class Ticket(models.Model):
    STATUS_CHOICES = [
        ('unattended', 'Unattended'),
        ('in_progress', 'In Progress'),
        ('resolved', 'Resolved'),
    ]

    SECTION_CHOICES = [
        ('authentication', 'Authentication'),
        ('reservation', 'Reservation Booking'),
        ('admissions', 'Admissions'),
        ('others', 'Others'),
    ]

    SEVERITY_CHOICES = [
        ('low', 'Low'),
        ('medium', 'Medium'),
        ('high', 'High'),
        ('critical', 'Critical'),
    ]

    TicketID = models.CharField(max_length=10, unique=True, editable=False)  
    full_name = models.CharField(max_length=255)
    email = models.EmailField()
    phone_number = models.CharField(max_length=15)
    section = models.CharField(max_length=50, choices=SECTION_CHOICES)
    severity = models.CharField(max_length=50, choices=SEVERITY_CHOICES)
    description = models.TextField()
    screenshot = models.ImageField(upload_to='screenshots/', blank=True, null=True)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='unattended')
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"{self.full_name} - {self.section} - {self.TicketID}"


class TicketLog(models.Model):
    ticket = models.ForeignKey(Ticket, on_delete=models.CASCADE)
    user = models.ForeignKey(CustomUser, on_delete=models.SET_NULL, null=True)
    user_email = models.EmailField(max_length=255, blank=True)
    changed_fields = models.TextField()
    timestamp = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        user_email = self.user_email if self.user_email else 'Unknown user'
        return f"Log for {self.ticket} by {user_email} at {self.timestamp}"