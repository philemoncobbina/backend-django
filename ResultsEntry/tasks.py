import logging
from datetime import datetime

import pytz
from celery import shared_task, group
from django.core.files.base import ContentFile
from django.db import transaction
from django.utils import timezone

logger = logging.getLogger(__name__)


def _student_full_name(student):
    """
    Safe helper that works whether or not CustomUser defines get_full_name().
    """
    if not student:
        return ""

    full_name = f"{student.first_name} {student.last_name}".strip()

    if full_name:
        return full_name

    if hasattr(student, "get_username"):
        return student.get_username()

    return str(student)


def _parse_scheduled_date(scheduled_date_iso):
    """
    Convert an ISO datetime string into an aware datetime.
    """
    if not scheduled_date_iso:
        return None

    if isinstance(scheduled_date_iso, datetime):
        scheduled_date = scheduled_date_iso
    else:
        scheduled_date = datetime.fromisoformat(
            str(scheduled_date_iso).replace("Z", "+00:00")
        )

    if scheduled_date.tzinfo is None:
        scheduled_date = pytz.utc.localize(scheduled_date)

    return scheduled_date


# ---------------------------------------------------------------------------
# Position calculation helpers
# ---------------------------------------------------------------------------

def recalculate_positions_for_class(class_name, term, academic_year="2023-2024"):
    """
    Recalculate overall result positions and per-course positions for a class,
    term, and academic year.

    This replaces the old dependency on PositionCalculator inside views.py.
    """
    from .models import Result, CourseResult, ClassCourse, ClassSize

    changed_result_ids = []

    with transaction.atomic():
        ClassSize.update_class_size(
            class_name=class_name,
            term=term,
            academic_year=academic_year,
        )

        results = list(
            Result.objects.filter(
                class_name=class_name,
                term=term,
                academic_year=academic_year,
            )
            .select_related("student")
            .prefetch_related("course_results")
        )

        if not results:
            logger.info(
                "[recalculate_positions_for_class] No results found for %s / %s / %s.",
                class_name,
                term,
                academic_year,
            )
            return changed_result_ids

        sorted_results = sorted(
            results,
            key=lambda result: (result.total_score, result.average_score),
            reverse=True,
        )

        result_updates = []
        current_position = 1
        previous_total_score = None
        previous_average_score = None

        for index, result in enumerate(sorted_results):
            if (
                previous_total_score is not None
                and (
                    result.total_score < previous_total_score
                    or (
                        result.total_score == previous_total_score
                        and result.average_score < previous_average_score
                    )
                )
            ):
                current_position = index + 1

            if result.overall_position != current_position:
                result.overall_position = current_position
                result_updates.append(result)
                changed_result_ids.append(result.id)

            previous_total_score = result.total_score
            previous_average_score = result.average_score

        if result_updates:
            Result.objects.bulk_update(result_updates, ["overall_position"])

        class_courses = ClassCourse.objects.filter(
            class_name=class_name,
            term=term,
            is_active=True,
        )

        for class_course in class_courses:
            course_results = list(
                CourseResult.objects.filter(
                    class_course=class_course,
                    result__class_name=class_name,
                    result__term=term,
                    result__academic_year=academic_year,
                )
                .select_related("result", "class_course", "class_course__course")
            )

            if not course_results:
                continue

            sorted_course_results = sorted(
                course_results,
                key=lambda course_result: course_result.total_score,
                reverse=True,
            )

            course_result_updates = []
            current_course_position = 1
            previous_score = None

            for index, course_result in enumerate(sorted_course_results):
                if previous_score is not None and course_result.total_score < previous_score:
                    current_course_position = index + 1

                if course_result.position != current_course_position:
                    course_result.position = current_course_position
                    course_result_updates.append(course_result)

                previous_score = course_result.total_score

            if course_result_updates:
                CourseResult.objects.bulk_update(
                    course_result_updates,
                    ["position"],
                )

    logger.info(
        "[recalculate_positions_for_class] Completed for %s / %s / %s. "
        "Changed overall positions: %s.",
        class_name,
        term,
        academic_year,
        changed_result_ids,
    )

    return changed_result_ids


# ---------------------------------------------------------------------------
# Core PDF generation task
# ---------------------------------------------------------------------------

