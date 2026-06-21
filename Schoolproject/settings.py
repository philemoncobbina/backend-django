"""
Django Settings — Schoolproject
================================
Environment : Development (DEBUG=True)
Django docs : https://docs.djangoproject.com/en/stable/ref/settings/
"""

# =============================================================================
# Imports
# =============================================================================

import os
from datetime import timedelta
from pathlib import Path

from dotenv import load_dotenv

# =============================================================================
# Paths
# =============================================================================

BASE_DIR = Path(__file__).resolve().parent.parent

# =============================================================================
# Environment Variables
# =============================================================================

_ENV_PATH = BASE_DIR / ".env"

if _ENV_PATH.exists():
    _ENCODINGS = ("utf-8", "utf-16", "utf-16le", "utf-16be", "latin1")
    for _enc in _ENCODINGS:
        try:
            load_dotenv(_ENV_PATH, encoding=_enc)
            break
        except UnicodeDecodeError:
            continue

# Core
SECRET_KEY: str = os.getenv("SECRET_KEY", "")

# Email / SMS services
BREVO_API_KEY: str = os.getenv("BREVO_API_KEY", "")
DEFAULT_FROM_EMAIL: str = os.getenv("DEFAULT_FROM_EMAIL", "")
BREVO_SMS_SENDER: str = "SchoolFees"

# Geolocation
IPINFO_API_KEY: str = os.getenv("IPINFO_API_KEY", "")

# Twilio (supports both naming conventions for backwards compatibility)
TWILIO_ACCOUNT_SID: str = os.getenv("TWILIO_ACCOUNT_SID") or os.getenv("account_sid", "")
TWILIO_AUTH_TOKEN: str  = os.getenv("TWILIO_AUTH_TOKEN") or os.getenv("auth_token", "")
TWILIO_PHONE_NUMBER: str = os.getenv("TWILIO_PHONE_NUMBER", "")

# Legacy aliases kept for compatibility with older app code
account_sid = TWILIO_ACCOUNT_SID
auth_token  = TWILIO_AUTH_TOKEN

# Validate that every required variable is present at startup
_REQUIRED_ENV_VARS: dict[str, str] = {
    "SECRET_KEY":         SECRET_KEY,
    "BREVO_API_KEY":      BREVO_API_KEY,
    "DEFAULT_FROM_EMAIL": DEFAULT_FROM_EMAIL,
    "IPINFO_API_KEY":     IPINFO_API_KEY,
}
_missing = [key for key, value in _REQUIRED_ENV_VARS.items() if not value]
if _missing:
    raise ValueError(
        f"The following required environment variables are not set: {', '.join(_missing)}"
    )

# =============================================================================
# Core
# =============================================================================

DEBUG = os.getenv("DEBUG", "False") == "True"


WSGI_APPLICATION = "Schoolproject.wsgi.application"
ROOT_URLCONF      = "Schoolproject.urls"
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"
SITE_ID = 1

# =============================================================================
# Hosts & Origins
# =============================================================================

ALLOWED_HOSTS: list[str] = [
    "localhost",
    "127.0.0.1",
    "13.60.29.130",
    "api.cobbina.uk",
    "cobbina.uk",
    ".cobbina.uk",  # Wildcard for all subdomains
    "backend-django-5-clix.onrender.com",
]

# Also update these to include both HTTP and HTTPS
CORS_ALLOWED_ORIGINS: list[str] = [
    "http://localhost:4200",
    "http://localhost:5173",
    "http://localhost:5174",
    "http://13.60.29.130:8000",
    "https://api.cobbina.uk",  # Add HTTPS
    "http://api.cobbina.uk",
    "https://cobbina.uk",  # Add HTTPS
    "http://cobbina.uk",
]

CSRF_TRUSTED_ORIGINS: list[str] = [
    "http://localhost:5174",
    "http://13.60.29.130:8000",
    "https://api.cobbina.uk",  # Add HTTPS
    "http://api.cobbina.uk",
    "https://cobbina.uk",  # Add HTTPS
    "http://cobbina.uk",
]



CORS_ORIGIN_ALLOW_ALL = True
CORS_ALLOW_CREDENTIALS = True

# =============================================================================
# Application Definition
# =============================================================================

DJANGO_APPS: list[str] = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "django.contrib.sites",
]

THIRD_PARTY_APPS: list[str] = [
    # REST & Auth
    "rest_framework",
    "rest_framework.authtoken",
    "rest_framework_simplejwt",
    "rest_framework_social_oauth2",
    "corsheaders",
    # Allauth (social login)
    "allauth",
    "allauth.account",
    "allauth.socialaccount",
    "allauth.socialaccount.providers.google",
    # OAuth2
    "oauth2_provider",
    "social_django",
    # Filters
    "django_filters",
    # Static files
    "whitenoise.runserver_nostatic",
    # Celery
    "django_celery_beat",
    "django_celery_results",
]

LOCAL_APPS: list[str] = [
    "authapp.apps.AuthappConfig",
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

MIDDLEWARE: list[str] = [
    "django.middleware.security.SecurityMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "corsheaders.middleware.CorsMiddleware",
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
        "DIRS": [os.path.join(BASE_DIR, "authapp")],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.debug",
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    },
]

# =============================================================================
# Database
# =============================================================================
import dj_database_url

