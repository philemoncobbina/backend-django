"""
================================================================================
Django Settings — Schoolproject
================================================================================
Docs : https://docs.djangoproject.com/en/stable/ref/settings/
================================================================================
"""

# =============================================================================
# Imports
# =============================================================================
import os
from datetime import timedelta
from pathlib import Path

import dj_database_url
from dotenv import load_dotenv

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


def get_bool(key: str, default: bool = False) -> bool:
    return get_env(key, str(default)).strip().lower() in ("1", "true", "yes", "on")


def get_list(key: str, default: str = "") -> list:
    return [v.strip() for v in get_env(key, default).split(",") if v.strip()]


# Core
SECRET_KEY = get_env("SECRET_KEY", required=True)
DEBUG = get_bool("DEBUG", False)

# Email / SMS
BREVO_API_KEY = get_env("BREVO_API_KEY", required=True)
DEFAULT_FROM_EMAIL = get_env("DEFAULT_FROM_EMAIL", required=True)
BREVO_SMS_SENDER = "SchoolFees"

TWILIO_ACCOUNT_SID = get_env("TWILIO_ACCOUNT_SID")
TWILIO_AUTH_TOKEN = get_env("TWILIO_AUTH_TOKEN")
TWILIO_PHONE_NUMBER = get_env("TWILIO_PHONE_NUMBER")

# Geo
IPINFO_API_KEY = get_env("IPINFO_API_KEY", required=True)

# Google sign-in: comma-separated OAuth client IDs. If empty, Google sign-in refuses every login.
GOOGLE_OAUTH_CLIENT_IDS = get_list("GOOGLE_OAUTH_CLIENT_IDS")

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

# Cookies carry the credentials now, so they must never travel over plain HTTP in production.
SESSION_COOKIE_SECURE = not DEBUG
CSRF_COOKIE_SECURE = not DEBUG
CSRF_COOKIE_SAMESITE = "Lax"
# The CSRF token reaches the frontends through /csrf-auth/ (JSON body), so JavaScript never
# needs to read the cookie.
CSRF_COOKIE_HTTPONLY = True
X_FRAME_OPTIONS = "DENY"

if not DEBUG:
    # Nginx must send: proxy_set_header X-Forwarded-Proto $scheme;
    SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
    # Start low. Raise to 31536000 (one year) after a few weeks without problems.
    SECURE_HSTS_SECONDS = int(get_env("SECURE_HSTS_SECONDS", "3600"))

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
    "whitenoise.runserver_nostatic",  # must come before django.contrib.staticfiles
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
    "rest_framework.authtoken",  # keep installed for its tables; no longer used for auth
    "rest_framework_simplejwt",
    "rest_framework_simplejwt.token_blacklist",  # NEW: needed for refresh rotation and revoking sessions
    "rest_framework_social_oauth2",
    "corsheaders",
    "allauth",
    "allauth.account",
    "allauth.socialaccount",
    "allauth.socialaccount.providers.google",
    "oauth2_provider",
    "social_django",
    "django_filters",
    "django_celery_beat",
    "django_celery_results",
]
# allauth, oauth2_provider, social_django and rest_framework_social_oauth2 overlap with the
# custom Google sign-in. Once you confirm no URL or code uses them, remove them one at a time.

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
    "attendance",
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
    "corsheaders.middleware.CorsMiddleware",  # first, so CORS headers reach every response
    "django.middleware.security.SecurityMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "authapp.ratelimit_middleware.RateLimitMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "allauth.account.middleware.AccountMiddleware",  # must come after Authentication and Messages
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
        conn_health_checks=True,
    )
}

# =============================================================================
# Cache (Redis)
# =============================================================================
# Also stores django-ratelimit counters, so it must be shared by every Gunicorn worker.
# Uses Redis DB 1; Celery uses DB 0 below, so cache data and queued tasks never mix.
CACHE_URL = get_env("CACHE_URL")

if CACHE_URL or not DEBUG:
    CACHES = {
        "default": {
            "BACKEND": "django.core.cache.backends.redis.RedisCache",
            "LOCATION": CACHE_URL or "redis://localhost:6379/1",
            "KEY_PREFIX": "campus",
        }
    }
