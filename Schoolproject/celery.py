import os
from celery import Celery

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'Schoolproject.settings')

app = Celery('Schoolproject')
app.config_from_object('django.conf:settings', namespace='CELERY')
app.autodiscover_tasks()  # This auto-discovers tasks from all INSTALLED_APPS