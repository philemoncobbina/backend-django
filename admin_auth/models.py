# admin_auth/models.py
from authapp.models import CustomUser

class AdminCustomUser(CustomUser):
    class Meta:
        proxy = True  # This makes it a proxy model
        verbose_name = 'Admin Custom User'
        verbose_name_plural = 'Admin Custom Users'
