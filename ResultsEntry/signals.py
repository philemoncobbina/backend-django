from django.db.models.signals import post_save
from django.dispatch import receiver
from django.core.files.base import ContentFile
from .models import Result
from .utils.pdf_generator import generate_report_card_pdf
import logging

logger = logging.getLogger(__name__)

@receiver(post_save, sender=Result)
def generate_report_card_on_save(sender, instance, created, **kwargs):
    """
    Generate PDF report card when a result is saved
    Only generates for published results or when explicitly requested
    """
    # Only proceed if the result is published and has course results
    if instance.status == 'PUBLISHED' and instance.course_results.exists():
        try:
            # Check if PDF already exists and is up to date
            if instance.report_card_pdf and instance.report_card_pdf.name:
                # You might want to add logic here to check if regeneration is needed
                # For now, we'll always regenerate for published results
                pass
            
            # Generate the PDF
            pdf_content = generate_report_card_pdf(instance)
            
            # Create filename
            filename = instance.get_report_card_filename()
            
            # Save the PDF to the model
            pdf_file = ContentFile(pdf_content, name=filename)
            instance.report_card_pdf.save(filename, pdf_file, save=False)
            
            # Save the instance without triggering the signal again
            Result.objects.filter(id=instance.id).update(report_card_pdf=instance.report_card_pdf)
            
            logger.info(f"PDF report card generated successfully for result {instance.id}")
            
        except Exception as e:
            logger.error(f"Failed to generate PDF report card for result {instance.id}: {str(e)}")
