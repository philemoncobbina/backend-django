"""
================================================================================
Django Settings — Schoolproject
================================================================================
Environment : Development / Production ready
Docs        : https://docs.djangoproject.com/en/stable/ref/settings/
================================================================================
"""

# =============================================================================
# Imports
# =============================================================================
import os
from pathlib import Path
from datetime import timedelta
from dotenv import load_dotenv
import dj_database_url

# =============================================================================
# Base Directory
# =============================================================================
BASE_DIR = Path(__file__).resolve().parent.parent

# =============================================================================
# Environment Variables
# =============================================================================
ENV_FILE = BASE_DIR / ".env"

if ENV_FILE.exists():
    for enc in ("utf-8", "utf-16", "utf-16le", "utf-16be", "latin1"):
        try:
            load_dotenv(ENV_FILE, encoding=enc)
            break
        except UnicodeDecodeError:
            continue

def get_env(key: str, default: str = "", required: bool = False):
    value = os.getenv(key, default)
    if required and not value:
        raise ValueError(f"Missing required environment variable: {key}")
    return value

# Core
SECRET_KEY = get_env("SECRET_KEY", required=True)
DEBUG = get_env("DEBUG", "False") == "True"

# Email / SMS
BREVO_API_KEY = get_env("BREVO_API_KEY", required=True)
DEFAULT_FROM_EMAIL = get_env("DEFAULT_FROM_EMAIL", required=True)
BREVO_SMS_SENDER = "SchoolFees"

TWILIO_ACCOUNT_SID = get_env("TWILIO_ACCOUNT_SID")
TWILIO_AUTH_TOKEN  = get_env("TWILIO_AUTH_TOKEN")
TWILIO_PHONE_NUMBER = get_env("TWILIO_PHONE_NUMBER")

# Geo
IPINFO_API_KEY = get_env("IPINFO_API_KEY", required=True)

# =============================================================================
# Core Django Settings
# =============================================================================
ROOT_URLCONF = "Schoolproject.urls"
WSGI_APPLICATION = "Schoolproject.wsgi.application"
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"
SITE_ID = 1

# =============================================================================
# Hosts & Security
# =============================================================================
ALLOWED_HOSTS = [
    "api.cobbina.uk",
    "localhost",
    "127.0.0.1",
]

CSRF_TRUSTED_ORIGINS = [
    "https://api.cobbina.uk",
    "https://cobbina.uk",
    "https://www.cobbina.uk",
    "https://student.cobbina.uk",
    "https://admin.cobbina.uk",
    "http://localhost:5173",
    "http://localhost:5174",
    "http://localhost:5175",
]

# =============================================================================
# CORS Configuration
# =============================================================================
CORS_ALLOWED_ORIGINS = [
    "https://cobbina.uk",
    "https://www.cobbina.uk",
    "https://student.cobbina.uk",
    "https://admin.cobbina.uk",
    "http://localhost:5173",
    "http://localhost:5174",
    "http://localhost:5175",
    "http://localhost:4200",
]

CORS_ALLOW_CREDENTIALS = True

# =============================================================================
# Applications
# =============================================================================
DJANGO_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "django.contrib.sites",
]

THIRD_PARTY_APPS = [
    "rest_framework",
    "rest_framework.authtoken",
    "rest_framework_simplejwt",
    "rest_framework_social_oauth2",
    "corsheaders",
    "allauth",
    "allauth.account",
    "allauth.socialaccount",
    "allauth.socialaccount.providers.google",
    "oauth2_provider",
    "social_django",
    "django_filters",
    "whitenoise.runserver_nostatic",
    "django_celery_beat",
    "django_celery_results",
]

LOCAL_APPS = [
    "authapp",
    "admin_auth",
    "student_auth",
    "Schoolapp",
    "Admissionapp",
    "ResultsEntry",
    "jobapplication",
    "jobposting",
    "Subscriptions",
    "Reservationapp",
    "student_billing",
    "booklist",
    "blogs",
    "tickets",
]

INSTALLED_APPS = DJANGO_APPS + THIRD_PARTY_APPS + LOCAL_APPS

# =============================================================================
# Middleware
# =============================================================================
MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "corsheaders.middleware.CorsMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "authapp.ratelimit_middleware.RateLimitMiddleware",
    "allauth.account.middleware.AccountMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

