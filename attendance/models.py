from datetime import datetime, timedelta

from django.db import models
from django.core.exceptions import ValidationError
from django.utils import timezone
from django.utils.dateparse import parse_date

from authapp.models import CustomUser
from .cache_utils import (
    term_total_school_days_key,
    term_holiday_dates_key,
    invalidate_term,
    invalidate_holiday,
    invalidate_summaries,
    safe_cache_get,
    safe_cache_set,
)

TERM_CHOICES = (
    ('first', 'First Term'),
    ('second', 'Second Term'),
    ('third', 'Third Term'),
)

STATUS_CHOICES = (
    ('present', 'Present'),
    ('absent', 'Absent'),
)


class AcademicTerm(models.Model):
    """
    The real calendar behind a term.
    Attendance totals are computed from start_date, end_date, weekends, and holidays.
    """

    # FIX (limitation: "invalidation is all-or-nothing"): only these fields
    # actually change total_school_days / holiday_dates / the calendar. A
    # save() that only flips is_current (or touches created_at/updated_at)
    # has no business busting those caches.
    CALENDAR_AFFECTING_FIELDS = ("start_date", "end_date", "exclude_weekends")

    academic_year = models.CharField(
        max_length=20,
        default="2023-2024",
        help_text="Academic year, e.g. '2024-2025'"
    )
    term = models.CharField(max_length=10, choices=TERM_CHOICES)
    start_date = models.DateField()
    end_date = models.DateField()
    exclude_weekends = models.BooleanField(
        default=True,
        help_text="If checked, Saturdays and Sundays are excluded from school-day counts."
    )
    is_current = models.BooleanField(
        default=False,
        help_text="Marks this as the currently active term. Only one term can be current."
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        unique_together = ('academic_year', 'term')
        ordering = ['-academic_year', 'term']
        verbose_name = "Academic Term"
        verbose_name_plural = "Academic Terms"

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # Snapshot the calendar-affecting fields as they were when this
        # instance was loaded/created, so save() can tell whether they
        # actually changed on THIS save, instead of unconditionally
        # invalidating every time anything on the row changes.
        self._snapshot_calendar_fields()

    def _snapshot_calendar_fields(self):
        self._loaded_calendar_values = {
            field: getattr(self, field, None) for field in self.CALENDAR_AFFECTING_FIELDS
        }

    def __str__(self):
        return f"{self.get_term_display()} - {self.academic_year}"

    def clean(self):
        if self.start_date and self.end_date and self.start_date >= self.end_date:
            raise ValidationError("Start date must be before end date.")

    def save(self, *args, **kwargs):
        is_new = self._state.adding
        calendar_changed = is_new or any(
            self._loaded_calendar_values.get(field) != getattr(self, field)
            for field in self.CALENDAR_AFFECTING_FIELDS
        )

        self.full_clean()
        super().save(*args, **kwargs)

        if self.is_current:
            # FIX (limitation: "bulk .update() bypasses save()"): flipping
            # every other current term to False via a queryset .update()
            # previously skipped invalidate_term() for those terms
            # entirely, because .update() never calls .save(). That was
            # harmless only because nothing was cached against is_current
            # yet — a landmine waiting for the day something was.
            #
            # Fix: grab the affected ids first, do the same efficient bulk
            # update (no need to run full_clean()/save() once per row just
            # to flip one boolean), then explicitly invalidate each of
            # their term caches so the "is_current changed" fact is never
            # silently lost from a cache-invalidation point of view.
            other_current_ids = list(
                AcademicTerm.objects.filter(is_current=True)
                .exclude(pk=self.pk)
                .values_list("pk", flat=True)
            )
            if other_current_ids:
                AcademicTerm.objects.filter(pk__in=other_current_ids).update(is_current=False)
                for other_id in other_current_ids:
                    invalidate_term(other_id)

        if calendar_changed:
            # Dates or weekend-exclusion changed (or this is a new term) —
            # every derived cache for THIS term is now stale, and so is
            # every already-cached student summary for this term, since
            # total_school_days feeds directly into their percentages.
            invalidate_term(self.pk)
            self._invalidate_affected_summaries()

        # Re-snapshot so a second .save() call on the same in-memory
        # instance (without re-fetching from the DB) compares against the
        # values we just persisted, not the ones from before this save.
        self._snapshot_calendar_fields()

    def delete(self, *args, **kwargs):
        term_id = self.pk
        affected_student_ids = self._affected_student_ids()
        result = super().delete(*args, **kwargs)
        invalidate_term(term_id)
        invalidate_summaries(affected_student_ids, term_id)
        return result

    def _affected_student_ids(self):
        """
        Every student who has at least one attendance record under this
        term — i.e. every student whose cached summary could be stale
        after a calendar-affecting term/holiday edit.
        """
        return list(
            AttendanceRecord.objects.filter(
                register__term_id=self.pk
            ).values_list('student_id', flat=True).distinct()
        )

    def _invalidate_affected_summaries(self):
        invalidate_summaries(self._affected_student_ids(), self.pk)

    @property
    def holiday_dates(self):
        cache_key = term_holiday_dates_key(self.pk)
        cached = safe_cache_get(cache_key)

        if cached is not None:
            return cached

        dates = set(self.holidays.values_list('date', flat=True))
        safe_cache_set(cache_key, dates, 60 * 30)
        return dates

    @property
    def total_school_days(self):
        """
        Total number of school days between start_date and end_date.
        Excludes weekends if selected and excludes explicit holidays.
        """
        cache_key = term_total_school_days_key(self.pk)
        cached = safe_cache_get(cache_key)

        if cached is not None:
            return cached

        holidays = self.holiday_dates
        count = 0
        current = self.start_date
        while current <= self.end_date:
            if self.exclude_weekends and current.weekday() >= 5:
                current += timedelta(days=1)
                continue

            if current in holidays:
                current += timedelta(days=1)
                continue

            count += 1
            current += timedelta(days=1)

        safe_cache_set(cache_key, count, 60 * 30)
        return count

    def is_school_day(self, date_value):
        """
        Whether attendance can legitimately be marked on this date.

        Accepts:
        - datetime.date
        - datetime.datetime
        - string in YYYY-MM-DD format
        """
        if isinstance(date_value, str):
            parsed = parse_date(date_value)

            if parsed is None:
                try:
                    parsed = datetime.strptime(date_value, "%Y-%m-%d").date()
                except ValueError:
                    return False

            date_value = parsed

        if hasattr(date_value, "date") and callable(date_value.date):
            date_value = date_value.date()

        if date_value < self.start_date or date_value > self.end_date:
            return False

        if self.exclude_weekends and date_value.weekday() >= 5:
            return False

        if date_value in self.holiday_dates:
            return False

        return True


class Holiday(models.Model):
    """An explicit non-school day within a term."""
    term = models.ForeignKey(
        AcademicTerm,
        on_delete=models.CASCADE,
        related_name='holidays'
    )
    date = models.DateField()
    reason = models.CharField(
        max_length=255,
        blank=True,
        help_text="e.g. Founder's Day, Mid-term break"
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = ('term', 'date')
        ordering = ['date']
        verbose_name = "Holiday"
        verbose_name_plural = "Holidays"

    def __str__(self):
        return f"{self.date} - {self.reason or 'Holiday'} ({self.term})"

    def clean(self):
        if self.term_id and self.date:
            if self.date < self.term.start_date or self.date > self.term.end_date:
                raise ValidationError(
                    "Holiday date must fall within the term's start and end dates."
                )

    def save(self, *args, **kwargs):
        self.full_clean()
        super().save(*args, **kwargs)
        # A holiday is, by definition, always calendar-affecting (it
        # changes total_school_days and holiday_dates), so unlike
        # AcademicTerm.save() there's no "which field changed" branch
        # needed here — every Holiday write invalidates.
        invalidate_holiday(self.term_id)
        self.term._invalidate_affected_summaries()

    def delete(self, *args, **kwargs):
        term_id = self.term_id
        affected_student_ids = self.term._affected_student_ids()
        result = super().delete(*args, **kwargs)
        invalidate_holiday(term_id)
        invalidate_summaries(affected_student_ids, term_id)
        return result


class ClassAttendanceRegister(models.Model):
    """One register per class per school day."""
    class_name = models.CharField(
        max_length=10,
        choices=CustomUser.CLASS_CHOICES
    )
    term = models.ForeignKey(
        AcademicTerm,
        on_delete=models.PROTECT,
        related_name='registers'
    )
    date = models.DateField()
    marked_by = models.ForeignKey(
        CustomUser,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='attendance_registers_marked'
    )
    is_finalized = models.BooleanField(default=False)
    finalized_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        unique_together = ('class_name', 'term', 'date')
        ordering = ['-date', 'class_name']
        verbose_name = "Class Attendance Register"
        verbose_name_plural = "Class Attendance Registers"

    def __str__(self):
        return f"{self.class_name} - {self.date} ({self.term})"

    def clean(self):
        if self.term_id and self.date:
            if not self.term.is_school_day(self.date):
                raise ValidationError(
                    "This date is not a valid school day for the selected term "
                    "(it falls outside the term dates, on a weekend, or on a holiday)."
                )

    def save(self, *args, **kwargs):
        self.full_clean()

        if self.is_finalized and not self.finalized_at:
            self.finalized_at = timezone.now()

        if not self.is_finalized:
            self.finalized_at = None

        super().save(*args, **kwargs)

    @property
    def present_count(self):
        return self.records.filter(status='present').count()

    @property
    def absent_count(self):
        return self.records.filter(status='absent').count()


class AttendanceRecord(models.Model):
    """A single student's mark within a ClassAttendanceRegister."""
    register = models.ForeignKey(
        ClassAttendanceRegister,
        on_delete=models.CASCADE,
        related_name='records'
    )
    student = models.ForeignKey(
        CustomUser,
        on_delete=models.CASCADE,
        related_name='attendance_records'
    )
    status = models.CharField(max_length=10, choices=STATUS_CHOICES)
    absence_reason = models.CharField(
        max_length=255,
        blank=True,
        null=True,
        help_text="Required when status is Absent."
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        unique_together = ('register', 'student')
        ordering = ['student__last_name', 'student__first_name']
        verbose_name = "Attendance Record"
        verbose_name_plural = "Attendance Records"

    def __str__(self):
        name = f"{self.student.first_name} {self.student.last_name}".strip()
        name = name or self.student.username
        return f"{name} - {self.register.date} - {self.get_status_display()}"

    def clean(self):
        if self.status == 'absent' and not (self.absence_reason and self.absence_reason.strip()):
            raise ValidationError({
                'absence_reason': "A reason is required when marking a student absent."
            })

        if self.status == 'present':
            self.absence_reason = None

        if self.register_id and self.student_id:
            if self.student.role != 'student':
                raise ValidationError(
                    "Attendance can only be recorded for users with role 'student'."
                )

            if self.student.class_name != self.register.class_name:
                raise ValidationError(
                    f"Student's class ({self.student.class_name}) does not match "
                    f"the register's class ({self.register.class_name})."
                )

    def save(self, *args, **kwargs):
        self.full_clean()
        super().save(*args, **kwargs)

    @classmethod
    def get_summary(cls, student, term):
        qs = cls.objects.filter(student=student, register__term=term)

        days_present = qs.filter(status='present').count()
        days_absent = qs.filter(status='absent').count()

        return {
            'days_present': days_present,
            'days_absent': days_absent,
            'total_marked': days_present + days_absent,
        }


def describe_changes(old_values, new_values):
    """
    Builds a human-readable diff string.
    Example:
    status: 'present' -> 'absent'; absence_reason: 'None' -> 'Sick'
    """
    parts = []

    for field, new_val in new_values.items():
        old_val = old_values.get(field)

        if old_val != new_val:
            parts.append(f"{field}: '{old_val}' -> '{new_val}'")

    return "; ".join(parts) if parts else "no field changes"


class AttendanceChangeLog(models.Model):
    """Audit trail for register and attendance record changes."""
    ACTION_CHOICES = [
        ('CREATE', 'Create'),
        ('UPDATE', 'Update'),
        ('STATUS_CHANGE', 'Status Change'),
        ('DELETE', 'Delete'),
        ('FINALIZE', 'Finalize'),
        ('REOPEN', 'Reopen'),
    ]

    register = models.ForeignKey(
        ClassAttendanceRegister,
        on_delete=models.CASCADE,
        related_name='logs'
    )
    record = models.ForeignKey(
        AttendanceRecord,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='logs'
    )
    student = models.ForeignKey(
        CustomUser,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='attendance_change_logs'
    )
    user = models.ForeignKey(
        CustomUser,
        on_delete=models.SET_NULL,
        null=True,
        related_name='attendance_changes_made'
    )
    user_email = models.EmailField(max_length=255, blank=True)
    changed_fields = models.TextField()
    action_type = models.CharField(max_length=20, choices=ACTION_CHOICES)
    timestamp = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-timestamp']
        verbose_name = "Attendance Change Log"
        verbose_name_plural = "Attendance Change Logs"

    def __str__(self):
        return (
            f"Log for register {self.register_id} "
            f"({self.action_type}) by {self.user_email} at {self.timestamp}"
        )