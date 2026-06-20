import logging
from celery import shared_task, group, chain
from django.core.files.base import ContentFile

logger = logging.getLogger(__name__)


def _student_full_name(student):
    """Safe helper — works whether or not CustomUser defines get_full_name()."""
    return f"{student.first_name} {student.last_name}".strip() or student.get_username()


# ---------------------------------------------------------------------------
# Core PDF generation task
# ---------------------------------------------------------------------------

@shared_task(
    bind=True,
    max_retries=3,
    default_retry_delay=10,
    name='ResultEntry.tasks.generate_pdf_for_result',
    acks_late=True,
)
def generate_pdf_for_result(self, result_id):
    """
    Asynchronously generate a PDF report card for a single Result.

    Retries up to 3 times with a 10-second delay on transient failures.
    Returns True on success, False if the result does not exist.
    """
    from .models import Result
    from .utils.pdf_generator import generate_report_card_pdf

    logger.info(
        f"[TASK generate_pdf_for_result] START — result_id={result_id} "
        f"(attempt {self.request.retries + 1}/{self.max_retries + 1})"
    )

    try:
        result = Result.objects.select_related('student').get(id=result_id)
    except Result.DoesNotExist:
        logger.error(
            f"[TASK generate_pdf_for_result] ABORT — Result {result_id} not found."
        )
        return False

    try:
        result.refresh_from_db()

        logger.debug(
            f"[TASK generate_pdf_for_result] Generating PDF for result {result_id} — "
            f"Student: {_student_full_name(result.student)}, "
            f"Class: {result.class_name}, Term: {result.term}, Status: {result.status}"
        )

        pdf_content = generate_report_card_pdf(result)

        if not pdf_content:
            logger.error(
                f"[TASK generate_pdf_for_result] FAIL — generate_report_card_pdf "
                f"returned empty content for result {result_id}."
            )
            return False

        filename = result.get_report_card_filename()
        pdf_file = ContentFile(pdf_content, name=filename)

        # Overwrite any existing PDF file
        if result.report_card_pdf:
            try:
                result.report_card_pdf.delete(save=False)
            except Exception:
                pass  # best-effort deletion of old file

        result.report_card_pdf.save(filename, pdf_file, save=True)

        logger.info(
            f"[TASK generate_pdf_for_result] SUCCESS — PDF saved for result {result_id} "
            f"at '{result.report_card_pdf.name}'."
        )
        return True

    except Exception as exc:
        logger.error(
            f"[TASK generate_pdf_for_result] ERROR — result_id={result_id}: {exc}",
            exc_info=True,
        )
        raise self.retry(exc=exc)


# ---------------------------------------------------------------------------
# Bulk PDF regeneration
# ---------------------------------------------------------------------------

@shared_task(
    bind=True,
    name='ResultEntry.tasks.regenerate_pdfs_for_class',
    acks_late=True,
)
def regenerate_pdfs_for_class(self, class_name, term, academic_year="2023-2024",
                               exclude_result_id=None, changed_result_ids=None):
    """
    Dispatch individual generate_pdf_for_result tasks for every Result
    in a given class / term / academic_year combination.

    This task acts as a fan-out coordinator; the actual PDF work is done
    by generate_pdf_for_result so each student's PDF is generated in
    parallel and benefits from independent retries.

    Args:
        class_name:         e.g. "JHS1"
        term:               e.g. "first"
        academic_year:      e.g. "2023-2024"
        exclude_result_id:  optional Result PK to skip (already regenerated)
        changed_result_ids: optional list of PKs whose positions changed
                            (used for log context only)
    """
    from .models import Result

    logger.info(
        f"[TASK regenerate_pdfs_for_class] START — "
        f"{class_name} / {term} / {academic_year}  "
        f"(exclude={exclude_result_id}, position_changes={changed_result_ids})"
    )

    qs = Result.objects.filter(
        class_name=class_name,
        term=term,
        academic_year=academic_year,
    ).values_list('id', flat=True)

    if exclude_result_id:
        qs = qs.exclude(id=exclude_result_id)

    result_ids = list(qs)

    if not result_ids:
        logger.info(
            f"[TASK regenerate_pdfs_for_class] No results found for "
            f"{class_name} / {term} / {academic_year} — nothing to do."
        )
        return {'dispatched': 0}

    changed_ids_set = set(changed_result_ids or [])
    for rid in result_ids:
        tag = "POSITION CHANGED" if rid in changed_ids_set else "position unchanged"
        logger.info(
            f"[TASK regenerate_pdfs_for_class] Dispatching PDF task for "
            f"result_id={rid} ({tag})."
        )

    # Fan out — each result gets its own retryable task
    job = group(generate_pdf_for_result.s(rid) for rid in result_ids)
    job.apply_async()

    logger.info(
        f"[TASK regenerate_pdfs_for_class] DISPATCHED {len(result_ids)} PDF tasks "
        f"for {class_name} / {term} / {academic_year}."
    )
    return {'dispatched': len(result_ids)}


