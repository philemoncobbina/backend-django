from django.contrib.auth.models import AbstractBaseUser, BaseUserManager, PermissionsMixin
from django.db import models
from django.utils import timezone

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
        
    def create_student(self, email, first_name, last_name, password, index_number, class_name, **extra_fields):
        # Generate username from index number if not provided
        username = index_number.lower()  # or any other logic you prefer
            
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
        ('student', 'Student'),  # Added student role
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
    
    # Student-specific fields
    index_number = models.CharField(max_length=20, unique=True, null=True, blank=True)
    class_name = models.CharField(max_length=10, choices=CLASS_CHOICES, null=True, blank=True)
    
    objects = CustomUserManager()
    
    USERNAME_FIELD = 'email'
    REQUIRED_FIELDS = ['username']
    
    def __str__(self):
        return self.email
    
    @property
    def is_student(self):
        return self.role == 'student'
        
    def update_class(self, new_class, academic_year=None):
        """
        Update user's class and save the previous class in history
        """
        # Import here to avoid circular imports
        from django.apps import apps
        AcademicYear = apps.get_model('booklistapp', 'AcademicYear')
        StudentClassHistory = apps.get_model('booklistapp', 'StudentClassHistory')
        
        # Get current academic year if not provided
        if not academic_year:
            academic_year = AcademicYear.objects.filter(is_current=True).first()
            if not academic_year:
                raise ValueError("No current academic year found")
        
        # If user already has a class, save it to history
        if self.class_name:
            # Only save to history if the class is changing
            if self.class_name != new_class:
                # Check if entry already exists
                history_entry, created = StudentClassHistory.objects.get_or_create(
                    student=self,
                    academic_year=academic_year,
                    defaults={'class_name': self.class_name}
                )
                if not created:
                    history_entry.class_name = self.class_name
                    history_entry.save()
        
        # Update the user's class
        self.class_name = new_class
        self.save(update_fields=['class_name'])