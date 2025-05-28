from django.apps import AppConfig

class AdminAuthConfig(AppConfig):
    name = 'admin_auth'

    def ready(self):
        import admin_auth.permissions 
