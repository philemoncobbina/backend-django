"""
ratelimit_middleware.py
-----------------------
Place this file in your authapp (or any app's) directory.

This middleware applies per-endpoint rate limits by inspecting the URL path
and marking `request.limited = True` when the limit is exceeded.
The views then check `request.limited` and return a 429 response.

No infrastructure change is needed — django-ratelimit uses Django's cache
backend (works with LocMemCache in development and Redis/Memcached in prod).

INSTALL
-------
1.  pip install django-ratelimit

2.  Add to INSTALLED_APPS in settings.py (not strictly required but recommended):
        'django_ratelimit',

3.  Add this middleware to MIDDLEWARE in settings.py, BEFORE
    'django.middleware.common.CommonMiddleware':
        'authapp.ratelimit_middleware.RateLimitMiddleware',
    (replace 'authapp' with wherever you save this file)

4.  Update urls.py for the VerifyEmailView — remove user_id from the URL:
        OLD: path('verify-email/<int:user_id>/<str:token>/', VerifyEmailView.as_view(), name='verify-email'),
        NEW: path('verify-email/<str:token>/',               VerifyEmailView.as_view(), name='verify-email'),

RATE LIMITS APPLIED
-------------------
Endpoint                        Method   Limit          Key
-------------------------------+---------+--------------+------
/api/auth/signup/               POST     5  / hour       IP
/api/auth/login/                POST     5  / minute     IP
/api/auth/google-signin/        POST     10 / minute     IP
/api/auth/password-reset/       GET+POST 5  / hour       IP
/api/auth/password-reset-confirm/ POST   5  / 15 min     IP
/api/auth/verify-reset-code/    POST     10 / 15 min     IP
/api/auth/change-password-request/ POST  5  / hour       IP
/api/auth/change-password/      POST     5  / 15 min     IP
/api/auth/verify-change-password-code/ POST 10 / 15 min  IP
/api/auth/verify-email/         GET      10 / hour       IP

Adjust the paths below to match your actual URL conf if they differ.
"""

import logging
from django_ratelimit.core import is_ratelimited

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Map (path_fragment, method) → ratelimit rate string
# Use path fragments so this works regardless of URL prefix.
# ---------------------------------------------------------------------------
RATE_LIMIT_RULES = [
    # (path_fragment,                   methods,             rate,    group_suffix)
    ('verify-email',                    ('GET',),            '10/h',  'verify_email'),
    ('signup',                          ('POST',),           '5/h',   'signup'),
    ('login',                           ('POST',),           '5/m',   'login'),
    ('google-signin',                   ('POST',),           '10/m',  'google_signin'),
    ('password-reset-confirm',          ('POST',),           '5/15m', 'pw_reset_confirm'),
    ('verify-reset-code',               ('POST',),           '10/15m','verify_reset_code'),
    ('change-password-request',         ('POST',),           '5/h',   'change_pw_req'),
    ('change-password',                 ('POST',),           '5/15m', 'change_pw'),
    ('verify-change-password-code',     ('POST',),           '10/15m','verify_change_pw'),
    # password-reset must come AFTER the more-specific variants above
    ('password-reset',                  ('GET', 'POST'),     '5/h',   'pw_reset'),
]


class RateLimitMiddleware:
    """
    Lightweight middleware that sets request.limited = True when a rate limit
    is exceeded. The actual 429 response is returned by the view itself, which
    keeps the DRF response format consistent across all endpoints.
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        request.limited = False  # safe default

        path = request.path_info  # e.g. '/api/auth/login/'
        method = request.method.upper()

        for fragment, methods, rate, group in RATE_LIMIT_RULES:
            if fragment in path and method in methods:
                limited = is_ratelimited(
                    request=request,
                    group=f'authapp_{group}',
                    key='ip',
                    rate=rate,
                    method=method,
                    increment=True,
                )
                if limited:
                    logger.warning(
                        f"Rate limit exceeded | path={path} | method={method} "
                        f"| group={group} | ip={self._get_ip(request)}"
                    )
                    request.limited = True
                # Stop at the first matching rule
                break

        response = self.get_response(request)
        return response

    @staticmethod
    def _get_ip(request):
        x_forwarded_for = request.META.get('HTTP_X_FORWARDED_FOR')
        if x_forwarded_for:
            return x_forwarded_for.split(',')[0].strip()
        return request.META.get('REMOTE_ADDR', 'unknown')