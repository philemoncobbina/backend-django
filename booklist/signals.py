# signals.py
from django.db.models.signals import post_save
from django.dispatch import receiver
from django.conf import settings
from .models import StudentClassHistory, AcademicYear

@receiver(post_save, sender=settings.AUTH_USER_MODEL)
def create_student_class_history(sender, instance, created, **kwargs):
    """
    Create a class history entry when a student is created or their class is updated
    """
    if not instance.is_student or not instance.class_name:
        return
    
    try:
        current_year = AcademicYear.objects.get(is_current=True)
        
        # Check if there's already a history entry for this student and academic year
        history_exists = StudentClassHistory.objects.filter(
            student=instance,
            academic_year=current_year
        ).exists()
        
        if not history_exists:
            StudentClassHistory.objects.create(
                student=instance,
                class_name=instance.class_name,
                academic_year=current_year
            )
    except AcademicYear.DoesNotExist:
        # No current academic year set
        pass