# =============================================================================
# Templates
# =============================================================================
TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / "authapp"],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.debug",
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    }
]

# =============================================================================
# Database
# =============================================================================
DATABASES = {
    "default": dj_database_url.config(
        default=f"sqlite:///{BASE_DIR / 'db.sqlite3'}",
        conn_max_age=600,
    )
}

# =============================================================================
# Authentication
# =============================================================================
AUTH_USER_MODEL = "authapp.CustomUser"

AUTHENTICATION_BACKENDS = (
    "django.contrib.auth.backends.ModelBackend",
)

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

# =============================================================================
# Django REST Framework
# =============================================================================
REST_FRAMEWORK = {
    "DEFAULT_AUTHENTICATION_CLASSES": [
        "rest_framework.authentication.SessionAuthentication",
        "rest_framework.authentication.BasicAuthentication",
        "rest_framework.authentication.TokenAuthentication",
        "rest_framework_simplejwt.authentication.JWTAuthentication",
    ],
}

# =============================================================================
# JWT
# =============================================================================
SIMPLE_JWT = {
    "ACCESS_TOKEN_LIFETIME": timedelta(days=1),
    "REFRESH_TOKEN_LIFETIME": timedelta(days=1),
    "ALGORITHM": "HS256",
    "SIGNING_KEY": SECRET_KEY,
    "AUTH_HEADER_TYPES": ("Bearer",),
}

# =============================================================================
# Sessions
# =============================================================================
SESSION_COOKIE_AGE = 25200  # 7 hours
SESSION_EXPIRE_AT_BROWSER_CLOSE = True
SESSION_SAVE_EVERY_REQUEST = True

# =============================================================================
# Internationalization
# =============================================================================
LANGUAGE_CODE = "en-us"
TIME_ZONE = "UTC"
USE_I18N = True
USE_TZ = True

# =============================================================================
# Static & Media
# =============================================================================
STATIC_URL = "/static/"
STATIC_ROOT = BASE_DIR / "staticfiles"

MEDIA_URL = "/media/"
MEDIA_ROOT = BASE_DIR / "media"

STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {
        "BACKEND": "whitenoise.storage.CompressedManifestStaticFilesStorage"
    },
}

# =============================================================================
# Celery
# =============================================================================
CELERY_BROKER_URL = get_env(
    "CELERY_BROKER_URL", "redis://localhost:6379/0"
)
CELERY_RESULT_BACKEND = get_env(
    "CELERY_RESULT_BACKEND", "redis://localhost:6379/0"
)

CELERY_ACCEPT_CONTENT = ["json"]
CELERY_TASK_SERIALIZER = "json"
CELERY_RESULT_SERIALIZER = "json"
CELERY_TIMEZONE = "UTC"
CELERY_BEAT_SCHEDULER = "django_celery_beat.schedulers:DatabaseScheduler"

CELERY_BEAT_SCHEDULE = {
    "auto-publish-results": {
        "task": "ResultEntry.tasks.auto_publish_scheduled_results",
        "schedule": 60.0,
    }
}

# =============================================================================
# Reporting System
# =============================================================================
REPORT_CARD_SETTINGS = {
    "STORAGE_PATH": "report_cards/",
    "SCHOOL_NAME": "RIDOANA COMPREHENSIVE SCHOOL",
    "SCHOOL_ADDRESS": "BT 247 TEMA",
    "SCHOOL_PHONE": "+233 24 123 4567",
    "SCHOOL_EMAIL": DEFAULT_FROM_EMAIL,
    "SCHOOL_LOGO_PATH": (
        "https://img.freepik.com/free-vector/"
        "gradient-high-school-logo-design_24-9626932.jpg"
    ),
}

# =============================================================================
# Logging
# =============================================================================
LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "verbose": {
            "format": "{levelname} {asctime} {module} {message}",
            "style": "{",
        },
    },
    "handlers": {
        "console": {
            "class": "logging.StreamHandler",
        },
        "file": {
            "class": "logging.FileHandler",
            "filename": BASE_DIR / "debug.log",
        },
    },
    "root": {
        "handlers": ["console", "file"],
        "level": "INFO",
    },
}