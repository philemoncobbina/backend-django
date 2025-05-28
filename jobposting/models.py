from django.db import models
from django.utils import timezone
from django.db.models import Max, Count
from authapp.models import CustomUser

class JobPost(models.Model):
    STATUS_CHOICES = [
        ('DRAFT', 'Draft'),
        ('SCHEDULED', 'Scheduled'),
        ('PUBLISHED', 'Published'),
    ]
    
    reference_number = models.CharField(
        max_length=8,
        unique=True,
        null=True,
        blank=True,
        editable=False
    )
    
    title = models.CharField(max_length=200)
    description = models.TextField()
    requirements = models.TextField()
    location = models.CharField(max_length=100)
    salary_range = models.CharField(max_length=100)
    status = models.CharField(
        max_length=20,
        choices=STATUS_CHOICES,
        default='DRAFT'
    )
    
    created_by = models.ForeignKey(
        CustomUser,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="job_posts"
    )
    created_by_email = models.EmailField(max_length=254, null=True, blank=True)
    
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    scheduled_date = models.DateTimeField(null=True, blank=True)
    published_date = models.DateTimeField(null=True, blank=True)

    # New field to store the number of applicants
    applications_count = models.PositiveIntegerField(default=0)

    class Meta:
        ordering = ['-created_at']
        verbose_name = 'Job Post'
        verbose_name_plural = 'Job Posts'
    
    def save(self, *args, **kwargs):
        if not self.reference_number:
            last_ref_num = JobPost.objects.aggregate(Max('reference_number'))['reference_number__max']
            if last_ref_num:
                try:
                    last_num = int(last_ref_num[2:])
                    new_num = last_num + 1
                except (ValueError, IndexError):
                    new_num = 1
            else:
                new_num = 1
            self.reference_number = f'RF{new_num:06d}'
        
        if self.created_by and not self.created_by_email:
            self.created_by_email = self.created_by.email
            
        super().save(*args, **kwargs)

    def update_application_count(self):
        """Update the count of job applications for this job post."""
        self.applications_count = self.applications.count()
        self.save(update_fields=['applications_count'])
    
    def __str__(self):
        return f"{self.reference_number or 'No Reference'} - {self.title}"

class JobPostLog(models.Model):
    job_post = models.ForeignKey(JobPost, on_delete=models.CASCADE, related_name='logs')
    user = models.ForeignKey(CustomUser, on_delete=models.SET_NULL, null=True)
    user_email = models.EmailField(max_length=255, blank=True)
    changed_fields = models.TextField()  # Store the fields that were changed
    timestamp = models.DateTimeField(auto_now_add=True)
    action_type = models.CharField(max_length=50, choices=[
        ('CREATE', 'Create'),
        ('UPDATE', 'Update'),
        ('STATUS_CHANGE', 'Status Change'),
        ('PUBLISH', 'Publish'),
        ('SCHEDULE', 'Schedule')
    ])

    def __str__(self):
        return f"Log for JobPost {self.job_post.reference_number} by {self.user_email} at {self.timestamp}"