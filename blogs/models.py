from django.db import models
from django.utils import timezone
from django.conf import settings
from authapp.models import CustomUser

class Category(models.Model):
    name = models.CharField(max_length=100, unique=True)
    created_by = models.ForeignKey(
        CustomUser,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='created_categories'
    )
    created_at = models.DateTimeField(auto_now_add=True)
    
    def __str__(self):
        return self.name
    
    class Meta:
        verbose_name_plural = "Categories"
        ordering = ['name']

class Author(models.Model):
    name = models.CharField(max_length=200, unique=True)
    bio = models.TextField(blank=True)
    profile_image = models.ImageField(upload_to='authors/', null=True, blank=True)
    created_by = models.ForeignKey(
        CustomUser,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='created_authors'
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    
    def __str__(self):
        return self.name
    
    class Meta:
        ordering = ['name']

class BlogPost(models.Model):
    STATUS_CHOICES = [
        ('DRAFT', 'Draft'),
        ('SCHEDULED', 'Scheduled'),
        ('PUBLISHED', 'Published'),
    ]
    
    title = models.CharField(max_length=500)
    slug = models.SlugField(max_length=500, unique=True, blank=True)
    image = models.ImageField(upload_to='blog_images/')
    categories = models.ManyToManyField(Category, related_name='blog_posts')
    author = models.ForeignKey(Author, on_delete=models.CASCADE, related_name='blog_posts')
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='DRAFT')
    scheduled_date = models.DateTimeField(null=True, blank=True)
    published_date = models.DateTimeField(null=True, blank=True)
    created_by = models.ForeignKey(
        CustomUser,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='created_blog_posts'
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    
    def save(self, *args, **kwargs):
        if not self.slug:
            from django.utils.text import slugify
            import uuid
            base_slug = slugify(self.title)[:450]
            unique_slug = f"{base_slug}-{uuid.uuid4().hex[:8]}"
            self.slug = unique_slug
        
        # Update status based on dates
        now = timezone.now()
        
        if self.status == 'SCHEDULED' and self.scheduled_date and now >= self.scheduled_date:
            self.status = 'PUBLISHED'
            if not self.published_date:
                self.published_date = now
        
        elif self.status == 'PUBLISHED' and not self.published_date:
            self.published_date = now
        
        super().save(*args, **kwargs)
    
    def __str__(self):
        return f"{self.title} ({self.status})"
    
    class Meta:
        ordering = ['-created_at']
    
    def check_and_update_status(self):
        """Check if scheduled post should be published"""
        if self.status == 'SCHEDULED' and self.scheduled_date and timezone.now() >= self.scheduled_date:
            self.status = 'PUBLISHED'
            if not self.published_date:
                self.published_date = timezone.now()
            self.save()
            return True
        return False

class BlogText(models.Model):
    blog_post = models.ForeignKey(BlogPost, on_delete=models.CASCADE, related_name='text_blocks')
    content = models.TextField()
    order = models.PositiveIntegerField(default=0, help_text="Order in which this text block appears")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    
    class Meta:
        ordering = ['order']
        verbose_name = 'Blog Text Block'
        verbose_name_plural = 'Blog Text Blocks'
        unique_together = ['blog_post', 'order']
    
    def __str__(self):
        return f"Text block {self.order} for {self.blog_post.title[:50]}..."