# ---------------------------------------------------------------------------
# Position recalculation + PDF fan-out (chainable)
# ---------------------------------------------------------------------------

@shared_task(
    bind=True,
    name='ResultEntry.tasks.recalculate_positions_and_regenerate_pdfs',
    acks_late=True,
)
def recalculate_positions_and_regenerate_pdfs(
    self,
    class_name,
    term,
    academic_year="2023-2024",
    exclude_result_id=None,
):
    """
    1. Recalculate overall and per-course positions for the class/term.
    2. Fan-out PDF regeneration for every result in the class/term.

    Designed to be called after any score change or status change that
    could affect rankings.
    """
    from .views import PositionCalculator

    logger.info(
        f"[TASK recalculate_positions_and_regenerate_pdfs] START — "
        f"{class_name} / {term} / {academic_year}"
    )

    try:
        changed_result_ids = PositionCalculator.recalculate_positions(
            class_name, term, academic_year
        )
        logger.info(
            f"[TASK recalculate_positions_and_regenerate_pdfs] Positions recalculated — "
            f"{len(changed_result_ids)} result(s) changed: {changed_result_ids}"
        )
    except Exception as exc:
        logger.error(
            f"[TASK recalculate_positions_and_regenerate_pdfs] Position calculation failed: {exc}",
            exc_info=True,
        )
        raise

    # Fan out PDFs
    regenerate_pdfs_for_class.delay(
        class_name,
        term,
        academic_year,
        exclude_result_id=exclude_result_id,
        changed_result_ids=changed_result_ids,
    )

    logger.info(
        f"[TASK recalculate_positions_and_regenerate_pdfs] COMPLETE — "
        f"{class_name} / {term} / {academic_year}"
    )
    return {'changed_result_ids': changed_result_ids}


# ---------------------------------------------------------------------------
# Auto-publish scheduled results (intended for a periodic Celery Beat task)
# ---------------------------------------------------------------------------

@shared_task(
    bind=True,
    name='ResultEntry.tasks.auto_publish_scheduled_results',
    acks_late=True,
)
def auto_publish_scheduled_results(self):
    """
    Publish any Result whose status is SCHEDULED and whose scheduled_date
    has passed. Sends email notifications and triggers PDF regeneration.

    Register this in CELERY_BEAT_SCHEDULE to run every minute (or as needed).
    """
    from django.utils import timezone
    from .models import Result
    from .views import EmailNotifier, PositionCalculator

    now = timezone.now()

    scheduled_qs = Result.objects.filter(
        status='SCHEDULED',
        scheduled_date__lte=now,
    ).select_related('student')

    result_ids = list(scheduled_qs.values_list('id', flat=True))

    if not result_ids:
        logger.debug("[TASK auto_publish_scheduled_results] No scheduled results due.")
        return {'published': 0}

    logger.info(
        f"[TASK auto_publish_scheduled_results] Found {len(result_ids)} result(s) to publish."
    )

    updated_classes = set()
    published_count = 0

    for result in scheduled_qs:
        try:
            result.status = 'PUBLISHED'
            result.published_date = now
            result.save(update_fields=['status', 'published_date'])

            logger.info(
                f"[TASK auto_publish_scheduled_results] Published result {result.id} — "
                f"Student: {_student_full_name(result.student)}"
            )

            EmailNotifier.send_result_published(result)
            updated_classes.add((result.class_name, result.term, result.academic_year))
            published_count += 1

        except Exception as exc:
            logger.error(
                f"[TASK auto_publish_scheduled_results] Failed to publish result {result.id}: {exc}",
                exc_info=True,
            )

    # Recalculate positions and regenerate PDFs for each affected class/term
    for class_name, term, academic_year in updated_classes:
        recalculate_positions_and_regenerate_pdfs.delay(class_name, term, academic_year)

    logger.info(
        f"[TASK auto_publish_scheduled_results] COMPLETE — "
        f"Published {published_count} result(s) across {len(updated_classes)} class/term(s)."
    )
    return {'published': published_count}


