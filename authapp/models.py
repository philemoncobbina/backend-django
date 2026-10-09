#authapp/models.py

from django.contrib.auth.models import AbstractBaseUser, BaseUserManager, PermissionsMixin
from django.db import models
from django.utils import timezone
from django.core.exceptions import ValidationError


class CustomUserManager(BaseUserManager):
    def create_user(self, email, username, password=None, **extra_fields):
        if not email:
            raise ValueError('The Email field must be set')
        if not username:
            raise ValueError('The Username field must be set')

        email = self.normalize_email(email)
        user = self.model(email=email, username=username, **extra_fields)
        user.set_password(password)
        user.save(using=self._db)
        return user

    def create_superuser(self, email, username, password=None, **extra_fields):
        extra_fields.setdefault('is_staff', True)
        extra_fields.setdefault('is_superuser', True)
        extra_fields.setdefault('is_active', True)
        extra_fields.setdefault('role', 'principal')

        if extra_fields.get('is_staff') is not True:
            raise ValueError('Superuser must have is_staff=True.')
        if extra_fields.get('is_superuser') is not True:
            raise ValueError('Superuser must have is_superuser=True.')

        return self.create_user(email, username, password, **extra_fields)

    def create_student(self, email, first_name, last_name, password,
                       index_number, class_name, **extra_fields):
        username = extra_fields.pop('username', index_number.lower())

        # Validate Graduated / Stopped requirements before anything is written.
        # Raises django.core.exceptions.ValidationError if data is missing/invalid.
        extra_fields.update(
            CustomUser.resolve_status_fields(
                class_name,
                graduation_year=extra_fields.get('graduation_year'),
                stopped_class=extra_fields.get('stopped_class'),
                stop_reason=extra_fields.get('stop_reason'),
                stop_year=extra_fields.get('stop_year'),
            )
        )

        extra_fields.setdefault('role', 'student')
        extra_fields.setdefault('is_active', True)
        extra_fields['index_number'] = index_number
        extra_fields['class_name'] = class_name

        user = self.create_user(
            email=email,
            username=username,
            first_name=first_name,
            last_name=last_name,
            password=password,
            **extra_fields
        )
        return user