DATABASES: dict = {
    "default": dj_database_url.config(
        default=f"sqlite:///{BASE_DIR / 'db.sqlite3'}",
        conn_max_age=600,
    )
}

# =============================================================================
# Authentication & Authorisation
# =============================================================================

AUTH_USER_MODEL = "authapp.CustomUser"

AUTHENTICATION_BACKENDS: tuple[str, ...] = (
    "django.contrib.auth.backends.ModelBackend",
)

AUTH_PASSWORD_VALIDATORS: list[dict] = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

# =============================================================================
# Django REST Framework
# =============================================================================

REST_FRAMEWORK: dict = {
    "DEFAULT_AUTHENTICATION_CLASSES": [
        "rest_framework.authentication.BasicAuthentication",
        "rest_framework.authentication.SessionAuthentication",
        "rest_framework_simplejwt.authentication.JWTAuthentication",
        "rest_framework.authentication.TokenAuthentication",
    ],
}

# =============================================================================
# Simple JWT
# =============================================================================

SIMPLE_JWT: dict = {
    "ACCESS_TOKEN_LIFETIME":  timedelta(days=1),
    "REFRESH_TOKEN_LIFETIME": timedelta(days=1),
    "ALGORITHM":    "HS256",
    "SIGNING_KEY":  SECRET_KEY,
    "AUTH_HEADER_TYPES": ("Bearer",),
}

# =============================================================================
# Allauth / Social Auth
# =============================================================================

ACCOUNT_EMAIL_VERIFICATION = "mandatory"
ACCOUNT_SIGNUP_FIELDS = ["email*", "username*", "password1*", "password2*"]

SOCIALACCOUNT_PROVIDERS: dict = {
    "google": {
        "SCOPE": ["profile", "email"],
        "AUTH_PARAMS": {"access_type": "online"},
        "OAUTH_PKCE_ENABLED": True,
    }
}

# =============================================================================
# Sessions
# =============================================================================

SESSION_COOKIE_AGE = 25_200          # 7 hours (in seconds)
SESSION_EXPIRE_AT_BROWSER_CLOSE = True
SESSION_SAVE_EVERY_REQUEST = True

# =============================================================================
# Internationalisation
# =============================================================================

LANGUAGE_CODE = "en-us"
TIME_ZONE     = "UTC"
USE_I18N = True
USE_TZ   = True

# =============================================================================
# Static & Media Files
# =============================================================================

STATIC_URL  = "static/"
STATIC_ROOT = BASE_DIR / "staticfiles"

MEDIA_URL  = "/media/"
MEDIA_ROOT = BASE_DIR / "media"

STORAGES: dict = {
    "default": {
        "BACKEND": "django.core.files.storage.FileSystemStorage",
    },
    "staticfiles": {
        "BACKEND": "whitenoise.storage.CompressedManifestStaticFilesStorage",
    },
}

# =============================================================================
# Report Card Settings
# =============================================================================

REPORT_CARD_SETTINGS: dict = {
    "STORAGE_PATH":     "report_cards/",
    "SCHOOL_NAME":      "RIDOANA COMPREHENSIVE SCHOOL",
    "SCHOOL_ADDRESS":   "BT 247 TEMA",
    "SCHOOL_PHONE":     "+233 24 123 4567",
    "SCHOOL_EMAIL":     "philemoncobbina19@gmail.com",
    "SCHOOL_LOGO_PATH": (
        "https://img.freepik.com/free-vector/"
        "gradient-high-school-logo-design_24-9626932.jpg"
    ),
}

# =============================================================================
# Celery
# =============================================================================

CELERY_BROKER_URL    = "redis://localhost:6379/0"
CELERY_RESULT_BACKEND = "redis://localhost:6379/0"

CELERY_ACCEPT_CONTENT    = ["json"]
CELERY_TASK_SERIALIZER   = "json"
CELERY_RESULT_SERIALIZER = "json"
CELERY_TIMEZONE          = "UTC"

CELERY_BEAT_SCHEDULER = "django_celery_beat.schedulers:DatabaseScheduler"

# =============================================================================
# Celery Beat Schedule
# =============================================================================

# Periodic task: auto-publish scheduled results every 60 seconds
CELERY_BEAT_SCHEDULE = {
    'auto-publish-scheduled-results': {
        'task': 'ResultEntry.tasks.auto_publish_scheduled_results',  
        'schedule': 60.0,
    },
}

# =============================================================================
# Logging
# =============================================================================

LOGGING: dict = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "verbose": {
            "format": "{levelname} {asctime} {module} {process:d} {thread:d} {message}",
            "style": "{",
        },
        "simple": {
            "format": "{levelname} {asctime} {module} {message}",
            "style": "{",
        },
    },
    "handlers": {
        "file": {
            "level":     "DEBUG",
            "class":     "logging.FileHandler",
            "filename":  BASE_DIR / "debug.log",
            "formatter": "verbose",
        },
        "console": {
            "level":     "INFO",
            "class":     "logging.StreamHandler",
            "formatter": "simple",
        },
    },
    "loggers": {
        "": {
            "handlers":  ["console", "file"],
            "level":     "DEBUG",
            "propagate": True,
        },
        "django": {
            "handlers":  ["console", "file"],
            "level":     "INFO",
            "propagate": False,
        },
        "authapp": {
            "handlers":  ["console", "file"],
            "level":     "DEBUG",
            "propagate": False,
        },
    },
}