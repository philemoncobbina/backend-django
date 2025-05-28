from django.db import models
from django.core.validators import MaxValueValidator, MinValueValidator
from authapp.models import CustomUser
from django.utils import timezone
from django.core.exceptions import ValidationError

class Course(models.Model):
    """Represents a course/subject"""
    name = models.CharField(max_length=100, unique=True)
    code = models.CharField(max_length=10, unique=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    created_by = models.ForeignKey(CustomUser, on_delete=models.CASCADE, related_name='created_courses')
    
    def __str__(self):
        return f"{self.name} ({self.code})"
    
    class Meta:
        ordering = ['name']

class ClassCourse(models.Model):
    """Assigns courses to specific classes"""
    course = models.ForeignKey(Course, on_delete=models.CASCADE, related_name='class_assignments')
    class_name = models.CharField(
        max_length=10, 
        choices=CustomUser.CLASS_CHOICES,
        help_text="Class this course is assigned to"
    )
    term = models.CharField(
        max_length=10, 
        choices=(
            ('first', 'First Term'),
            ('second', 'Second Term'),
            ('third', 'Third Term'),
        ),
        null=False,
        blank=False,
        
    )
    is_active = models.BooleanField(default=True)
    
    def __str__(self):
        return f"{self.course.name} - {self.class_name} ({self.get_term_display()})"
    
    class Meta:
        unique_together = ('course', 'class_name', 'term')
        ordering = ['class_name', 'term', 'course__name']
        
    def clean(self):
        if ClassCourse.objects.filter(
            course=self.course, 
            class_name=self.class_name, 
            term=self.term
        ).exclude(pk=self.pk).exists():
            raise ValidationError(f"This course is already assigned to class {self.class_name} in {self.get_term_display()}")

class Result(models.Model):
    """Represents a complete result for a student in a term"""
    STATUS_CHOICES = [
        ('DRAFT', 'Draft'),
        ('SCHEDULED', 'Scheduled'),
        ('PUBLISHED', 'Published'),
    ]
    
    student = models.ForeignKey(CustomUser, on_delete=models.CASCADE, related_name='results')
    class_name = models.CharField(max_length=10, choices=CustomUser.CLASS_CHOICES)
    term = models.CharField(max_length=10, choices=ClassCourse._meta.get_field('term').choices)
    status = models.CharField(max_length=10, choices=STATUS_CHOICES, default='DRAFT')
    scheduled_date = models.DateTimeField(null=True, blank=True)
    published_date = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    
    @property
    def is_published(self):
        return self.status == 'PUBLISHED'
    
    def clean(self):
        if self.status in ['PUBLISHED', 'SCHEDULED']:
            if not self.student:
                raise ValidationError("Student must be specified before publishing or scheduling results")
            if not self.class_name:
                raise ValidationError("Class name must be specified before publishing or scheduling results")
            if not self.term:
                raise ValidationError("Term must be specified before publishing or scheduling results")
            
            if self.status == 'SCHEDULED' and not self.scheduled_date:
                raise ValidationError("Scheduled date must be specified when setting status to Scheduled")
    
    def save(self, *args, **kwargs):
        self.full_clean()
        
        current_time = timezone.now()
        
        if self.status == 'SCHEDULED' and self.scheduled_date and self.scheduled_date <= current_time:
            self.status = 'PUBLISHED'
            self.published_date = current_time
        
        if self.status == 'PUBLISHED' and not self.published_date:
            self.published_date = current_time
            
        super().save(*args, **kwargs)
    
    def __str__(self):
        return f"{self.student.get_full_name()} - {self.class_name} - {self.get_term_display()} ({self.status})"
    
    class Meta:
        ordering = ['student__last_name', 'student__first_name', 'term']
        unique_together = ('student', 'class_name', 'term')
        verbose_name = "Result"
        verbose_name_plural = "Results"

class CourseResult(models.Model):
    """Individual course results within a result"""
    GRADE_CHOICES = (
        ('A', 'Excellent (70-100)'),
        ('B', 'Very Good (60-69)'),
        ('C', 'Good (50-59)'),
        ('D', 'Fair (45-49)'),
        ('E', 'Pass (40-44)'),
        ('F', 'Fail (0-39)'),
    )
    
    result = models.ForeignKey(Result, on_delete=models.CASCADE, related_name='course_results')
    class_course = models.ForeignKey(ClassCourse, on_delete=models.CASCADE, related_name='course_results')
    class_score = models.DecimalField(
        max_digits=5, 
        decimal_places=2,
        validators=[MinValueValidator(0), MaxValueValidator(40)],
        help_text="Class score (max 40)"
    )
    exam_score = models.DecimalField(
        max_digits=5, 
        decimal_places=2,
        validators=[MinValueValidator(0), MaxValueValidator(60)],
        help_text="Exam score (max 60)"
    )
    remarks = models.CharField(max_length=255, blank=True, null=True)
    
    @property
    def total_score(self):
        return float(self.class_score) + float(self.exam_score)
    
    @property
    def grade(self):
        total = self.total_score
        
        if total >= 70:
            return 'A'
        elif total >= 60:
            return 'B'
        elif total >= 50:
            return 'C'
        elif total >= 45:
            return 'D'
        elif total >= 40:
            return 'E'
        else:
            return 'F'
    
    def clean(self):
        if not self.class_course:
            raise ValidationError("Class course must be specified")
        if self.class_score is None:
            raise ValidationError("Class score must be specified")
        if self.exam_score is None:
            raise ValidationError("Exam score must be specified")
            
        # Ensure the class course matches the result's term and class
        if self.class_course.class_name != self.result.class_name:
            raise ValidationError("Class course class name doesn't match result class name")
        if self.class_course.term != self.result.term:
            raise ValidationError("Class course term doesn't match result term")
    
    def __str__(self):
        return f"{self.result} - {self.class_course.course.name} - {self.total_score} ({self.grade})"
    
    class Meta:
        ordering = ['class_course__course__name']
        unique_together = ('result', 'class_course')
        verbose_name = "Course Result"
        verbose_name_plural = "Course Results"

class ResultChangeLog(models.Model):
    """Tracks changes made to results"""
    result = models.ForeignKey(Result, on_delete=models.CASCADE, related_name='change_logs')
    # Changed from ForeignKey to EmailField to store user's email directly
    changed_by = models.EmailField(max_length=255)
    changed_at = models.DateTimeField(auto_now_add=True)
    field_name = models.CharField(max_length=50)
    previous_value = models.TextField(null=True, blank=True)
    new_value = models.TextField(null=True, blank=True)
    
    def __str__(self):
        return f"{self.field_name} changed from {self.previous_value} to {self.new_value} by {self.changed_by}"
    
    class Meta:
        ordering = ['-changed_at']
        verbose_name = "Result Change Log"
        verbose_name_plural = "Result Change Logs"