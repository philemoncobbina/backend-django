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

    CLASS_CHOICES = (
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

    objects = CustomUserManager()

    USERNAME_FIELD = 'email'
    REQUIRED_FIELDS = ['username']

    def __str__(self):
        return self.email

    @property
    def is_student(self):
        return self.role == 'student'

    def update_class(self, new_class, academic_year=None):
        """Update student's class and persist the previous class in history."""
        from django.apps import apps
        AcademicYear = apps.get_model('booklistapp', 'AcademicYear')
        StudentClassHistory = apps.get_model('booklistapp', 'StudentClassHistory')

        if not academic_year:
            academic_year = AcademicYear.objects.filter(is_current=True).first()
            if not academic_year:
                raise ValueError("No current academic year found")

        if self.class_name and self.class_name != new_class:
            history_entry, created = StudentClassHistory.objects.get_or_create(
                student=self,
                academic_year=academic_year,
                defaults={'class_name': self.class_name}
            )
            if not created:
                history_entry.class_name = self.class_name
                history_entry.save()

        self.class_name = new_class
        self.save(update_fields=['class_name'])


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