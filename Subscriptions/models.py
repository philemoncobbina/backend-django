from django.db import models
from django.utils import timezone

class Subscription(models.Model):
    full_name = models.CharField(max_length=100)
    email = models.EmailField(unique=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return self.full_name

class EmailList(models.Model):
    emails = models.TextField()

    def __str__(self):
        return self.emails