@shared_task(
    bind=True,
    max_retries=3,
    default_retry_delay=10,
    name="ResultEntry.tasks.generate_pdf_for_result",
    acks_late=True,
)
def generate_pdf_for_result(self, result_id):
    """
    Generate a PDF report card for a single Result.
    """
    from .models import Result
    from .utils.pdf_generator import generate_report_card_pdf

    logger.info(
        "[TASK generate_pdf_for_result] START. result_id=%s. attempt=%s/%s.",
        result_id,
        self.request.retries + 1,
        self.max_retries + 1,
    )

    try:
        result = (
            Result.objects.select_related("student")
            .prefetch_related("course_results", "course_results__class_course")
            .get(id=result_id)
        )
    except Result.DoesNotExist:
        logger.error(
            "[TASK generate_pdf_for_result] ABORT. Result %s not found.",
            result_id,
        )
        return False

    try:
        result.refresh_from_db()

        logger.info(
            "[TASK generate_pdf_for_result] Generating PDF for result %s. "
            "Student: %s. Class: %s. Term: %s. Academic year: %s.",
            result.id,
            _student_full_name(result.student),
            result.class_name,
            result.term,
            result.academic_year,
        )

        pdf_content = generate_report_card_pdf(result)

        if not pdf_content:
            logger.error(
                "[TASK generate_pdf_for_result] PDF generator returned empty content "
                "for result %s.",
                result.id,
            )
            return False

        filename = result.get_report_card_filename()
        pdf_file = ContentFile(pdf_content, name=filename)

        if result.report_card_pdf:
            try:
                result.report_card_pdf.delete(save=False)
            except Exception as exc:
                logger.warning(
                    "[TASK generate_pdf_for_result] Could not delete old PDF for "
                    "result %s: %s",
                    result.id,
                    exc,
                )

        result.report_card_pdf.save(filename, pdf_file, save=False)
        result.save(
            update_fields=["report_card_pdf", "updated_at"],
            calculate_positions=False,
        )

        logger.info(
            "[TASK generate_pdf_for_result] SUCCESS. PDF saved for result %s at %s.",
            result.id,
            result.report_card_pdf.name,
        )

        return True

    except Exception as exc:
        logger.error(
            "[TASK generate_pdf_for_result] ERROR. result_id=%s: %s",
            result_id,
            exc,
            exc_info=True,
        )
        raise self.retry(exc=exc)


# ---------------------------------------------------------------------------
# Bulk PDF regeneration
# ---------------------------------------------------------------------------

@shared_task(
    bind=True,
    name="ResultEntry.tasks.regenerate_pdfs_for_class",
    acks_late=True,
)
def regenerate_pdfs_for_class(
    self,
    class_name,
    term,
    academic_year="2023-2024",
    exclude_result_id=None,
    changed_result_ids=None,
):
    """
    Dispatch individual PDF generation tasks for every Result in a class,
    term, and academic year.
    """
    from .models import Result

    logger.info(
        "[TASK regenerate_pdfs_for_class] START. %s / %s / %s. exclude=%s. "
        "changed_result_ids=%s.",
        class_name,
        term,
        academic_year,
        exclude_result_id,
        changed_result_ids,
    )

    result_ids_queryset = Result.objects.filter(
        class_name=class_name,
        term=term,
        academic_year=academic_year,
    ).values_list("id", flat=True)

    if exclude_result_id:
        result_ids_queryset = result_ids_queryset.exclude(id=exclude_result_id)

    result_ids = list(result_ids_queryset)

    if not result_ids:
        logger.info(
            "[TASK regenerate_pdfs_for_class] No results found for %s / %s / %s.",
            class_name,
            term,
            academic_year,
        )
        return {"dispatched": 0}

    changed_ids_set = set(changed_result_ids or [])

    for result_id in result_ids:
        status_label = (
            "position changed"
            if result_id in changed_ids_set
            else "position unchanged"
        )

        logger.info(
            "[TASK regenerate_pdfs_for_class] Dispatching PDF task for result %s. %s.",
            result_id,
            status_label,
        )

    job = group(generate_pdf_for_result.s(result_id) for result_id in result_ids)
    job.apply_async()

    logger.info(
        "[TASK regenerate_pdfs_for_class] DISPATCHED %s PDF task(s) for %s / %s / %s.",
        len(result_ids),
        class_name,
        term,
        academic_year,
    )

    return {"dispatched": len(result_ids)}