class CustomUser(AbstractBaseUser, PermissionsMixin):
    ROLE_CHOICES = (
        ('principal', 'Principal'),
        ('staff', 'Staff'),
        ('student', 'Student'),
    )

    # Status values stored in `class_name` for students who are no longer in a class.
    GRADUATED = 'Graduated'
    STOPPED = 'Stopped'

    # Classes a student can actually sit in.
    ACTIVE_CLASS_CHOICES = (
        ('Creche', 'Creche'),
        ('Nursery', 'Nursery'),
        ('KG 1', 'KG 1'),
        ('KG 2', 'KG 2'),
        ('Class 1', 'Class 1'),
        ('Class 2', 'Class 2'),
        ('Class 3', 'Class 3'),
        ('Class 4', 'Class 4'),
        ('Class 5', 'Class 5'),
        ('Class 6', 'Class 6'),
        ('JHS 1', 'JHS 1'),
        ('JHS 2', 'JHS 2'),
        ('JHS 3', 'JHS 3'),
    )

    # Non-class statuses.
    STATUS_CHOICES = (
        (GRADUATED, 'Graduated'),
        (STOPPED, 'Stopped School'),
    )

    # Full list used by the `class_name` field.
    CLASS_CHOICES = ACTIVE_CLASS_CHOICES + STATUS_CHOICES

    # Earliest year accepted for graduation / stopping year.
    MIN_STATUS_YEAR = 1990

    username = models.CharField(max_length=150, unique=False, default="default_username")
    email = models.EmailField(unique=True)
    first_name = models.CharField(max_length=30, blank=True)
    last_name = models.CharField(max_length=30, blank=True)
    password = models.CharField(max_length=128)
    is_active = models.BooleanField(default=False)
    is_staff = models.BooleanField(default=False)
    is_superuser = models.BooleanField(default=False)
    is_blocked = models.BooleanField(default=False)
    date_joined = models.DateTimeField(default=timezone.now)
    verification_code = models.CharField(max_length=6, null=True, blank=True)
    role = models.CharField(max_length=10, choices=ROLE_CHOICES)
    is_google_account = models.BooleanField(default=False)

    index_number = models.CharField(max_length=20, unique=True, null=True, blank=True)
    class_name = models.CharField(
        max_length=10, choices=CLASS_CHOICES, null=True, blank=True
    )

    # Required only when class_name == 'Graduated'
    graduation_year = models.PositiveSmallIntegerField(
        null=True, blank=True,
        help_text="Year the student graduated (required for graduated students)."
    )

    # Required only when class_name == 'Stopped'
    stopped_class = models.CharField(
        max_length=10, choices=ACTIVE_CLASS_CHOICES, null=True, blank=True,
        help_text="Class the student was in when they stopped school."
    )
    stop_reason = models.TextField(
        blank=True, default='',
        help_text="Reason the student stopped school."
    )
    stop_year = models.PositiveSmallIntegerField(
        null=True, blank=True,
        help_text="Year the student stopped school."
    )

    objects = CustomUserManager()

    USERNAME_FIELD = 'email'
    REQUIRED_FIELDS = ['username']

    def __str__(self):
        return self.email

    @property
    def is_student(self):
        return self.role == 'student'

    @property
    def is_graduated(self):
        return self.class_name == self.GRADUATED

    @property
    def has_stopped_school(self):
        return self.class_name == self.STOPPED

    @property
    def is_enrolled(self):
        """True when the student is currently sitting in a regular class."""
        return self.class_name in {value for value, _ in self.ACTIVE_CLASS_CHOICES}

    @classmethod
    def resolve_status_fields(cls, class_name, graduation_year=None,
                              stopped_class=None, stop_reason='', stop_year=None):
        """
        Single source of truth for the Graduated / Stopped rules.

        - Graduated  -> graduation_year is required.
        - Stopped    -> stopped_class, stop_reason and stop_year are required.
        - Any other class -> all four status fields are cleared.

        Returns a dict of the cleaned values for the four status fields, or
        raises django.core.exceptions.ValidationError with per-field messages.
        """
        errors = {}
        current_year = timezone.now().year
        active_classes = {value for value, _ in cls.ACTIVE_CLASS_CHOICES}

        cleaned = {
            'graduation_year': None,
            'stopped_class': None,
            'stop_reason': '',
            'stop_year': None,
        }

        if class_name == cls.GRADUATED:
            if graduation_year is None:
                errors['graduation_year'] = "Year of graduation is required for graduated students."
            elif not (cls.MIN_STATUS_YEAR <= graduation_year <= current_year + 1):
                errors['graduation_year'] = (
                    f"Enter a valid graduation year between {cls.MIN_STATUS_YEAR} and {current_year + 1}."
                )
            cleaned['graduation_year'] = graduation_year

        elif class_name == cls.STOPPED:
            reason = (stop_reason or '').strip()

            if not stopped_class:
                errors['stopped_class'] = "The class the student stopped at is required."
            elif stopped_class not in active_classes:
                errors['stopped_class'] = "Choose a regular class (e.g. 'Class 4'), not a status."

            if not reason:
                errors['stop_reason'] = "A reason for stopping school is required."

            if stop_year is None:
                errors['stop_year'] = "The year the student stopped school is required."
            elif not (cls.MIN_STATUS_YEAR <= stop_year <= current_year):
                errors['stop_year'] = (
                    f"Enter a valid year between {cls.MIN_STATUS_YEAR} and {current_year}."
                )

            cleaned.update({
                'stopped_class': stopped_class,
                'stop_reason': reason,
                'stop_year': stop_year,
            })

        if errors:
            raise ValidationError(errors)

        return cleaned

    def clean(self):
        """Runs on full_clean() and Django admin saves."""
        super().clean()
        cleaned = self.resolve_status_fields(
            self.class_name,
            graduation_year=self.graduation_year,
            stopped_class=self.stopped_class,
            stop_reason=self.stop_reason,
            stop_year=self.stop_year,
        )
        for field, value in cleaned.items():
            setattr(self, field, value)

    def update_class(self, new_class, academic_year=None, *, graduation_year=None,
                     stopped_class=None, stop_reason='', stop_year=None):
        """
        Update student's class and persist the previous class in history.

        To graduate a student:
            student.update_class('Graduated', graduation_year=2026)

        To record a student who stopped school:
            student.update_class('Stopped', stop_reason='Relocated', stop_year=2026)
            (stopped_class defaults to the class they were in; pass it to override)
        """
        from django.apps import apps
        AcademicYear = apps.get_model('booklistapp', 'AcademicYear')
        StudentClassHistory = apps.get_model('booklistapp', 'StudentClassHistory')

        previous_class = self.class_name
        active_classes = {value for value, _ in self.ACTIVE_CLASS_CHOICES}

        # Default "class stopped at" to the class the student is leaving.
        if new_class == self.STOPPED and not stopped_class and previous_class in active_classes:
            stopped_class = previous_class

        # Validate first so nothing is written if the status data is incomplete.
        cleaned = self.resolve_status_fields(
            new_class,
            graduation_year=graduation_year,
            stopped_class=stopped_class,
            stop_reason=stop_reason,
            stop_year=stop_year,
        )

        if not academic_year:
            academic_year = AcademicYear.objects.filter(is_current=True).first()
            if not academic_year:
                raise ValueError("No current academic year found")

        # Only real classes go into history (not 'Graduated' / 'Stopped').
        if previous_class in active_classes and previous_class != new_class:
            history_entry, created = StudentClassHistory.objects.get_or_create(
                student=self,
                academic_year=academic_year,
                defaults={'class_name': previous_class}
            )
            if not created:
                history_entry.class_name = previous_class
                history_entry.save()

        self.class_name = new_class
        for field, value in cleaned.items():
            setattr(self, field, value)

        self.save(update_fields=[
            'class_name', 'graduation_year', 'stopped_class', 'stop_reason', 'stop_year',
        ])


