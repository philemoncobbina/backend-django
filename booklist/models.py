from django.db import models
from django.utils import timezone
from authapp.models import CustomUser

class BookList(models.Model):
    """
    Main model for book lists associated with specific classes and academic years
    """
    STATUS_CHOICES = (
        ('draft', 'Draft'),
        ('published', 'Published'),
        ('scheduled', 'Scheduled'),
    )
    
    title = models.CharField(max_length=255)
    academic_year = models.CharField(max_length=20, help_text="Academic year (e.g., '2024-2025')")
    class_name = models.CharField(
        max_length=10, 
        choices=CustomUser.CLASS_CHOICES,
        help_text="Class this book list is for"
    )
    status = models.CharField(max_length=10, choices=STATUS_CHOICES, default='draft')
    created_by = models.ForeignKey(CustomUser, on_delete=models.CASCADE, related_name='created_booklists')
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    scheduled_date = models.DateTimeField(null=True, blank=True, help_text="Date when book list will be automatically published")
    publish_date = models.DateTimeField(null=True, blank=True, help_text="Date when book list was actually published")
    description = models.TextField(blank=True, null=True)
    
    def __str__(self):
        return f"{self.title} - {self.class_name} ({self.academic_year})"
    
    def save(self, *args, **kwargs):
        # Check if scheduled date has passed and update status to published
        if self.status == 'scheduled' and self.scheduled_date and self.scheduled_date <= timezone.now():
            self.status = 'published'
            self.publish_date = timezone.now()
        super().save(*args, **kwargs)
    
    def check_and_update_status(self):
        """Check if scheduled date has passed and update status to published"""
        if self.status == 'scheduled' and self.scheduled_date and self.scheduled_date <= timezone.now():
            self.status = 'published'
            self.publish_date = timezone.now()
            self.save(update_fields=['status', 'publish_date'])
            return True
        return False
    
    def is_visible_to_students(self):
        """Check if this booklist should be visible to students"""
        # First check if status needs to be updated
        self.check_and_update_status()
        
        return self.status == 'published'
    
    def total_price(self):
        """Calculate the total price of all items in this book list"""
        return sum(item.total_item_price() for item in self.items.all())
    
    class Meta:
        ordering = ['-created_at', 'class_name']
        unique_together = ['academic_year', 'class_name']

class BookListItem(models.Model):
    """
    Individual items in a book list (books, supplies, etc.)
    """
    book_list = models.ForeignKey(BookList, on_delete=models.CASCADE, related_name='items', null=True, blank=True)
    name = models.CharField(max_length=255)
    description = models.TextField(blank=True, null=True)
    price = models.DecimalField(max_digits=10, decimal_places=2)
    quantity = models.PositiveIntegerField(default=1)
    is_required = models.BooleanField(default=True, help_text="Whether this item is required or optional")
    order = models.PositiveIntegerField(default=0, help_text="Order in which items appear")
    
    def __str__(self):
        return f"{self.name} ({self.book_list.class_name} - {self.book_list.academic_year})"
    
    def total_item_price(self):
        """Calculate the total price for this item (price × quantity)"""
        return self.price * self.quantity
    
    class Meta:
        ordering = ['order', 'name']


class StudentClassHistory(models.Model):
    """Track student's class history over academic years"""
    student = models.ForeignKey('authapp.CustomUser', on_delete=models.CASCADE, related_name='class_history')
    academic_year = models.CharField(max_length=20, help_text="Academic year (e.g., '2024-2025')")
    class_name = models.CharField(max_length=10, choices=CustomUser.CLASS_CHOICES)
    
    class Meta:
        ordering = ['-academic_year']
    
    def __str__(self):
        return f"{self.student.username} - {self.class_name} ({self.academic_year})"