else:
    # Local development without Redis running: every rate-limited view would otherwise crash.
    CACHES = {"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}}

# =============================================================================
# Authentication
# =============================================================================
AUTH_USER_MODEL = "authapp.CustomUser"

AUTHENTICATION_BACKENDS = ("django.contrib.auth.backends.ModelBackend",)

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

# --- Cookie auth -------------------------------------------------------------
AUTH_COOKIE_ACCESS = "access_token"
AUTH_COOKIE_REFRESH = "refresh_token"
AUTH_COOKIE_SECURE = not DEBUG          # set DEBUG=True in your local .env
AUTH_COOKIE_SAMESITE = "Lax"
# AUTH_COOKIE_DOMAIN is left unset: host-only cookies, never shared with other subdomains.

# Which frontend a request came from decides the cookie names (admin_access_token, ...), so
# logging into one portal can't overwrite another portal's session in the same browser.
# The public website is not listed and uses the default names.
AUTH_PORTAL_BY_ORIGIN = {
    "https://admin.cobbina.uk": "admin",
    "https://student.cobbina.uk": "student",
    # Local dev: adjust the ports to match which frontend runs where.
    "http://localhost:5173": "admin",
    "http://localhost:5174": "student",
}

# While any frontend still reads tokens from the JSON body, keep this True. Once all three
# use cookies, set AUTH_RETURN_TOKENS_IN_BODY=False in .env: tokens leave the response body
# and the JWT lifetimes below get much shorter.
AUTH_RETURN_TOKENS_IN_BODY = get_bool("AUTH_RETURN_TOKENS_IN_BODY", True)

# =============================================================================
# Django REST Framework
# =============================================================================
REST_FRAMEWORK = {
    "DEFAULT_AUTHENTICATION_CLASSES": (
        "authapp.authentication.CookieJWTAuthentication",
    ),
    "DEFAULT_PERMISSION_CLASSES": (
        "rest_framework.permissions.IsAuthenticated",
    ),
}


if not DEBUG:
    REST_FRAMEWORK["DEFAULT_RENDERER_CLASSES"] = ["rest_framework.renderers.JSONRenderer"]

# =============================================================================
# JWT
# =============================================================================
SIMPLE_JWT = {
    "ACCESS_TOKEN_LIFETIME": (
        timedelta(hours=8) if AUTH_RETURN_TOKENS_IN_BODY else timedelta(minutes=15)
    ),
    "REFRESH_TOKEN_LIFETIME": (
        timedelta(days=1) if AUTH_RETURN_TOKENS_IN_BODY else timedelta(hours=1)
    ),
    "ALGORITHM": "HS256",
    "SIGNING_KEY": SECRET_KEY,
    "ROTATE_REFRESH_TOKENS": True,
    "BLACKLIST_AFTER_ROTATION": True,
    "UPDATE_LAST_LOGIN": False,
    "AUTH_HEADER_TYPES": ("Bearer",),
}

# Lifetimes of the emailed verification links (used by the Celery tasks)
EMAIL_VERIFICATION_TOKEN_MINUTES = int(get_env("EMAIL_VERIFICATION_TOKEN_MINUTES", "60"))
ADMIN_EMAIL_VERIFICATION_HOURS = 48
STUDENT_EMAIL_VERIFICATION_HOURS = 168
ADMIN_EMAIL_SENDER_NAME = "RIDOANA COMPREHENSIVE SCHOOL"

# Student creation
STUDENT_BATCH_MAX = 50
STUDENT_ENFORCE_PASSWORD_POLICY = True

# Where emailed links point. Override in .env if your frontends live elsewhere.
BACKEND_BASE_URL = get_env("BACKEND_BASE_URL", "http://127.0.0.1:8000")
FRONTEND_LOGIN_URL = get_env("FRONTEND_LOGIN_URL", "https://cobbina.uk/login")
FRONTEND_DASHBOARD_URL = get_env("FRONTEND_DASHBOARD_URL", "https://admin.cobbina.uk/dashboard")
STUDENT_PORTAL_LOGIN_URL = get_env("STUDENT_PORTAL_LOGIN_URL", "https://student.cobbina.uk/login")

# =============================================================================
# Sessions (only the Django admin site uses these now)
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
    "staticfiles": {"BACKEND": "whitenoise.storage.CompressedManifestStaticFilesStorage"},
}

# =============================================================================
# Celery
# =============================================================================
CELERY_BROKER_URL = get_env("CELERY_BROKER_URL", "redis://localhost:6379/0")
CELERY_RESULT_BACKEND = get_env("CELERY_RESULT_BACKEND", "redis://localhost:6379/0")
CELERY_BROKER_CONNECTION_RETRY_ON_STARTUP = True

# Local development without Redis: run tasks immediately instead of queueing them.
# (This sends real emails if Brevo keys are set.) Leave False in production.
CELERY_TASK_ALWAYS_EAGER = get_bool("CELERY_TASK_ALWAYS_EAGER", False)

CELERY_ACCEPT_CONTENT = ["json"]
CELERY_TASK_SERIALIZER = "json"
CELERY_RESULT_SERIALIZER = "json"
CELERY_TIMEZONE = "UTC"
CELERY_BEAT_SCHEDULER = "django_celery_beat.schedulers:DatabaseScheduler"

# Sized for a small server
CELERY_TASK_SOFT_TIME_LIMIT = 240
CELERY_TASK_TIME_LIMIT = 300
CELERY_WORKER_PREFETCH_MULTIPLIER = 1
CELERY_WORKER_MAX_TASKS_PER_CHILD = 200
CELERY_RESULT_EXPIRES = 3600

CELERY_BEAT_SCHEDULE = {
    "auto-publish-results": {
        # Check this name: the app is "ResultsEntry" but this says "ResultEntry".
        # See the note below for how to confirm it.
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
# Rate Limiting (django-ratelimit)
# =============================================================================
# Production: Nginx must send   proxy_set_header X-Real-IP $remote_addr;
# X-Forwarded-For is not used: clients can fake it to dodge the limits.
# If Nginx doesn't send X-Real-IP, every visitor falls back to 127.0.0.1 and shares ONE limit.
RATELIMIT_IP_META_KEY = (
    "REMOTE_ADDR" if DEBUG else get_env("RATELIMIT_IP_META_KEY", "HTTP_X_REAL_IP")
)

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
        "console": {"class": "logging.StreamHandler", "formatter": "verbose"},
        "file": {
            "class": "logging.handlers.RotatingFileHandler",
            "filename": BASE_DIR / "debug.log",
            "maxBytes": 5 * 1024 * 1024,  # the old file grew without limit on a 10 GB disk
            "backupCount": 3,
            "formatter": "verbose",
        },
    },
    "root": {
        "handlers": ["console", "file"],
        "level": get_env("LOG_LEVEL", "INFO"),
    },
}