class ParentGuardian(models.Model):
    RELATIONSHIP_CHOICES = (
        ('father', 'Father'),
        ('mother', 'Mother'),
        ('guardian', 'Guardian'),
        ('grandparent', 'Grandparent'),
        ('sibling', 'Sibling'),
        ('uncle', 'Uncle'),
        ('aunt', 'Aunt'),
        ('other', 'Other'),
    )

    ID_TYPE_CHOICES = (
        ('national_id', 'National ID'),
        ('passport', 'Passport'),
        ('drivers_license', "Driver's License"),
        ('voters_id', "Voter's ID"),
        ('other', 'Other'),
    )

    student = models.ForeignKey(
        CustomUser,
        on_delete=models.CASCADE,
        related_name='guardians',
        limit_choices_to={'role': 'student'},
    )

    first_name = models.CharField(max_length=50)
    middle_name = models.CharField(max_length=50, blank=True, default='')
    last_name = models.CharField(max_length=50)
    suffix = models.CharField(max_length=10, blank=True, default='', help_text="e.g. Jr., Sr., III")
    relationship = models.CharField(max_length=20, choices=RELATIONSHIP_CHOICES)

    primary_phone = models.CharField(
        max_length=20,
        help_text="Primary mobile number with country code, e.g. +233241234567"
    )
    secondary_phone = models.CharField(max_length=20, blank=True, default='',
        help_text="Alternate mobile, home, or office number")
    email = models.EmailField(
        blank=True, default='',
        help_text="Personal email for official communication and portal access"
    )

    street_address = models.CharField(max_length=255, blank=True, default='')
    city = models.CharField(max_length=100, blank=True, default='')
    state_region = models.CharField(max_length=100, blank=True, default='',
        help_text="State, region, or province")
    postal_code = models.CharField(max_length=20, blank=True, default='')

    id_type = models.CharField(max_length=20, choices=ID_TYPE_CHOICES, blank=True, default='')
    id_number = models.CharField(max_length=50, blank=True, default='',
        help_text="ID document number for legal guardianship verification")

    is_primary_contact = models.BooleanField(default=False,
        help_text="Mark as the main contact for this student")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=['student', 'id_type', 'id_number'],
                condition=~models.Q(id_number=''),
                name='unique_guardian_id_per_student',
            )
        ]
        ordering = ['-is_primary_contact', 'last_name']

    def __str__(self):
        return (
            f"{self.get_relationship_display()} of "
            f"{self.student.get_full_name() or self.student.email} — "
            f"{self.first_name} {self.last_name}"
        )

    def clean(self):
        """
        Ensure a guardian's email is never the same as their linked student's email.
        This runs on full_clean() / admin saves / any caller that invokes clean().
        """
        if self.email and self.student_id:
            student_email = (
                self.student.email
                if hasattr(self, '_student_cache') or self.student_id
                else None
            )
            try:
                student_email = CustomUser.objects.filter(
                    pk=self.student_id
                ).values_list('email', flat=True).first()
            except Exception:
                student_email = None

            if student_email and self.email.lower() == student_email.lower():
                raise ValidationError(
                    {'email': (
                        "A guardian's email cannot be the same as the student's email. "
                        f"Please use a different email address for this guardian."
                    )}
                )

    def save(self, *args, **kwargs):
        self.full_clean()
        super().save(*args, **kwargs)

    @property
    def full_name(self):
        parts = [self.first_name, self.middle_name, self.last_name]
        name = ' '.join(p for p in parts if p)
        if self.suffix:
            name += f', {self.suffix}'
        return name