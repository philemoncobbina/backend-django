import logging
import os
from datetime import datetime

import pytz
from django.conf import settings
from django.core.cache import cache
from django.core.files.base import ContentFile
from django.db import transaction
from django.db.models import Q, Prefetch
from django.utils import timezone
from django_filters import CharFilter
from django_filters.rest_framework import DjangoFilterBackend, FilterSet
from rest_framework import viewsets, status, filters, permissions
from rest_framework.decorators import action
from rest_framework.exceptions import ValidationError
from rest_framework.response import Response
from sib_api_v3_sdk import Configuration, ApiClient, SendSmtpEmail
from sib_api_v3_sdk.api.transactional_emails_api import TransactionalEmailsApi
from sib_api_v3_sdk.rest import ApiException

from authapp.models import CustomUser

from attendance.models import AcademicTerm

from .models import (
    Course,
    ClassCourse,
    Result,
    CourseResult,
    ResultChangeLog,
    ClassSize,
)
from .permissions import (
    IsStaffOrPrincipal,
    IsOwnerOrReadOnly,
    PublishedResultsOnlyPrincipal,
)
from .serializers import (
    CourseSerializer,
    ClassCourseSerializer,
    ClassCourseDetailSerializer,
    ResultSerializer,
    ResultCreateSerializer,
    CourseResultSerializer,
    ResultChangeLogSerializer,
    StudentSerializer,
    BulkResultUpdateSerializer,
    StudentCourseSerializer,
)
from .utils.pdf_generator import generate_report_card_pdf
from .cache_utils import (
    CACHE_TTL_MEDIUM,
    CACHE_TTL_SHORT,
    available_courses_key,
    class_courses_key,
    invalidate_attendance_stats,
    invalidate_class_courses,
    invalidate_class_size,
    students_by_class_key,
)


logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Helper functions
# ---------------------------------------------------------------------------

def _student_full_name(student):
    """
    Safe helper for student full name.
    Works whether or not CustomUser defines get_full_name().
    """
    if not student:
        return ""

    full_name = f"{student.first_name} {student.last_name}".strip()

    if full_name:
        return full_name

    if hasattr(student, "get_username"):
        return student.get_username()

    return str(student)


def _parse_datetime_value(value):
    """
    Converts ISO string datetime values into aware datetimes.
    DRF already parses DateTimeField values, but this helper protects
    manual endpoint validation too.
    """
    if not value:
        return None

    if isinstance(value, datetime):
        parsed_value = value
    elif isinstance(value, str):
        try:
            parsed_value = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            raise ValidationError({
                "scheduled_date": "Invalid date format. Use ISO format, e.g. YYYY-MM-DDTHH:MM:SSZ."
            })
    else:
        raise ValidationError({
            "scheduled_date": "Invalid scheduled_date value."
        })

    if timezone.is_naive(parsed_value):
        parsed_value = pytz.utc.localize(parsed_value)

    return parsed_value


# ---------------------------------------------------------------------------
# Email Notifier
# ---------------------------------------------------------------------------

class EmailNotifier:
    @staticmethod
    def send_result_published(result):
        """Send email notification when a result is published."""
        try:
            configuration = Configuration()
            configuration.api_key['api-key'] = os.getenv('BREVO_API_KEY')

            api_instance = TransactionalEmailsApi(ApiClient(configuration))

            student = result.student
            student_name = f"{student.first_name} {student.last_name}"

            send_smtp_email = SendSmtpEmail(
                to=[{"email": student.email}],
                sender={"name": "School Administration", "email": settings.DEFAULT_FROM_EMAIL},
                subject=f"Your Results for {result.class_name} - {result.term} term Have Been Published",
                html_content=f"""
                <html>
                <body>
                    <p>Dear {student_name},</p>
                    <p>Your academic results for <strong>{result.class_name}</strong> -
                       <strong>{result.term} term</strong> have been published.</p>
                    <p>You can access your results by logging into your student portal.</p>
                    <p>Best regards,<br>School Administration</p>
                </body>
                </html>
                """
            )

            api_response = api_instance.send_transac_email(send_smtp_email)
            logger.info(
                f"[EmailNotifier] Email sent to {student.email} "
                f"for result {result.id}: {api_response}"
            )

        except ApiException as e:
            logger.error(
                f"[EmailNotifier] ApiException sending email to "
                f"{result.student.email}: {e}"
            )
        except Exception as e:
            logger.error(
                f"[EmailNotifier] Unexpected error sending email to "
                f"{result.student.email}: {e}"
            )


