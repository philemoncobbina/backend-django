from django.core.cache import cache
from django.db import models, transaction
from django.db.models import Case, Count, IntegerField, When
from django.core.validators import MaxValueValidator, MinValueValidator
from django.core.exceptions import ValidationError
from django.utils import timezone

from authapp.models import CustomUser

# Change "attendance" to your actual attendance app name if different.
from attendance.models import AcademicTerm, AttendanceRecord

from .cache_utils import (
    CACHE_TTL_SHORT,
    attendance_stats_key,
    class_size_key,
    invalidate_attendance_stats,
)


TERM_CHOICES = (
    ('first', 'First Term'),
    ('second', 'Second Term'),
    ('third', 'Third Term'),
)


class Course(models.Model):
    """Represents a course or subject."""
    name = models.CharField(max_length=100, unique=True)
    code = models.CharField(max_length=10, unique=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    created_by = models.ForeignKey(
        CustomUser,
        on_delete=models.CASCADE,
        related_name='created_courses'
    )

    def __str__(self):
        return f"{self.name} ({self.code})"

    class Meta:
        ordering = ['name']


class ClassCourse(models.Model):
    """Assigns courses to specific classes and terms."""
    course = models.ForeignKey(
        Course,
        on_delete=models.CASCADE,
        related_name='class_assignments'
    )
    class_name = models.CharField(
        max_length=10,
        choices=CustomUser.CLASS_CHOICES,
        help_text="Class this course is assigned to"
    )
    term = models.CharField(
        max_length=10,
        choices=TERM_CHOICES,
        null=False,
        blank=False,
    )
    is_active = models.BooleanField(default=True)

    teacher_name = models.CharField(
        max_length=150,
        help_text="Full name of the course teacher",
        default="Staff Name"
    )
    teacher_email = models.EmailField(
        max_length=254,
        help_text="Email address of the course teacher",
        default="staff@school.edu"
    )
    teacher_phone = models.CharField(
        max_length=20,
        help_text="Phone number of the course teacher",
        default="0000000000"
    )

    def __str__(self):
        return f"{self.course.name} - {self.class_name} ({self.get_term_display()})"

    def clean(self):
        if ClassCourse.objects.filter(
            course=self.course,
            class_name=self.class_name,
            term=self.term
        ).exclude(pk=self.pk).exists():
            raise ValidationError(
                f"This course is already assigned to class {self.class_name} "
                f"in {self.get_term_display()}"
            )

    class Meta:
        unique_together = ('course', 'class_name', 'term')
        ordering = ['class_name', 'term', 'course__name']


class ClassSize(models.Model):
    """Tracks total number of result records in each class for a term."""
    class_name = models.CharField(
        max_length=10,
        choices=CustomUser.CLASS_CHOICES,
        help_text="Class name"
    )
    term = models.CharField(
        max_length=10,
        choices=TERM_CHOICES,
        help_text="Academic term"
    )
    academic_year = models.CharField(
        max_length=20,
        default="2023-2024",
        help_text="Academic year, e.g. '2024-2025'"
    )
    total_students = models.PositiveIntegerField(
        default=0,
        help_text="Total number of students with results in this class and term"
    )
    last_updated = models.DateTimeField(auto_now=True)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return (
            f"{self.class_name} - {self.get_term_display()} "
            f"({self.academic_year}): {self.total_students} students"
        )

    @classmethod
    def update_class_size(cls, class_name, term, academic_year="2023-2024"):
        student_count = Result.objects.filter(
            class_name=class_name,
            term=term,
            academic_year=academic_year
        ).count()

        class_size, created = cls.objects.get_or_create(
            class_name=class_name,
            term=term,
            academic_year=academic_year,
            defaults={'total_students': student_count}
        )

        if not created:
            class_size.total_students = student_count
            class_size.save(update_fields=['total_students', 'last_updated'])

        # Write the fresh count straight into the cache instead of just
        # invalidating - the caller (Result.save(), bulk operations) always
        # knows this count is now correct, so there's no reason to force
        # the next get_class_size() call to hit the DB again.
        cache.set(
            class_size_key(class_name, term, academic_year),
            class_size.total_students,
            CACHE_TTL_SHORT,
        )

        return class_size

    @classmethod
    def get_class_size(cls, class_name, term, academic_year="2023-2024"):
        cache_key = class_size_key(class_name, term, academic_year)
        cached = cache.get(cache_key)

        if cached is not None:
            return cached

        try:
            total_students = cls.objects.get(
                class_name=class_name,
                term=term,
                academic_year=academic_year
            ).total_students
        except cls.DoesNotExist:
            class_size = cls.update_class_size(class_name, term, academic_year)
            # update_class_size already wrote the cache - just return its value.
            return class_size.total_students

        cache.set(cache_key, total_students, CACHE_TTL_SHORT)
        return total_students

    class Meta:
        unique_together = ('class_name', 'term', 'academic_year')
        ordering = ['class_name', 'term', 'academic_year']
        verbose_name = "Class Size"
        verbose_name_plural = "Class Sizes"


class Result(models.Model):
    """Represents a complete result for a student in a term."""
    STATUS_CHOICES = [
        ('DRAFT', 'Draft'),
        ('SCHEDULED', 'Scheduled'),
        ('PUBLISHED', 'Published'),
    ]

    student = models.ForeignKey(
        CustomUser,
        on_delete=models.CASCADE,
        related_name='results'
    )
    class_name = models.CharField(
        max_length=10,
        choices=CustomUser.CLASS_CHOICES
    )
    term = models.CharField(
        max_length=10,
        choices=TERM_CHOICES
    )
    academic_year = models.CharField(
        max_length=20,
        default="2023-2024",
        help_text="Academic year, e.g. '2024-2025'"
    )

    status = models.CharField(
        max_length=10,
        choices=STATUS_CHOICES,
        default='DRAFT'
    )
    scheduled_date = models.DateTimeField(null=True, blank=True)
    published_date = models.DateTimeField(null=True, blank=True)

    overall_position = models.PositiveIntegerField(
        null=True,
        blank=True,
        help_text="Overall position in class"
    )

    class_teacher_remarks = models.TextField(
        blank=True,
        null=True,
        help_text="Class teacher's general remarks"
    )

    promoted_to = models.CharField(
        max_length=10,
        choices=CustomUser.CLASS_CHOICES,
        blank=True,
        null=True,
        help_text="Class student is promoted to"
    )

    next_term_begins = models.DateField(
        null=True,
        blank=True,
        help_text="Date when next term begins"
    )

    report_card_pdf = models.FileField(
        upload_to='report_cards/',
        null=True,
        blank=True,
        help_text="Generated PDF report card"
    )

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        student_name = self.student.get_full_name() or self.student.username
        return (
            f"{student_name} - {self.class_name} - "
            f"{self.get_term_display()} ({self.status})"
        )

    def get_report_card_filename(self):
        safe_student_name = "".join(
            c for c in f"{self.student.first_name}_{self.student.last_name}"
            if c.isalnum() or c in "._- "
        )
        safe_academic_year = self.academic_year.replace('-', '_')
        return (
            f"{safe_student_name}_{self.class_name}_"
            f"{self.term}_{safe_academic_year}.pdf"
        )

    @property
    def is_published(self):
        return self.status == 'PUBLISHED'

    @property
    def academic_term(self):
        """
        Finds the matching attendance AcademicTerm using the result's
        academic_year and term.
        """
        if not self.academic_year or not self.term:
            return None

        return AcademicTerm.objects.filter(
            academic_year=self.academic_year,
            term=self.term
        ).first()

    def get_attendance_queryset(self):
        """
        Returns AttendanceRecord queryset for this student, class, year, and term.
        """
        academic_term = self.academic_term

        if not academic_term or not self.student_id:
            return AttendanceRecord.objects.none()

        return AttendanceRecord.objects.filter(
            student_id=self.student_id,
            register__class_name=self.class_name,
            register__term=academic_term
        )

    def _attendance_stats(self):
        """
        Cached bundle of days_present / days_absent / total_school_days.

        Previously, days_present/days_absent/total_school_days/
        days_unmarked/attendance_percentage/marked_attendance_percentage
        were six independent properties, each re-querying self.academic_term
        and re-running its own .count() - reading all six on one Result
        could hit the DB 10+ times, and a class list multiplies that by
        every student in it.

        Now: one Redis lookup (or, on a miss, one academic_term query plus
        one aggregate query that counts present/absent in a single pass)
        computes the whole bundle, and every property below reads from it.
        The dict is also memoized on the instance itself, so accessing all
        six fields during one serialization only touches Redis once.
        """
        if hasattr(self, '_attendance_stats_cache'):
            return self._attendance_stats_cache

        if not self.student_id or not self.class_name or not self.term:
            stats = {'days_present': 0, 'days_absent': 0, 'total_school_days': 0}
            self._attendance_stats_cache = stats
            return stats

        cache_key = attendance_stats_key(
            self.student_id, self.class_name, self.term, self.academic_year
        )
        cached = cache.get(cache_key)

        if cached is not None:
            self._attendance_stats_cache = cached
            return cached

        academic_term = self.academic_term

        if not academic_term:
            stats = {'days_present': 0, 'days_absent': 0, 'total_school_days': 0}
        else:
            counts = self.get_attendance_queryset().aggregate(
                days_present=Count(Case(
                    When(status='present', then=1),
                    output_field=IntegerField(),
                )),
                days_absent=Count(Case(
                    When(status='absent', then=1),
                    output_field=IntegerField(),
                )),
            )
            stats = {
                'days_present': counts['days_present'] or 0,
                'days_absent': counts['days_absent'] or 0,
                # total_school_days is itself a cached property on
                # AcademicTerm (see attendance/models.py) - this is a
                # cache-hit-on-cache-hit in the common case.
                'total_school_days': academic_term.total_school_days,
            }

        cache.set(cache_key, stats, CACHE_TTL_SHORT)
        self._attendance_stats_cache = stats
        return stats

    @property
    def days_present(self):
        return self._attendance_stats()['days_present']

    @property
    def days_absent(self):
        return self._attendance_stats()['days_absent']

    @property
    def total_marked_attendance_days(self):
        stats = self._attendance_stats()
        return stats['days_present'] + stats['days_absent']

    @property
    def total_school_days(self):
        return self._attendance_stats()['total_school_days']

    @property
    def days_unmarked(self):
        return max(self.total_school_days - self.total_marked_attendance_days, 0)

    @property
    def total_days(self):
        """
        Kept for backward compatibility.
        This now means total marked attendance days, not manual entry.
        """
        return self.total_marked_attendance_days

    @property
    def attendance_percentage(self):
        """
        Attendance percentage based on total school days in the AcademicTerm.
        """
        stats = self._attendance_stats()
        total_school_days = stats['total_school_days']

        if total_school_days == 0:
            return 0.0

        return round((stats['days_present'] / total_school_days) * 100, 2)

    @property
    def marked_attendance_percentage(self):
        """
        Attendance percentage based only on days attendance has been marked.
        """
        stats = self._attendance_stats()
        total_marked = stats['days_present'] + stats['days_absent']

        if total_marked == 0:
            return 0.0

        return round((stats['days_present'] / total_marked) * 100, 2)

    @property
    def total_score(self):
        return sum(cr.total_score for cr in self.course_results.all())

    @property
    def average_score(self):
        course_results = self.course_results.all()

        if not course_results:
            return 0.0

        return round(self.total_score / len(course_results), 2)

    @property
    def total_students_in_class(self):
        return ClassSize.get_class_size(
            self.class_name,
            self.term,
            self.academic_year
        )

    @property
    def position_context(self):
        total_students = self.total_students_in_class

        if self.overall_position and total_students > 0:
            return f"{self.overall_position}/{total_students}"

        return "N/A"

    def calculate_positions(self):
        with transaction.atomic():
            ClassSize.update_class_size(
                self.class_name,
                self.term,
                self.academic_year
            )
            self._calculate_all_overall_positions()
            self._calculate_all_course_positions()

    def _calculate_all_overall_positions(self):
        all_results = Result.objects.filter(
            class_name=self.class_name,
            term=self.term,
            academic_year=self.academic_year
        ).select_related('student').prefetch_related('course_results')

        if not all_results.exists():
            return

        results_list = list(all_results)

        sorted_results = sorted(
            results_list,
            key=lambda r: (r.total_score, r.average_score),
            reverse=True
        )

        current_position = 1
        previous_total_score = None
        previous_average_score = None

        for index, result in enumerate(sorted_results):
            if (
                previous_total_score is not None
                and (
                    result.total_score < previous_total_score
                    or (
                        result.total_score == previous_total_score
                        and result.average_score < previous_average_score
                    )
                )
            ):
                current_position = index + 1

            if result.overall_position != current_position:
                result.overall_position = current_position
                result.save(
                    update_fields=['overall_position'],
                    calculate_positions=False
                )

            previous_total_score = result.total_score
            previous_average_score = result.average_score

    def _calculate_all_course_positions(self):
        class_courses = ClassCourse.objects.filter(
            class_name=self.class_name,
            term=self.term,
            is_active=True
        )

        for class_course in class_courses:
            course_results = CourseResult.objects.filter(
                class_course=class_course,
                result__class_name=self.class_name,
                result__term=self.term,
                result__academic_year=self.academic_year
            ).select_related('result')

            if not course_results.exists():
                continue

            course_results_list = list(course_results)

            sorted_course_results = sorted(
                course_results_list,
                key=lambda cr: cr.total_score,
                reverse=True
            )

            current_position = 1
            previous_score = None

            for index, course_result in enumerate(sorted_course_results):
                if previous_score is not None and course_result.total_score < previous_score:
                    current_position = index + 1

                if course_result.position != current_position:
                    course_result.position = current_position
                    course_result.save(
                        update_fields=['position'],
                        calculate_positions=False
                    )

                previous_score = course_result.total_score

    def clean(self):
        if self.status in ['PUBLISHED', 'SCHEDULED']:
            if not self.student_id:
                raise ValidationError(
                    "Student must be specified before publishing or scheduling results."
                )

            if not self.class_name:
                raise ValidationError(
                    "Class name must be specified before publishing or scheduling results."
                )

            if not self.term:
                raise ValidationError(
                    "Term must be specified before publishing or scheduling results."
                )

            if self.status == 'SCHEDULED' and not self.scheduled_date:
                raise ValidationError(
                    "Scheduled date must be specified when setting status to Scheduled."
                )

        if self.status == 'SCHEDULED' and self.scheduled_date:
            if self.scheduled_date <= timezone.now():
                raise ValidationError(
                    "Scheduled date must be in the future."
                )

        if self.term == 'third':
            if not self.promoted_to or str(self.promoted_to).strip() == "":
                raise ValidationError(
                    "Promoted to class must be specified for third term results."
                )

        if self.promoted_to and self.promoted_to == self.class_name:
            raise ValidationError(
                "Student cannot be promoted to the same class."
            )

        if self.overall_position:
            total_students = ClassSize.get_class_size(
                self.class_name,
                self.term,
                self.academic_year
            )

            if self.overall_position > total_students:
                raise ValidationError(
                    f"Position {self.overall_position} cannot be greater than "
                    f"total students {total_students} in class {self.class_name}."
                )

    def save(self, *args, **kwargs):
        calculate_positions = kwargs.pop('calculate_positions', True)

        self.full_clean()

        current_time = timezone.now()

        if (
            self.status == 'SCHEDULED'
            and self.scheduled_date
            and self.scheduled_date <= current_time
        ):
            self.status = 'PUBLISHED'
            self.published_date = current_time

        if self.status == 'PUBLISHED' and not self.published_date:
            self.published_date = current_time

        super().save(*args, **kwargs)

        ClassSize.update_class_size(
            self.class_name,
            self.term,
            self.academic_year
        )

        # This result's own attendance-stat cache (for its current
        # class/term/year) is now stale - clear it and the instance-level
        # memo so the next read recomputes. Note this only covers the
        # result's CURRENT location; if class_name/term/academic_year just
        # changed, the OLD location's cache is invalidated separately in
        # ResultViewSet.update(), which is the only place that still has
        # the old values at invalidation time.
        invalidate_attendance_stats(
            self.student_id, self.class_name, self.term, self.academic_year
        )
        if hasattr(self, '_attendance_stats_cache'):
            del self._attendance_stats_cache

        if calculate_positions and self.course_results.exists():
            self.calculate_positions()

    class Meta:
        ordering = ['student__last_name', 'student__first_name', 'term']
        unique_together = ('student', 'class_name', 'term', 'academic_year')
        verbose_name = "Result"
        verbose_name_plural = "Results"


class CourseResult(models.Model):
    """Individual course results within a result."""
    GRADE_CHOICES = (
        ('A', 'Excellent (70-100)'),
        ('B', 'Very Good (60-69)'),
        ('C', 'Good (50-59)'),
        ('D', 'Fair (45-49)'),
        ('E', 'Pass (40-44)'),
        ('F', 'Fail (0-39)'),
    )

    result = models.ForeignKey(
        Result,
        on_delete=models.CASCADE,
        related_name='course_results'
    )
    class_course = models.ForeignKey(
        ClassCourse,
        on_delete=models.CASCADE,
        related_name='course_results'
    )
    class_score = models.DecimalField(
        max_digits=5,
        decimal_places=2,
        validators=[MinValueValidator(0), MaxValueValidator(40)],
        help_text="Class score, maximum 40"
    )
    exam_score = models.DecimalField(
        max_digits=5,
        decimal_places=2,
        validators=[MinValueValidator(0), MaxValueValidator(60)],
        help_text="Exam score, maximum 60"
    )
    remarks = models.CharField(max_length=255, blank=True, null=True)
    position = models.PositiveIntegerField(
        null=True,
        blank=True,
        help_text="Position in this course for the class"
    )

    @property
    def total_score(self):
        return float(self.class_score) + float(self.exam_score)

    @property
    def grade(self):
        total = self.total_score

        if total >= 70:
            return 'A'
        if total >= 60:
            return 'B'
        if total >= 50:
            return 'C'
        if total >= 45:
            return 'D'
        if total >= 40:
            return 'E'

        return 'F'

    @property
    def position_context(self):
        total_students = ClassSize.get_class_size(
            self.result.class_name,
            self.result.term,
            self.result.academic_year
        )

        if self.position and total_students > 0:
            return f"{self.position}/{total_students}"

        return "N/A"

    def clean(self):
        if not self.class_course_id:
            raise ValidationError("Class course must be specified.")

        if self.class_score is None:
            raise ValidationError("Class score must be specified.")

        if self.exam_score is None:
            raise ValidationError("Exam score must be specified.")

        if self.result_id and self.class_course_id:
            if self.class_course.class_name != self.result.class_name:
                raise ValidationError(
                    "Class course class name does not match result class name."
                )

            if self.class_course.term != self.result.term:
                raise ValidationError(
                    "Class course term does not match result term."
                )

        if self.position and self.result_id:
            total_students = ClassSize.get_class_size(
                self.result.class_name,
                self.result.term,
                self.result.academic_year
            )

            if self.position > total_students:
                raise ValidationError(
                    f"Course position {self.position} cannot be greater than "
                    f"total students {total_students} in class."
                )

    def save(self, *args, **kwargs):
        calculate_positions = kwargs.pop('calculate_positions', True)

        self.full_clean()
        super().save(*args, **kwargs)

        if calculate_positions and self.result_id:
            self.result.calculate_positions()

    def __str__(self):
        return (
            f"{self.result} - {self.class_course.course.name} - "
            f"{self.total_score} ({self.grade})"
        )

    class Meta:
        ordering = ['class_course__course__name']
        unique_together = ('result', 'class_course')
        verbose_name = "Course Result"
        verbose_name_plural = "Course Results"


class ResultChangeLog(models.Model):
    """Tracks changes made to results."""
    result = models.ForeignKey(
        Result,
        on_delete=models.CASCADE,
        related_name='change_logs'
    )
    changed_by = models.EmailField(max_length=255)
    changed_at = models.DateTimeField(auto_now_add=True)
    field_name = models.CharField(max_length=100)
    previous_value = models.TextField(null=True, blank=True)
    new_value = models.TextField(null=True, blank=True)

    def __str__(self):
        return (
            f"{self.field_name} changed from {self.previous_value} "
            f"to {self.new_value} by {self.changed_by}"
        )

    class Meta:
        ordering = ['-changed_at']
        verbose_name = "Result Change Log"
        verbose_name_plural = "Result Change Logs"