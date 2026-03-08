"""
ratelimit_middleware.py  [UPDATED — covers both authapp and admin_auth]
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
Endpoint                                    Method      Limit        Key
-------------------------------------------+----------+-----------+----
--- authapp (public portal) ---
verify-email/                               GET         10/h         IP
signup/                                     POST        5/h          IP
login/                                      POST        5/m          IP
google-signin/                              POST        10/m         IP
password-reset-confirm/                     POST        5/15m        IP
verify-reset-code/                          POST        10/15m       IP
change-password-request/                    POST        5/h          IP
change-password/                            POST        5/15m        IP
verify-change-password-code/               POST        10/15m       IP
password-reset/                             GET+POST    5/h          IP

--- admin_auth (staff portal) ---
signup-auth/                                POST        10/h         IP
login-auth/                                 POST        5/m          IP

Adjust the path fragments below if your URL conf differs.
"""

import logging
from django_ratelimit.core import is_ratelimited

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Rules: (path_fragment, methods_tuple, rate, group_name)
# More-specific fragments must come BEFORE generic ones.
# First matching rule wins — loop stops there.
# ---------------------------------------------------------------------------
RATE_LIMIT_RULES = [
    # ── admin_auth endpoints (more specific — checked first) ────────────────
    ('signup-auth',                         ('POST',),           '10/h',  'admin_signup'),
    ('login-auth',                          ('POST',),           '5/m',   'admin_login'),

    # ── authapp endpoints ───────────────────────────────────────────────────
    ('verify-email',                        ('GET',),            '10/h',  'verify_email'),
    ('signup',                              ('POST',),           '5/h',   'signup'),
    ('login',                               ('POST',),           '5/m',   'login'),
    ('google-signin',                       ('POST',),           '10/m',  'google_signin'),
    ('password-reset-confirm',              ('POST',),           '5/15m', 'pw_reset_confirm'),
    ('verify-reset-code',                   ('POST',),           '10/15m','verify_reset_code'),
    ('change-password-request',             ('POST',),           '5/h',   'change_pw_req'),
    ('change-password',                     ('POST',),           '5/15m', 'change_pw'),
    ('verify-change-password-code',         ('POST',),           '10/15m','verify_change_pw'),
    # password-reset must come last — it's a prefix of more-specific variants above
    ('password-reset',                      ('GET', 'POST'),     '5/h',   'pw_reset'),
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