# ---------------------------------------------------------------------------
# Filters
# ---------------------------------------------------------------------------

class ResultFilter(FilterSet):
    student = CharFilter(field_name="student")
    class_name = CharFilter(field_name="class_name")
    term = CharFilter(field_name="term")
    academic_year = CharFilter(field_name="academic_year")
    status = CharFilter(field_name="status")

    class Meta:
        model = Result
        fields = [
            "student",
            "class_name",
            "term",
            "academic_year",
            "status",
        ]


# ---------------------------------------------------------------------------
# CourseViewSet
# ---------------------------------------------------------------------------

class CourseViewSet(viewsets.ModelViewSet):
    queryset = Course.objects.all()
    serializer_class = CourseSerializer
    permission_classes = [IsOwnerOrReadOnly]
    filter_backends = [
        DjangoFilterBackend,
        filters.SearchFilter,
        filters.OrderingFilter,
    ]
    filterset_fields = ["name", "code"]
    search_fields = ["name", "code"]
    ordering_fields = ["name", "code", "created_at"]

    def perform_create(self, serializer):
        serializer.save(created_by=self.request.user)


# ---------------------------------------------------------------------------
# ClassCourseViewSet
# ---------------------------------------------------------------------------

class ClassCourseViewSet(viewsets.ModelViewSet):
    queryset = ClassCourse.objects.select_related("course").all()
    serializer_class = ClassCourseSerializer
    permission_classes = [IsStaffOrPrincipal]
    filter_backends = [
        DjangoFilterBackend,
        filters.SearchFilter,
        filters.OrderingFilter,
    ]
    filterset_fields = ["class_name", "term", "course", "is_active"]
    search_fields = ["class_name", "course__name", "course__code"]
    ordering_fields = ["class_name", "term", "course__name"]

    def get_queryset(self):
        return ClassCourse.objects.select_related("course").all()

    def perform_create(self, serializer):
        instance = serializer.save()
        invalidate_class_courses(instance.class_name, instance.term)

    def perform_update(self, serializer):
        # class_name/term can change on an edit - invalidate both the old
        # and new combination so neither is left showing stale data.
        old_class_name = serializer.instance.class_name
        old_term = serializer.instance.term

        instance = serializer.save()

        invalidate_class_courses(old_class_name, old_term)
        if instance.class_name != old_class_name or instance.term != old_term:
            invalidate_class_courses(instance.class_name, instance.term)

    def perform_destroy(self, instance):
        class_name = instance.class_name
        term = instance.term
        instance.delete()
        invalidate_class_courses(class_name, term)

    @action(detail=False, methods=["get"])
    def by_class_and_term(self, request):
        class_name = request.query_params.get("class_name")
        term = request.query_params.get("term")

        if not class_name or not term:
            return Response(
                {"error": "Both class_name and term parameters are required."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        cache_key = class_courses_key(class_name, term)
        cached_response = cache.get(cache_key)

        if cached_response is not None:
            return Response(cached_response)

        queryset = self.get_queryset().filter(
            class_name=class_name,
            term=term,
            is_active=True,
        )

        serializer = ClassCourseDetailSerializer(
            queryset,
            many=True,
            context=self.get_serializer_context(),
        )

        cache.set(cache_key, serializer.data, CACHE_TTL_MEDIUM)

        return Response(serializer.data)

    @action(detail=False, methods=["post"])
    def bulk_assign(self, request):
        data = request.data

        if not isinstance(data, list):
            return Response(
                {"error": "Expected a list of course assignments."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        created_items = []
        errors = []
        touched_class_terms = set()

        with transaction.atomic():
            for item in data:
                serializer = self.get_serializer(data=item)

                if serializer.is_valid():
                    try:
                        instance = serializer.save()
                        created_items.append(serializer.data)
                        touched_class_terms.add((instance.class_name, instance.term))
                    except Exception as exc:
                        errors.append({
                            "data": item,
                            "error": str(exc),
                        })
                else:
                    errors.append({
                        "data": item,
                        "error": serializer.errors,
                    })

        for class_name, term in touched_class_terms:
            invalidate_class_courses(class_name, term)

        response_data = {
            "created": created_items,
            "errors": errors,
        }

        return Response(
            response_data,
            status=status.HTTP_207_MULTI_STATUS if errors else status.HTTP_201_CREATED,
        )


# ---------------------------------------------------------------------------
# StudentCoursesViewSet
# ---------------------------------------------------------------------------

class StudentCoursesViewSet(viewsets.ReadOnlyModelViewSet):
    """
    NOTE ON CACHING: deliberately not cached, unlike ClassCourseViewSet's
    equivalent action. Every method here branches on the requesting user
    (their own class_name, their class_history, a 403 for viewing another
    class) - caching by class_name+term alone risks serving one student's
    permitted response to a different student who shouldn't see it.
    """
    serializer_class = StudentCourseSerializer
    permission_classes = [permissions.IsAuthenticated]
    queryset = ClassCourse.objects.select_related("course").filter(is_active=True)

    def get_queryset(self):
        return ClassCourse.objects.select_related("course").filter(is_active=True)

    def list(self, request, *args, **kwargs):
        user = request.user

        if user.role in ["staff", "principal"]:
            class_name = request.query_params.get("class_name")

            if not class_name:
                return Response(
                    {
                        "error": (
                            "Staff and principal users must supply a "
                            "?class_name= query parameter."
                        )
                    },
                    status=status.HTTP_400_BAD_REQUEST,
                )
        else:
            class_name = getattr(user, "class_name", None)

            if not class_name:
                return Response(
                    {"error": "Your account does not have a class assigned."},
                    status=status.HTTP_400_BAD_REQUEST,
                )

        term = request.query_params.get("term")

        queryset = self.get_queryset().filter(class_name=class_name)

        if term:
            queryset = queryset.filter(term=term)

        serializer = self.get_serializer(queryset, many=True)

        return Response({
            "class_name": class_name,
            "term": term or "all",
            "total_courses": queryset.count(),
            "courses": serializer.data,
        })

    def retrieve(self, request, *args, **kwargs):
        return Response(
            {"detail": "Direct lookup by ID is not supported on this endpoint."},
            status=status.HTTP_405_METHOD_NOT_ALLOWED,
        )

    @action(detail=False, methods=["get"], url_path="previous")
    def previous(self, request):
        user = request.user
        class_name = request.query_params.get("class_name")
        term = request.query_params.get("term")

        if not class_name or not term:
            return Response(
                {"error": "Both class_name and term query parameters are required."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        if user.role not in ["staff", "principal"]:
            current_class = getattr(user, "class_name", None)

            if class_name == current_class:
                logger.info(
                    "[StudentCoursesViewSet.previous] Student %s requested previous "
                    "courses for current class %s.",
                    user.id,
                    class_name,
                )

            if hasattr(user, "class_history"):
                has_history = user.class_history.filter(class_name=class_name).exists()

                if not has_history:
                    return Response(
                        {"detail": f"You have no enrolment history in class '{class_name}'."},
                        status=status.HTTP_403_FORBIDDEN,
                    )

        queryset = self.get_queryset().filter(
            class_name=class_name,
            term=term,
        )

        serializer = self.get_serializer(queryset, many=True)

        return Response({
            "class_name": class_name,
            "term": term,
            "total_courses": queryset.count(),
            "courses": serializer.data,
        })

    @action(detail=False, methods=["get"], url_path="by_class_and_term")
    def by_class_and_term(self, request):
        class_name = request.query_params.get("class_name")
        term = request.query_params.get("term")

        if not class_name or not term:
            return Response(
                {"error": "Both class_name and term query parameters are required."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        user = request.user

        if user.role not in ["staff", "principal"]:
            current_class = getattr(user, "class_name", None)

            if class_name != current_class:
                return Response(
                    {
                        "detail": (
                            "You are not permitted to view course assignments "
                            "for other classes."
                        )
                    },
                    status=status.HTTP_403_FORBIDDEN,
                )

        queryset = self.get_queryset().filter(
            class_name=class_name,
            term=term,
        )

        serializer = self.get_serializer(queryset, many=True)

        return Response({
            "class_name": class_name,
            "term": term,
            "total_courses": queryset.count(),
            "courses": serializer.data,
        })


# ---------------------------------------------------------------------------
# ResultViewSet
# ---------------------------------------------------------------------------

class ResultViewSet(viewsets.ModelViewSet):
    # Add a default queryset for the router to determine the basename
    queryset = Result.objects.none()  # Empty queryset, will be overridden by get_queryset
    serializer_class = ResultSerializer
    permission_classes = [PublishedResultsOnlyPrincipal]
    filter_backends = [
        DjangoFilterBackend,
        filters.SearchFilter,
        filters.OrderingFilter,
    ]
    filterset_class = ResultFilter
    search_fields = [
        "student__first_name",
        "student__last_name",
        "student__username",
        "class_name",
        "academic_year",
    ]
    ordering_fields = [
        "student__last_name",
        "student__first_name",
        "class_name",
        "term",
        "academic_year",
        "status",
        "created_at",
        "updated_at",
    ]

    def get_queryset(self):
        return Result.objects.select_related("student").prefetch_related(
            Prefetch(
                "course_results",
                queryset=CourseResult.objects.select_related(
                    "class_course",
                    "class_course__course",
                ),
            )
        )

    def get_serializer_class(self):
        if self.action == "bulk_update_status":
            return BulkResultUpdateSerializer

        if self.action in ["create", "update", "partial_update"]:
            return ResultCreateSerializer

        return ResultSerializer

    def get_permissions(self):
        if self.action in [
            "list",
            "retrieve",
            "get_student_results",
            "get_class_results",
            "get_available_courses",
            "get_students_by_class",
            "attendance_summary",
        ]:
            return [permissions.IsAuthenticated()]

        if self.action in [
            "bulk_update_status",
            "recalculate_positions",
        ]:
            return [IsStaffOrPrincipal()]

        return [PublishedResultsOnlyPrincipal()]

    def list(self, request, *args, **kwargs):
        self._auto_publish_scheduled_results()
        return super().list(request, *args, **kwargs)

    def retrieve(self, request, *args, **kwargs):
        self._auto_publish_scheduled_results()
        return super().retrieve(request, *args, **kwargs)

    def _auto_publish_scheduled_results(self):
        """Auto-publish scheduled results that are due."""
        from .tasks import recalculate_positions_and_regenerate_pdfs

        try:
            now = timezone.now()
            scheduled_results = Result.objects.filter(
                status='SCHEDULED',
                scheduled_date__lte=now
            )

            published_count = 0
            updated_classes = set()

            for result in scheduled_results:
                logger.info(
                    f"[ResultViewSet._auto_publish] Publishing result {result.id} — "
                    f"{_student_full_name(result.student)}"
                )
                result.status = 'PUBLISHED'
                result.published_date = now
                result.save()

                EmailNotifier.send_result_published(result)
                updated_classes.add((result.class_name, result.term, result.academic_year))
                published_count += 1

            for class_name, term, academic_year in updated_classes:
                recalculate_positions_and_regenerate_pdfs.delay(class_name, term, academic_year)

            return published_count

        except Exception as e:
            logger.error(f"[ResultViewSet._auto_publish] Error: {e}")
            raise

    def _capture_old_data(self, instance):
        """Capture old data before update for change tracking and cache invalidation."""
        return {
            "student": instance.student_id,
            "status": instance.status,
            "class_name": instance.class_name,
            "term": instance.term,
            "academic_year": instance.academic_year,
        }

    def _validate_scheduled_date(self, scheduled_date):
        """Validate scheduled date is in the future."""
        if not scheduled_date:
            raise ValidationError({
                'scheduled_date': 'Scheduled date is required when status is SCHEDULED.'
            })

        if isinstance(scheduled_date, str):
            try:
                scheduled_date = datetime.fromisoformat(
                    scheduled_date.replace('Z', '+00:00')
                )
                scheduled_date = pytz.utc.localize(
                    scheduled_date.replace(tzinfo=None)
                )
            except ValueError:
                raise ValidationError({
                    'scheduled_date': 'Invalid date format. Use ISO format, e.g. YYYY-MM-DDTHH:MM:SSZ.'
                })

        if timezone.is_naive(scheduled_date):
            scheduled_date = timezone.make_aware(
                scheduled_date,
                timezone.get_current_timezone()
            )

        if scheduled_date <= timezone.now():
            raise ValidationError({
                'scheduled_date': 'Scheduled date must be in the future.'
            })

        return scheduled_date

    def _validate_create_request(self, data):
        """Validate create request data."""
        if data.get('status') == 'SCHEDULED':
            self._validate_scheduled_date(data.get('scheduled_date'))

        if data.get('term') == 'third':
            promoted_to = data.get('promoted_to')
            if not promoted_to or str(promoted_to).strip() == '':
                raise ValidationError({
                    'promoted_to': 'Promoted to class must be specified for third term results.'
                })

    def _build_student_results_queryset(self, student_id, class_name, term, academic_year, user):
        """Build queryset for student results with proper permissions."""
        queryset = self.get_queryset().filter(student_id=student_id)

        if class_name:
            queryset = queryset.filter(class_name=class_name)
        if term:
            queryset = queryset.filter(term=term)
        if academic_year:
            queryset = queryset.filter(academic_year=academic_year)

        # Apply permission-based filtering
        if user.role not in ['staff', 'principal'] and user.id != int(student_id):
            queryset = queryset.filter(status='PUBLISHED')

        return queryset

    def _build_class_results_queryset(self, class_name, term, academic_year, user):
        """Build queryset for class results with proper permissions."""
        queryset = self.get_queryset().filter(class_name=class_name)

        if term:
            queryset = queryset.filter(term=term)
        if academic_year:
            queryset = queryset.filter(academic_year=academic_year)

        # Apply permission-based filtering
        if user.role not in ['staff', 'principal']:
            queryset = queryset.filter(status='PUBLISHED')

        return queryset

    def create(self, request, *args, **kwargs):
        from .tasks import recalculate_positions_and_regenerate_pdfs

        self._validate_create_request(request.data)

        serializer = self.get_serializer(
            data=request.data,
            context=self.get_serializer_context(),
        )
        serializer.is_valid(raise_exception=True)

        with transaction.atomic():
            instance = serializer.save()

        recalculate_positions_and_regenerate_pdfs.delay(
            instance.class_name,
            instance.term,
            instance.academic_year,
        )

        if instance.status == "PUBLISHED":
            EmailNotifier.send_result_published(instance)

        logger.info(
            "[ResultViewSet.create] Result %s created. Recalculation and PDF "
            "generation dispatched for %s / %s / %s.",
            instance.id,
            instance.class_name,
            instance.term,
            instance.academic_year,
        )

        response_serializer = ResultSerializer(
            instance,
            context=self.get_serializer_context(),
        )

        return Response(
            response_serializer.data,
            status=status.HTTP_201_CREATED,
        )

    def update(self, request, *args, **kwargs):
        from .tasks import (
            generate_pdf_for_result,
            recalculate_positions_and_regenerate_pdfs,
        )

        partial = kwargs.pop("partial", False)
        instance = self.get_object()
        old_data = self._capture_old_data(instance)

        if request.data.get("status") == "SCHEDULED":
            self._validate_scheduled_date(request.data.get("scheduled_date"))

        serializer = self.get_serializer(
            instance,
            data=request.data,
            partial=partial,
            context=self.get_serializer_context(),
        )
        serializer.is_valid(raise_exception=True)

        with transaction.atomic():
            updated_instance = serializer.save()

        status_changed = old_data["status"] != updated_instance.status
        location_changed = any([
            old_data["class_name"] != updated_instance.class_name,
            old_data["term"] != updated_instance.term,
            old_data["academic_year"] != updated_instance.academic_year,
        ])
        student_changed = old_data["student"] != updated_instance.student_id
        scores_changed = getattr(updated_instance, "_scores_changed", False)
        regenerate_this_pdf = getattr(updated_instance, "_regenerate_pdf", False)

        if old_data["status"] != "PUBLISHED" and updated_instance.status == "PUBLISHED":
            EmailNotifier.send_result_published(updated_instance)

        if scores_changed:
            logger.info(
                "[ResultViewSet.update] Scores changed for result %s. "
                "Dispatching full recalculation and PDF regeneration.",
                updated_instance.id,
            )

            recalculate_positions_and_regenerate_pdfs.delay(
                updated_instance.class_name,
                updated_instance.term,
                updated_instance.academic_year,
            )

        elif status_changed or regenerate_this_pdf:
            logger.info(
                "[ResultViewSet.update] Status or significant fields changed "
                "for result %s. Dispatching single PDF regeneration.",
                updated_instance.id,
            )

            generate_pdf_for_result.delay(updated_instance.id)

        elif updated_instance.status == "PUBLISHED" and not updated_instance.report_card_pdf:
            logger.info(
                "[ResultViewSet.update] Result %s is published but has no PDF. "
                "Dispatching single PDF generation.",
                updated_instance.id,
            )

            generate_pdf_for_result.delay(updated_instance.id)

        if location_changed or student_changed:
            logger.info(
                "[ResultViewSet.update] Result %s location changed. "
                "Dispatching recalculation for old class/term/year %s / %s / %s.",
                updated_instance.id,
                old_data["class_name"],
                old_data["term"],
                old_data["academic_year"],
            )

            recalculate_positions_and_regenerate_pdfs.delay(
                old_data["class_name"],
                old_data["term"],
                old_data["academic_year"],
            )

            # Result.save() (called inside serializer.save() above) already
            # invalidated the NEW class/term/year's caches. Only the OLD
            # location's attendance-stats and class-size caches are still
            # stale at this point, and this is the only place that still
            # has the old values to invalidate them with.
            invalidate_attendance_stats(
                old_data["student"],
                old_data["class_name"],
                old_data["term"],
                old_data["academic_year"],
            )
            invalidate_class_size(
                old_data["class_name"], old_data["term"], old_data["academic_year"]
            )

        response_serializer = ResultSerializer(
            updated_instance,
            context=self.get_serializer_context(),
        )

        return Response(response_serializer.data)

    def destroy(self, request, *args, **kwargs):
        from .tasks import recalculate_positions_and_regenerate_pdfs

        instance = self.get_object()
        class_name = instance.class_name
        term = instance.term
        academic_year = instance.academic_year
        student_id = instance.student_id

        with transaction.atomic():
            self.perform_destroy(instance)

        # Result.save() isn't involved in a delete, so nothing has
        # invalidated this student's stats or the class's size yet.
        invalidate_attendance_stats(student_id, class_name, term, academic_year)
        invalidate_class_size(class_name, term, academic_year)

        recalculate_positions_and_regenerate_pdfs.delay(
            class_name,
            term,
            academic_year,
        )

        logger.info(
            "[ResultViewSet.destroy] Result deleted. Recalculation dispatched "
            "for %s / %s / %s.",
            class_name,
            term,
            academic_year,
        )

        return Response(status=status.HTTP_204_NO_CONTENT)

    # -----------------------------------------------------------------------
    # Result action endpoints
    # -----------------------------------------------------------------------

    @action(detail=False, methods=["get"])
    def get_student_results(self, request):
        self._auto_publish_scheduled_results()

        student_id = request.query_params.get("student")

        if not student_id:
            return Response(
                {"error": "student parameter is required."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        class_name = request.query_params.get("class_name")
        term = request.query_params.get("term")
        academic_year = request.query_params.get("academic_year")

        queryset = self._build_student_results_queryset(
            student_id=student_id,
            class_name=class_name,
            term=term,
            academic_year=academic_year,
            user=request.user,
        )

        serializer = ResultSerializer(
            queryset,
            many=True,
            context=self.get_serializer_context(),
        )

        return Response(serializer.data)

    @action(detail=False, methods=["get"])
    def get_class_results(self, request):
        self._auto_publish_scheduled_results()

        class_name = request.query_params.get("class_name")

        if not class_name:
            return Response(
                {"error": "class_name parameter is required."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        term = request.query_params.get("term")
        academic_year = request.query_params.get("academic_year")

        queryset = self._build_class_results_queryset(
            class_name=class_name,
            term=term,
            academic_year=academic_year,
            user=request.user,
        )

        serializer = ResultSerializer(
            queryset,
            many=True,
            context=self.get_serializer_context(),
        )

        return Response(serializer.data)

    @action(detail=True, methods=["get"])
    def change_log(self, request, pk=None):
        result = self.get_object()

        logs = ResultChangeLog.objects.filter(result=result).order_by("-changed_at")

        serializer = ResultChangeLogSerializer(
            logs,
            many=True,
            context=self.get_serializer_context(),
        )

        return Response(serializer.data)

    @action(detail=False, methods=["get"])
    def get_available_courses(self, request):
        class_name = request.query_params.get("class_name")
        term = request.query_params.get("term")

        if not class_name or not term:
            return Response(
                {"error": "Both class_name and term parameters are required."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        cache_key = available_courses_key(class_name, term)
        cached_response = cache.get(cache_key)

        if cached_response is not None:
            return Response(cached_response)

        class_courses = ClassCourse.objects.select_related("course").filter(
            class_name=class_name,
            term=term,
            is_active=True,
        )

        serializer = ClassCourseSerializer(
            class_courses,
            many=True,
            context=self.get_serializer_context(),
        )

        cache.set(cache_key, serializer.data, CACHE_TTL_MEDIUM)

        return Response(serializer.data)

    @action(detail=False, methods=["get"])
    def get_students_by_class(self, request):
        class_name = request.query_params.get("class_name")

        if not class_name:
            return Response(
                {"error": "class_name parameter is required."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        cache_key = students_by_class_key(class_name)
        cached_response = cache.get(cache_key)

        if cached_response is not None:
            return Response(cached_response)

        students = CustomUser.objects.filter(
            class_name=class_name,
            role="student",
            is_active=True,
        ).order_by("last_name", "first_name", "id")

        serializer = StudentSerializer(
            students,
            many=True,
            context=self.get_serializer_context(),
        )

        cache.set(cache_key, serializer.data, CACHE_TTL_SHORT)

        return Response(serializer.data)

    @action(detail=True, methods=["get"])
    def attendance_summary(self, request, pk=None):
        """
        Shows the attendance values that will appear on a student's result.

        These values are not manually stored on Result anymore.
        They are calculated from the Attendance app through the Result model.
        """
        result = self.get_object()

        return Response({
            "result_id": result.id,
            "student": {
                "id": result.student_id,
                "name": _student_full_name(result.student),
                "class_name": result.class_name,
            },
            "academic_year": result.academic_year,
            "term": result.term,
            "term_display": result.get_term_display(),
            "attendance": {
                "days_present": result.days_present,
                "days_absent": result.days_absent,
                "total_days": result.total_days,
                "total_school_days": result.total_school_days,
                "total_marked_attendance_days": result.total_marked_attendance_days,
                "days_unmarked": result.days_unmarked,
                "attendance_percentage": result.attendance_percentage,
                "marked_attendance_percentage": result.marked_attendance_percentage,
            },
        })

    @action(detail=False, methods=["post"])
    def recalculate_positions(self, request):
        from .tasks import recalculate_positions_and_regenerate_pdfs

        class_name = request.data.get("class_name")
        term = request.data.get("term")
        academic_year = request.data.get("academic_year", "2023-2024")

        if not class_name or not term:
            return Response(
                {"error": "Both class_name and term are required."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        results_count = Result.objects.filter(
            class_name=class_name,
            term=term,
            academic_year=academic_year,
        ).count()

        if results_count == 0:
            return Response(
                {
                    "error": (
                        f"No results found for {class_name}, "
                        f"{term}, {academic_year}."
                    )
                },
                status=status.HTTP_404_NOT_FOUND,
            )

        recalculate_positions_and_regenerate_pdfs.delay(
            class_name,
            term,
            academic_year,
        )

        logger.info(
            "[ResultViewSet.recalculate_positions] Recalculation dispatched "
            "for %s / %s / %s. Results count: %s.",
            class_name,
            term,
            academic_year,
            results_count,
        )

        return Response({
            "message": (
                "Position recalculation and PDF regeneration dispatched "
                f"for {results_count} result(s)."
            ),
            "class_name": class_name,
            "term": term,
            "academic_year": academic_year,
            "results_count": results_count,
        })

    @action(detail=False, methods=["post"])
    def bulk_update_status(self, request):
        serializer = self.get_serializer(
            data=request.data,
            context=self.get_serializer_context(),
        )
        serializer.is_valid(raise_exception=True)

        data = serializer.validated_data
        class_name = data.get("class_name")
        term = data.get("term")
        academic_year = data.get("academic_year")
        status_value = data.get("status")
        scheduled_date = data.get("scheduled_date")

        # Build the filter
        filter_kwargs = {
            "class_name": class_name,
            "term": term,
        }
        if academic_year:
            filter_kwargs["academic_year"] = academic_year

        # Get all results matching the filter
        results = Result.objects.filter(**filter_kwargs)
        results_count = results.count()

        if results_count == 0:
            return Response(
                {"error": "No results found matching the criteria."},
                status=status.HTTP_404_NOT_FOUND,
            )

        # Update all results
        with transaction.atomic():
            update_data = {"status": status_value}
            if status_value == "PUBLISHED":
                update_data["published_date"] = timezone.now()
            elif status_value == "SCHEDULED" and scheduled_date:
                update_data["scheduled_date"] = scheduled_date

            results.update(**update_data)

            # Send emails for published results
            if status_value == "PUBLISHED":
                for result in results:
                    EmailNotifier.send_result_published(result)

        # Trigger recalculation
        from .tasks import recalculate_positions_and_regenerate_pdfs
        recalculate_positions_and_regenerate_pdfs.delay(
            class_name,
            term,
            academic_year or "2023-2024",
        )

        logger.info(
            "[ResultViewSet.bulk_update_status] Bulk updated %s results to status %s "
            "for %s / %s / %s.",
            results_count,
            status_value,
            class_name,
            term,
            academic_year,
        )

        return Response({
            "message": f"Successfully updated {results_count} results.",
            "class_name": class_name,
            "term": term,
            "academic_year": academic_year,
            "status": status_value,
            "results_updated": results_count,
        })


# ---------------------------------------------------------------------------
# StudentResultsViewSet
# ---------------------------------------------------------------------------

class StudentResultsViewSet(viewsets.ReadOnlyModelViewSet):
    serializer_class = ResultSerializer
    permission_classes = [permissions.IsAuthenticated]
    queryset = Result.objects.none()  # Empty queryset, will be overridden

    def get_queryset(self):
        user = self.request.user
        self._check_scheduled_results()

        return Result.objects.filter(
            student=user,
            status__in=['PUBLISHED', 'SCHEDULED'],
        ).filter(
            Q(status='PUBLISHED') |
            Q(status='SCHEDULED', scheduled_date__lte=timezone.now())
        )

    def _check_scheduled_results(self):
        from .tasks import recalculate_positions_and_regenerate_pdfs

        try:
            now = timezone.now()
            scheduled_results = Result.objects.filter(
                status='SCHEDULED',
                scheduled_date__lte=now
            )

            published_count = 0
            updated_classes = set()

            for result in scheduled_results:
                logger.info(
                    f"[StudentResultsViewSet._check_scheduled] Publishing result {result.id} — "
                    f"{_student_full_name(result.student)}"
                )
                result.status = 'PUBLISHED'
                result.published_date = now
                result.save()

                EmailNotifier.send_result_published(result)
                updated_classes.add((result.class_name, result.term, result.academic_year))
                published_count += 1

            for class_name, term, academic_year in updated_classes:
                recalculate_positions_and_regenerate_pdfs.delay(class_name, term, academic_year)

            return published_count

        except Exception as e:
            logger.error(f"[StudentResultsViewSet._check_scheduled] Error: {e}")
            raise

    def list(self, request, *args, **kwargs):
        term = request.query_params.get('term')
        class_name = request.query_params.get('class_name')

        queryset = self.get_queryset()

        if term:
            queryset = queryset.filter(term=term)
        if class_name:
            queryset = queryset.filter(class_name=class_name)

        serializer = self.get_serializer(queryset, many=True)
        return Response(serializer.data)

    @action(detail=False, methods=['get'])
    def current_class(self, request):
        user = request.user

        if not user.role == 'student':
            return Response(
                {"detail": "Only students can access this endpoint."},
                status=status.HTTP_403_FORBIDDEN
            )

        self._check_scheduled_results()

        queryset = Result.objects.filter(
            student=user,
            class_name=user.class_name,
            status__in=['PUBLISHED', 'SCHEDULED']
        ).filter(
            Q(status='PUBLISHED') |
            Q(status='SCHEDULED', scheduled_date__lte=timezone.now())
        )

        term = request.query_params.get('term')
        if term:
            queryset = queryset.filter(term=term)

        serializer = self.get_serializer(queryset, many=True)
        return Response(serializer.data)

    @action(detail=False, methods=['get'])
    def previous_classes(self, request):
        user = request.user

        if not user.role == 'student':
            return Response(
                {"detail": "Only students can access this endpoint."},
                status=status.HTTP_403_FORBIDDEN
            )

        class_name = request.query_params.get('class_name')
        term = request.query_params.get('term')

        if not class_name or not term:
            return Response(
                {"error": "Both class_name and term parameters are required"},
                status=status.HTTP_400_BAD_REQUEST
            )

        self._check_scheduled_results()

        current_class = user.class_name

        if not hasattr(user, 'class_history'):
            return Response(
                {"detail": "No class history available for this student."},
                status=status.HTTP_404_NOT_FOUND
            )

        previous_classes = user.class_history.exclude(
            class_name=current_class
        ).values_list('class_name', flat=True).distinct()

        if not previous_classes:
            return Response(
                {"detail": "No previous class history found (excluding current class)."},
                status=status.HTTP_404_NOT_FOUND
            )

        if class_name not in previous_classes:
            return Response(
                {"detail": f"Student has no history in class {class_name}."},
                status=status.HTTP_404_NOT_FOUND
            )

        queryset = Result.objects.filter(
            student=user,
            class_name=class_name,
            term=term,
            status__in=['PUBLISHED', 'SCHEDULED']
        ).filter(
            Q(status='PUBLISHED') |
            Q(status='SCHEDULED', scheduled_date__lte=timezone.now())
        )

        if not queryset.exists():
            return Response(
                {"detail": f"No results found for class {class_name}, term {term}."},
                status=status.HTTP_404_NOT_FOUND
            )

        serializer = self.get_serializer(queryset, many=True)
        return Response(serializer.data)