# ---------------------------------------------------------------------------
# Bulk status update task
# ---------------------------------------------------------------------------

@shared_task(
    bind=True,
    name='ResultEntry.tasks.bulk_update_status_task',
    acks_late=True,
)
def bulk_update_status_task(self, class_name, term, new_status,
                             scheduled_date_iso=None, user_email=None):
    """
    Performs a bulk status update for all students in a class/term,
    then recalculates positions and regenerates all PDFs.

    Args:
        class_name:           Target class
        term:                 Target term
        new_status:           'DRAFT' | 'SCHEDULED' | 'PUBLISHED'
        scheduled_date_iso:   ISO-format string for scheduled_date (if SCHEDULED)
        user_email:           Email of the user who triggered the update (for logs)
    """
    from django.utils import timezone
    from .models import Result, ResultChangeLog
    from authapp.models import CustomUser
    from .views import EmailNotifier

    logger.info(
        f"[TASK bulk_update_status_task] START — "
        f"{class_name} / {term} → {new_status} (triggered by {user_email})"
    )

    students = CustomUser.objects.filter(
        class_name=class_name, role='student'
    ).values_list('id', flat=True)

    results_qs = Result.objects.filter(
        class_name=class_name,
        term=term,
        student_id__in=students,
    )

    if new_status == 'PUBLISHED':
        results_qs = results_qs.exclude(status='PUBLISHED')

    results = list(results_qs.select_related('student'))

    if not results:
        logger.info(
            f"[TASK bulk_update_status_task] No results to update for "
            f"{class_name} / {term}."
        )
        return {'updated': 0}

    updated_count = 0
    now = timezone.now()

    scheduled_date = None
    if scheduled_date_iso:
        from datetime import datetime
        import pytz
        scheduled_date = datetime.fromisoformat(scheduled_date_iso)
        if scheduled_date.tzinfo is None:
            scheduled_date = pytz.utc.localize(scheduled_date)

    for result in results:
        old_status = result.status

        result.status = new_status
        if new_status == 'PUBLISHED':
            result.published_date = now
        elif new_status == 'SCHEDULED':
            result.scheduled_date = scheduled_date
        elif new_status == 'DRAFT':
            result.scheduled_date = None
            result.published_date = None

        result.save(update_fields=['status', 'published_date', 'scheduled_date'])
        updated_count += 1

        if old_status != 'PUBLISHED' and new_status == 'PUBLISHED':
            EmailNotifier.send_result_published(result)

        if user_email:
            ResultChangeLog.objects.create(
                result=result,
                changed_by=user_email,
                field_name="status (bulk update)",
                previous_value=old_status,
                new_value=new_status,
            )

        logger.info(
            f"[TASK bulk_update_status_task] Updated result {result.id} — "
            f"Student: {_student_full_name(result.student)}, "
            f"{old_status} → {new_status}"
        )

    logger.info(
        f"[TASK bulk_update_status_task] {updated_count} result(s) updated. "
        f"Triggering position recalculation + PDF regeneration..."
    )

    recalculate_positions_and_regenerate_pdfs.delay(class_name, term)

    logger.info(
        f"[TASK bulk_update_status_task] COMPLETE — {updated_count} updated "
        f"for {class_name} / {term}."
    )
    return {'updated': updated_count}