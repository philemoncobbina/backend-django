# __init__.py
default_app_config = 'booklist.apps.BooklistConfig'

# apps.py
from django.apps import AppConfig

class BooklistConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'booklist'
    
    def ready(self):
        import booklist.signals  # noqa