from django.apps import AppConfig

class BookListConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'booklist'
    verbose_name = 'Book Lists Management'