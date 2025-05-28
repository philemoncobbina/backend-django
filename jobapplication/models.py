from django.db import models
from jobposting.models import JobPost
from django.contrib.auth import get_user_model

User = get_user_model()

class JobApplication(models.Model):
    STATUS_CHOICES = [
        ('PENDING', 'Pending'),
        ('SHORTLISTED', 'Shortlisted'),
        ('REJECTED', 'Rejected'),
        ('HIRED', 'Hired'),
    ]
    
    EDUCATIONAL_LEVEL_CHOICES = [
        ('HIGH_SCHOOL', 'High School'),
        ('ASSOCIATE', 'Associate Degree'),
        ('BACHELOR', "Bachelor's Degree"),
        ('MASTER', "Master's Degree"),
        ('PHD', 'PhD'),
    ]
    
    job_post = models.ForeignKey(
        JobPost,
        on_delete=models.CASCADE,
        related_name="applications"
    )
    resume = models.FileField(upload_to="resumes/")
    status = models.CharField(
        max_length=20,
        choices=STATUS_CHOICES,
        default='PENDING'
    )
    applied_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    
    job_title = models.CharField(max_length=200, blank=True, null=True)
    job_reference_number = models.CharField(max_length=8, blank=True, null=True)
    
    first_name = models.CharField(max_length=100)
    last_name = models.CharField(max_length=100)
    email = models.EmailField(max_length=254)
    
    educational_level = models.CharField(
        max_length=20,
        choices=EDUCATIONAL_LEVEL_CHOICES,
        default='HIGH_SCHOOL'
    )
    
    # Track who last modified the application
    last_modified_by = models.ForeignKey(
        User, 
        on_delete=models.SET_NULL, 
        null=True, 
        blank=True,
        related_name='modified_applications'
    )
    
    class Meta:
        ordering = ['-applied_at']
        verbose_name = 'Job Application'
        verbose_name_plural = 'Job Applications'
        unique_together = ['job_post', 'email']
    
    def __str__(self):
        return f"{self.email} - {self.job_post.title}"
    
    def save(self, *args, **kwargs):
        if not self.job_title:
            self.job_title = self.job_post.title
        if not self.job_reference_number:
            self.job_reference_number = self.job_post.reference_number
        
        super().save(*args, **kwargs)
        
        # Update application count on JobPost
        self.job_post.update_application_count()
    
    def delete(self, *args, **kwargs):
        super().delete(*args, **kwargs)
        
        # Update application count on JobPost after deletion
        self.job_post.update_application_count()


class JobApplicationLog(models.Model):
    """
    Tracks all changes made to a JobApplication instance.
    """
    application = models.ForeignKey(
        JobApplication, 
        on_delete=models.CASCADE,
        related_name='logs'
    )
    user = models.ForeignKey(
        User, 
        on_delete=models.SET_NULL, 
        null=True,
        blank=True
    )
    user_email = models.EmailField(max_length=255, blank=True)  # Track the email separately
    changed_fields = models.TextField()  # Store the fields that were changed
    timestamp = models.DateTimeField(auto_now_add=True)
    
    class Meta:
        ordering = ['-timestamp']
        verbose_name = 'Job Application Log'
        verbose_name_plural = 'Job Application Logs'
    
    def __str__(self):
        user_email = self.user_email if self.user_email else 'Unknown user'
        log_str = f"Log for {self.application} by {user_email} at {self.timestamp}"
        
        # Print for debugging purposes
        print(f"JobApplicationLog: {log_str}")
        
        return log_str