# ---------------------------------------------------------------------------
# Position recalculation and PDF fan-out
# ---------------------------------------------------------------------------

@shared_task(
    bind=True,
    name="ResultEntry.tasks.recalculate_positions_and_regenerate_pdfs",
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
    Recalculate result positions and regenerate PDFs for a class, term,
    and academic year.

    This task no longer imports PositionCalculator from views.py.
    """
    logger.info(
        "[TASK recalculate_positions_and_regenerate_pdfs] START. %s / %s / %s.",
        class_name,
        term,
        academic_year,
    )

    try:
        changed_result_ids = recalculate_positions_for_class(
            class_name=class_name,
            term=term,
            academic_year=academic_year,
        )
    except Exception as exc:
        logger.error(
            "[TASK recalculate_positions_and_regenerate_pdfs] Position calculation "
            "failed for %s / %s / %s: %s",
            class_name,
            term,
            academic_year,
            exc,
            exc_info=True,
        )
        raise

    regenerate_pdfs_for_class.delay(
        class_name,
        term,
        academic_year,
        exclude_result_id=exclude_result_id,
        changed_result_ids=changed_result_ids,
    )

    logger.info(
        "[TASK recalculate_positions_and_regenerate_pdfs] COMPLETE. %s / %s / %s. "
        "Changed results: %s.",
        class_name,
        term,
        academic_year,
        changed_result_ids,
    )

    return {"changed_result_ids": changed_result_ids}


# ---------------------------------------------------------------------------
# Auto-publish scheduled results
# ---------------------------------------------------------------------------

@shared_task(
    bind=True,
    name="ResultEntry.tasks.auto_publish_scheduled_results",
    acks_late=True,
)
def auto_publish_scheduled_results(self):
    """
    Publish all scheduled results whose scheduled_date has passed.
    """
    from .models import Result
    from .views import EmailNotifier

    now = timezone.now()

    scheduled_results = list(
        Result.objects.filter(
            status="SCHEDULED",
            scheduled_date__lte=now,
        ).select_related("student")
    )

    if not scheduled_results:
        logger.debug("[TASK auto_publish_scheduled_results] No scheduled results due.")
        return {"published": 0}

    logger.info(
        "[TASK auto_publish_scheduled_results] Found %s scheduled result(s) to publish.",
        len(scheduled_results),
    )

    updated_classes = set()
    published_count = 0

    with transaction.atomic():
        for result in scheduled_results:
            try:
                old_status = result.status

                result.status = "PUBLISHED"
                result.published_date = now
                result.scheduled_date = None
                result.save(
                    update_fields=["status", "published_date", "scheduled_date", "updated_at"],
                    calculate_positions=False,
                )

                logger.info(
                    "[TASK auto_publish_scheduled_results] Published result %s for %s. "
                    "Old status: %s.",
                    result.id,
                    _student_full_name(result.student),
                    old_status,
                )

                EmailNotifier.send_result_published(result)

                updated_classes.add((
                    result.class_name,
                    result.term,
                    result.academic_year,
                ))

                published_count += 1

            except Exception as exc:
                logger.error(
                    "[TASK auto_publish_scheduled_results] Failed to publish result %s: %s",
                    result.id,
                    exc,
                    exc_info=True,
                )

    for class_name, term, academic_year in updated_classes:
        recalculate_positions_and_regenerate_pdfs.delay(
            class_name,
            term,
            academic_year,
        )

    logger.info(
        "[TASK auto_publish_scheduled_results] COMPLETE. Published %s result(s) "
        "across %s class/term/year group(s).",
        published_count,
        len(updated_classes),
    )

    return {"published": published_count}


# ---------------------------------------------------------------------------
# Bulk status update task
# ---------------------------------------------------------------------------

@shared_task(
    bind=True,
    name="ResultEntry.tasks.bulk_update_status_task",
    acks_late=True,
)
def bulk_update_status_task(
    self,
    class_name,
    term,
    new_status,
    scheduled_date_iso=None,
    user_email=None,
    academic_year="2023-2024",
):
    """
    Bulk update result status for all students in a class, term, and academic year.

    new_status must be one of:
    DRAFT, SCHEDULED, PUBLISHED
    """
    from .models import Result, ResultChangeLog
    from authapp.models import CustomUser
    from .views import EmailNotifier

    logger.info(
        "[TASK bulk_update_status_task] START. %s / %s / %s to %s. Triggered by %s.",
        class_name,
        term,
        academic_year,
        new_status,
        user_email,
    )

    valid_statuses = ["DRAFT", "SCHEDULED", "PUBLISHED"]

    if new_status not in valid_statuses:
        logger.error(
            "[TASK bulk_update_status_task] Invalid status supplied: %s.",
            new_status,
        )
        return {
            "updated": 0,
            "error": f"Invalid status '{new_status}'.",
        }

    scheduled_date = None

    if new_status == "SCHEDULED":
        scheduled_date = _parse_scheduled_date(scheduled_date_iso)

        if not scheduled_date:
            logger.error(
                "[TASK bulk_update_status_task] scheduled_date is required for SCHEDULED status."
            )
            return {
                "updated": 0,
                "error": "scheduled_date is required for SCHEDULED status.",
            }

        if scheduled_date <= timezone.now():
            logger.error(
                "[TASK bulk_update_status_task] scheduled_date must be in the future."
            )
            return {
                "updated": 0,
                "error": "scheduled_date must be in the future.",
            }

    student_ids = CustomUser.objects.filter(
        class_name=class_name,
        role="student",
        is_active=True,
    ).values_list("id", flat=True)

    results_queryset = Result.objects.filter(
        class_name=class_name,
        term=term,
        academic_year=academic_year,
        student_id__in=student_ids,
    )

    if new_status == "PUBLISHED":
        results_queryset = results_queryset.exclude(status="PUBLISHED")

    results = list(results_queryset.select_related("student"))

    if not results:
        logger.info(
            "[TASK bulk_update_status_task] No results to update for %s / %s / %s.",
            class_name,
            term,
            academic_year,
        )
        return {"updated": 0}

    updated_count = 0
    now = timezone.now()

    with transaction.atomic():
        for result in results:
            old_status = result.status
            old_scheduled_date = result.scheduled_date
            old_published_date = result.published_date

            result.status = new_status

            if new_status == "PUBLISHED":
                result.published_date = now
                result.scheduled_date = None

            elif new_status == "SCHEDULED":
                result.scheduled_date = scheduled_date

            elif new_status == "DRAFT":
                result.scheduled_date = None
                result.published_date = None

            result.save(
                update_fields=[
                    "status",
                    "scheduled_date",
                    "published_date",
                    "updated_at",
                ],
                calculate_positions=False,
            )

            updated_count += 1

            if user_email:
                if old_status != result.status:
                    ResultChangeLog.objects.create(
                        result=result,
                        changed_by=user_email,
                        field_name="status (bulk update)",
                        previous_value=old_status,
                        new_value=result.status,
                    )

                if old_scheduled_date != result.scheduled_date:
                    ResultChangeLog.objects.create(
                        result=result,
                        changed_by=user_email,
                        field_name="scheduled_date (bulk update)",
                        previous_value=old_scheduled_date,
                        new_value=result.scheduled_date,
                    )

                if old_published_date != result.published_date:
                    ResultChangeLog.objects.create(
                        result=result,
                        changed_by=user_email,
                        field_name="published_date (bulk update)",
                        previous_value=old_published_date,
                        new_value=result.published_date,
                    )

            if old_status != "PUBLISHED" and new_status == "PUBLISHED":
                EmailNotifier.send_result_published(result)

            logger.info(
                "[TASK bulk_update_status_task] Updated result %s for %s. "
                "%s to %s.",
                result.id,
                _student_full_name(result.student),
                old_status,
                new_status,
            )

    recalculate_positions_and_regenerate_pdfs.delay(
        class_name,
        term,
        academic_year,
    )

    logger.info(
        "[TASK bulk_update_status_task] COMPLETE. Updated %s result(s) for %s / %s / %s.",
        updated_count,
        class_name,
        term,
        academic_year,
    )

    return {"updated": updated_count}