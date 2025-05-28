from django.db import models
from authapp.models import CustomUser

class Contact(models.Model):
    STATUS_CHOICES = [
        ('unattended', 'Unattended'),
        ('in_progress', 'In Progress'),
        ('resolved', 'Resolved'),
    ]

    firstName = models.CharField(max_length=255, default='')
    lastName = models.CharField(max_length=255, default='')
    email = models.EmailField(default='')
    phoneNumber  = models.CharField(max_length=255, default='')
    message = models.TextField(default='')
    timestamp = models.DateTimeField(auto_now_add=True)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='unattended')
    action_taken = models.TextField(default='', blank=True)

    def __str__(self):
        return f"{self.first_name} {self.last_name} - {self.email}"

    class Meta:
        verbose_name = "Contact"
        verbose_name_plural = "Contacts"
        ordering = ['-timestamp']


class ContactLog(models.Model):
    contact = models.ForeignKey('Contact', on_delete=models.CASCADE)
    user = models.ForeignKey(CustomUser, on_delete=models.SET_NULL, null=True)
    user_email = models.EmailField(max_length=255, blank=True)
    changed_fields = models.TextField()  # Store the fields that were changed
    timestamp = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"Log for {self.contact.email} by {self.user_email} at {self.timestamp}"
