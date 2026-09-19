"""
Caching helpers for the results app.

Uses Django's cache framework (Redis backend - shared with the attendance
app's CACHES setting). Same tiers and the same key-sanitization approach
as attendance/cache_utils.py, since results derives a lot of what it shows
from attendance and course-assignment data.

NOTE ON STALENESS (attendance_stats_key specifically):
This cache is invalidated whenever a Result is saved (see Result.save()
in models.py), but NOT when an attendance record changes over in the
attendance app - mark/finalize/reopen there don't currently know this
cache exists. That's an accepted trade-off given the 5-minute TTL, the
same trade-off already made for attendance's own roster/summary caches.
If you want zero staleness here too, the real fix is a Django signal
fired from attendance/cache_utils.py's invalidate_summary(s) that this
app listens for - happy to wire that up if it matters for your workflow.
"""
import re

from django.core.cache import cache

CACHE_TTL_SHORT = 60 * 5
CACHE_TTL_MEDIUM = 60 * 30

# Same rationale as attendance/cache_utils.py: Django validates cache keys
# against memcached's character rules even on a Redis backend, and values
# like "Class 1" contain spaces.
_UNSAFE_KEY_CHARS = re.compile(r"[^A-Za-z0-9_.-]")


def _safe(value) -> str:
    return _UNSAFE_KEY_CHARS.sub("_", str(value))


def attendance_stats_key(student_id, class_name, term, academic_year):
    return (
        f"results:attendance_stats:{_safe(student_id)}:{_safe(class_name)}:"
        f"{_safe(term)}:{_safe(academic_year)}"
    )


def class_size_key(class_name, term, academic_year):
    return f"results:class_size:{_safe(class_name)}:{_safe(term)}:{_safe(academic_year)}"


def class_courses_key(class_name, term):
    """Backs ClassCourseViewSet.by_class_and_term."""
    return f"results:class_courses:{_safe(class_name)}:{_safe(term)}"


def available_courses_key(class_name, term):
    """Backs ResultViewSet.get_available_courses."""
    return f"results:available_courses:{_safe(class_name)}:{_safe(term)}"


def students_by_class_key(class_name):
    """
    Backs ResultViewSet.get_students_by_class.

    No invalidation hook: student roster changes (new enrolment, class
    change, deactivation) happen in authapp, which this app doesn't listen
    to. Accepted the same way as attendance_stats_key's cross-app gap -
    5-minute TTL bounds the staleness.
    """
    return f"results:students_by_class:{_safe(class_name)}"


def invalidate_attendance_stats(student_id, class_name, term, academic_year):
    if not (student_id and class_name and term):
        return
    cache.delete(attendance_stats_key(student_id, class_name, term, academic_year))


def invalidate_class_size(class_name, term, academic_year):
    cache.delete(class_size_key(class_name, term, academic_year))


def invalidate_class_courses(class_name, term):
    """Call whenever a ClassCourse for this class+term is created, edited, or removed."""
    cache.delete_many([
        class_courses_key(class_name, term),
        available_courses_key(class_name, term),
    ])


def invalidate_students_by_class(class_name):
    cache.delete(students_by_class_key(class_name))