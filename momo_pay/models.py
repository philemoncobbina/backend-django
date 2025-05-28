from django.db import models

class Payment(models.Model):
    amount = models.DecimalField(max_digits=10, decimal_places=2)
    transaction_id = models.CharField(max_length=100, unique=True)
    status = models.CharField(max_length=20)
    created_at = models.DateTimeField(auto_now_add=True)
