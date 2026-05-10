from django.db import models
from django.core.validators import MaxValueValidator, MinValueValidator
from authapp.models import CustomUser
from django.utils import timezone
from django.core.exceptions import ValidationError
from django.db import transaction
import os

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

    # Teacher information
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


class ClassSize(models.Model):
    """Tracks the total number of students in each class for each term"""
    class_name = models.CharField(
        max_length=10, 
        choices=CustomUser.CLASS_CHOICES,
        help_text="Class name"
    )
    term = models.CharField(
        max_length=10, 
        choices=(
            ('first', 'First Term'),
            ('second', 'Second Term'),
            ('third', 'Third Term'),
        ),
        help_text="Academic term"
    )
    academic_year = models.CharField(
        max_length=20, 
        default="2023-2024",
        help_text="Academic year (e.g., '2024-2025')"
    )
    total_students = models.PositiveIntegerField(
        default=0,
        help_text="Total number of students in this class for this term"
    )
    last_updated = models.DateTimeField(auto_now=True)
    created_at = models.DateTimeField(auto_now_add=True)
    
    def __str__(self):
        return f"{self.class_name} - {self.get_term_display()} ({self.academic_year}): {self.total_students} students"
    
    class Meta:
        unique_together = ('class_name', 'term', 'academic_year')
        ordering = ['class_name', 'term', 'academic_year']
        verbose_name = "Class Size"
        verbose_name_plural = "Class Sizes"
    
    @classmethod
    def update_class_size(cls, class_name, term, academic_year="2023-2024"):
        """Update the total number of students for a specific class and term"""
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
            class_size.save()
        
        return class_size
    
    @classmethod
    def get_class_size(cls, class_name, term, academic_year="2023-2024"):
        """Get the total number of students for a specific class and term"""
        try:
            class_size = cls.objects.get(
                class_name=class_name,
                term=term,
                academic_year=academic_year
            )
            return class_size.total_students
        except cls.DoesNotExist:
            class_size = cls.update_class_size(class_name, term, academic_year)
            return class_size.total_students


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
    
    academic_year = models.CharField(max_length=20, default="2023-2024", help_text="Academic year (e.g., '2024-2025')")
    overall_position = models.PositiveIntegerField(null=True, blank=True, help_text="Overall position in class")
    class_teacher_remarks = models.TextField(blank=True, null=True, help_text="Class teacher's general remarks")
    promoted_to = models.CharField(
        max_length=10, 
        choices=CustomUser.CLASS_CHOICES,
        blank=True, 
        null=True,
        help_text="Class student is promoted to"
    )
    next_term_begins = models.DateField(null=True, blank=True, help_text="Date when next term begins")
    days_present = models.PositiveIntegerField(default=0, help_text="Number of days student was present")
    days_absent = models.PositiveIntegerField(default=0, help_text="Number of days student was absent")
    report_card_pdf = models.FileField(
        upload_to='report_cards/',
        null=True,
        blank=True,
        help_text="Generated PDF report card"
    )
    
    def get_report_card_filename(self):
        """Generate a standardized filename for the report card"""
        safe_student_name = "".join(c for c in f"{self.student.first_name}_{self.student.last_name}" if c.isalnum() or c in "._- ")
        return f"{safe_student_name}_{self.class_name}_{self.term}_{self.academic_year.replace('-', '_')}.pdf"
    
    @property
    def is_published(self):
        return self.status == 'PUBLISHED'
    
    @property
    def total_days(self):
        return self.days_present + self.days_absent
    
    @property
    def attendance_percentage(self):
        if self.total_days == 0:
            return 0.0
        return round((self.days_present / self.total_days) * 100, 2)
    
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
        return ClassSize.get_class_size(self.class_name, self.term, self.academic_year)
    
    @property
    def position_context(self):
        total_students = self.total_students_in_class
        if self.overall_position and total_students > 0:
            return f"{self.overall_position}/{total_students}"
        return "N/A"
    
    def calculate_positions(self):
        with transaction.atomic():
            ClassSize.update_class_size(self.class_name, self.term, self.academic_year)
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
        previous_avg_score = None
        
        for i, result in enumerate(sorted_results):
            if (previous_total_score is not None and 
                (result.total_score < previous_total_score or 
                 (result.total_score == previous_total_score and result.average_score < previous_avg_score))):
                current_position = i + 1
            
            if result.overall_position != current_position:
                result.overall_position = current_position
                result.save(update_fields=['overall_position'], calculate_positions=False)
            
            previous_total_score = result.total_score
            previous_avg_score = result.average_score

    def _calculate_all_course_positions(self):
        class_courses = ClassCourse.objects.filter(
            class_name=self.class_name,
            term=self.term
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
            
            for i, course_result in enumerate(sorted_course_results):
                if previous_score is not None and course_result.total_score < previous_score:
                    current_position = i + 1
                
                if course_result.position != current_position:
                    course_result.position = current_position
                    course_result.save(update_fields=['position'])
                
                previous_score = course_result.total_score
    
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
        
        if self.promoted_to and self.promoted_to == self.class_name:
            raise ValidationError("Student cannot be promoted to the same class")
        
        if self.overall_position:
            total_students = ClassSize.get_class_size(self.class_name, self.term, self.academic_year)
            if self.overall_position > total_students:
                raise ValidationError(
                    f"Position {self.overall_position} cannot be greater than total students {total_students} in class {self.class_name}"
                )
    
    def save(self, *args, **kwargs):
        self.full_clean()
        
        current_time = timezone.now()
        
        if self.status == 'SCHEDULED' and self.scheduled_date and self.scheduled_date <= current_time:
            self.status = 'PUBLISHED'
            self.published_date = current_time
        
        if self.status == 'PUBLISHED' and not self.published_date:
            self.published_date = current_time
        
        calculate_positions = kwargs.pop('calculate_positions', True)
        
        super().save(*args, **kwargs)
        
        ClassSize.update_class_size(self.class_name, self.term, self.academic_year)
        
        if calculate_positions and self.course_results.exists():
            self.calculate_positions()
    
    def __str__(self):
        return f"{self.student.get_full_name()} - {self.class_name} - {self.get_term_display()} ({self.status})"
    
    class Meta:
        ordering = ['student__last_name', 'student__first_name', 'term']
        unique_together = ('student', 'class_name', 'term', 'academic_year')
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
    position = models.PositiveIntegerField(null=True, blank=True, help_text="Position in this course for the class")
    
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
        if not self.class_course:
            raise ValidationError("Class course must be specified")
        if self.class_score is None:
            raise ValidationError("Class score must be specified")
        if self.exam_score is None:
            raise ValidationError("Exam score must be specified")
            
        if self.result and self.class_course:
            if self.class_course.class_name != self.result.class_name:
                raise ValidationError("Class course class name doesn't match result class name")
            if self.class_course.term != self.result.term:
                raise ValidationError("Class course term doesn't match result term")
        
        if self.position and self.result:
            total_students = ClassSize.get_class_size(
                self.result.class_name, 
                self.result.term, 
                self.result.academic_year
            )
            if self.position > total_students:
                raise ValidationError(
                    f"Course position {self.position} cannot be greater than total students {total_students} in class"
                )
    
    def save(self, *args, **kwargs):
        super().save(*args, **kwargs)
        if hasattr(self, 'result') and self.result:
            self.result.calculate_positions()
    
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