"""
Caching utilities for the attendance application.

The database remains the source of truth. Cache failures are treated as
cache misses so that attendance functionality continues when Redis is
temporarily unavailable.
"""

import hashlib
import logging
import time
from datetime import date, datetime
from uuid import uuid4

from django.core.cache import cache
from django.utils.dateparse import parse_date


logger = logging.getLogger(__name__)


CACHE_SCHEMA_VERSION = "v2"

CACHE_TTL_SHORT = 60 * 5
CACHE_TTL_MEDIUM = 60 * 30
CACHE_TTL_LONG = 60 * 60 * 24

CLASS_CHOICES_CACHE_KEY = (
    f"attendance:{CACHE_SCHEMA_VERSION}:class_choices"
)


def _safe(value) -> str:
    """
    Produce a short, collision-resistant cache key component.

    Hashing avoids collisions such as:
    - Class 1
    - Class@1
    - Class/1
    """
    raw_value = str(value).encode("utf-8")
    return hashlib.sha256(raw_value).hexdigest()[:24]


def _normalize_date(value) -> str:
    """
    Convert date-like values to YYYY-MM-DD.

    Invalid values are hashed so they cannot produce unsafe cache keys.
    """
    if isinstance(value, datetime):
        return value.date().isoformat()

    if isinstance(value, date):
        return value.isoformat()

    if isinstance(value, str):
        parsed = parse_date(value)

        if parsed is not None:
            return parsed.isoformat()

        try:
            return datetime.strptime(
                value,
                "%Y-%m-%d",
            ).date().isoformat()
        except ValueError:
            return _safe(value)

    return _safe(value)


def safe_cache_get(key, default=None):
    try:
        return cache.get(key, default)
    except Exception:
        logger.warning(
            "Cache GET failed for key=%s. Treating it as a miss.",
            key,
            exc_info=True,
        )
        return default


def safe_cache_set(key, value, timeout):
    try:
        return cache.set(key, value, timeout)
    except Exception:
        logger.warning(
            "Cache SET failed for key=%s. Continuing without caching.",
            key,
            exc_info=True,
        )
        return False


def safe_cache_add(key, value, timeout):
    try:
        return cache.add(key, value, timeout)
    except Exception:
        logger.warning(
            "Cache ADD failed for key=%s. Failing open.",
            key,
            exc_info=True,
        )
        return True


def safe_cache_delete(key):
    try:
        return cache.delete(key)
    except Exception:
        logger.warning(
            "Cache DELETE failed for key=%s.",
            key,
            exc_info=True,
        )
        return False


def safe_cache_delete_many(keys):
    keys = list(keys)

    if not keys:
        return 0

    try:
        return cache.delete_many(keys)
    except Exception:
        logger.warning(
            "Cache DELETE_MANY failed for %s key(s).",
            len(keys),
            exc_info=True,
        )
        return 0


def _release_lock(lock_key, lock_token):
    """
    Best-effort owner-aware lock release.

    This is safer than blindly deleting the lock. A fully atomic
    compare-and-delete operation requires Redis-specific functionality,
    but this simple check ensures an expired lock is not normally deleted
    after another request has acquired it.
    """
    current_token = safe_cache_get(lock_key)

    if current_token == lock_token:
        safe_cache_delete(lock_key)


def get_or_compute_locked(
    cache_key,
    compute_fn,
    ttl,
    lock_ttl=30,
    retry_interval=0.05,
    max_wait=2.0,
):
    """
    Return a cached value or compute it with basic stampede protection.
    """
    value = safe_cache_get(cache_key)

    if value is not None:
        return value

    lock_key = f"{cache_key}:lock"
    lock_token = uuid4().hex

    acquired = safe_cache_add(
        lock_key,
        lock_token,
        lock_ttl,
    )

    if acquired:
        try:
            value = compute_fn()
            safe_cache_set(cache_key, value, ttl)
            return value
        finally:
            _release_lock(lock_key, lock_token)

    waited = 0.0

    while waited < max_wait:
        time.sleep(retry_interval)
        waited += retry_interval

        value = safe_cache_get(cache_key)

        if value is not None:
            return value

    # The application must remain usable even if the lock holder is slow.
    return compute_fn()


def term_total_school_days_key(term_id):
    return (
        f"attendance:{CACHE_SCHEMA_VERSION}:term:"
        f"{_safe(term_id)}:total_school_days"
    )


def term_holiday_dates_key(term_id):
    return (
        f"attendance:{CACHE_SCHEMA_VERSION}:term:"
        f"{_safe(term_id)}:holiday_dates"
    )


def term_calendar_key(term_id):
    return (
        f"attendance:{CACHE_SCHEMA_VERSION}:term:"
        f"{_safe(term_id)}:calendar"
    )


def roster_key(class_name, term_id, date_value):
    return (
        f"attendance:{CACHE_SCHEMA_VERSION}:roster:"
        f"{_safe(class_name)}:{_safe(term_id)}:"
        f"{_normalize_date(date_value)}"
    )


def summary_key(student_id, term_id):
    return (
        f"attendance:{CACHE_SCHEMA_VERSION}:summary:"
        f"{_safe(student_id)}:{_safe(term_id)}"
    )


def invalidate_term(term_id):
    if not term_id:
        return

    safe_cache_delete_many(
        [
            term_total_school_days_key(term_id),
            term_holiday_dates_key(term_id),
            term_calendar_key(term_id),
        ]
    )


def invalidate_holiday(term_id):
    invalidate_term(term_id)


def invalidate_roster(class_name, term_id, date_value):
    if not class_name or not term_id or not date_value:
        return

    safe_cache_delete(
        roster_key(
            class_name,
            term_id,
            date_value,
        )
    )


def invalidate_summary(student_id, term_id):
    if not student_id or not term_id:
        return

    safe_cache_delete(
        summary_key(
            student_id,
            term_id,
        )
    )


def invalidate_summaries(student_ids, term_id):
    if not term_id:
        return

    unique_student_ids = {
        student_id
        for student_id in student_ids
        if student_id
    }

    keys = [
        summary_key(student_id, term_id)
        for student_id in unique_student_ids
    ]

    safe_cache_delete_many(keys)


def invalidate_class_choices():
    safe_cache_delete(CLASS_CHOICES_CACHE_KEY)