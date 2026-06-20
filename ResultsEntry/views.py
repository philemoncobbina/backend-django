import logging
import os
from datetime import datetime

from django.conf import settings
from django.core.files.base import ContentFile
from django.db import transaction
from django.db.models import Q, F, Prefetch
from django.shortcuts import get_object_or_404
from django.utils import timezone
from django_filters import CharFilter, ChoiceFilter
from django_filters.rest_framework import DjangoFilterBackend, FilterSet
import pytz
from rest_framework import (
    viewsets, status, generics, filters,
    permissions, response, exceptions
)
from rest_framework.decorators import action
from rest_framework.response import Response
from rest_framework.exceptions import ValidationError
from sib_api_v3_sdk import Configuration, ApiClient, SendSmtpEmail
from sib_api_v3_sdk.api.transactional_emails_api import TransactionalEmailsApi
from sib_api_v3_sdk.rest import ApiException

from authapp.models import CustomUser
from .models import Course, ClassCourse, Result, CourseResult, ResultChangeLog, ClassSize
from .permissions import (
    IsStaffOrPrincipal, IsOwnerOrReadOnly,
    IsOwnerOrStaffOrPrincipal, IsPrincipal,
    PublishedResultsOnlyPrincipal
)
from .serializers import (
    CourseSerializer, ClassCourseSerializer,
    ClassCourseDetailSerializer, ResultSerializer,
    ResultCreateSerializer, CourseResultSerializer,
    ResultChangeLogSerializer, StudentSerializer,
    BulkResultUpdateSerializer, StudentCourseSerializer,
)
from .utils.pdf_generator import generate_report_card_pdf

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------

def _student_full_name(student):
    """Safe helper — works whether or not CustomUser defines get_full_name()."""
    return f"{student.first_name} {student.last_name}".strip() or student.get_username()


# ---------------------------------------------------------------------------
# Filters
# ---------------------------------------------------------------------------

class ResultFilter(FilterSet):
    student = CharFilter(field_name='student')
    class_name = CharFilter(field_name='class_name')
    term = CharFilter(field_name='term')
    status = CharFilter(field_name='status')

    class Meta:
        model = Result
        fields = ['student', 'class_name', 'term', 'status']


# ---------------------------------------------------------------------------
# CourseViewSet
# ---------------------------------------------------------------------------

class CourseViewSet(viewsets.ModelViewSet):
    queryset = Course.objects.all()
    serializer_class = CourseSerializer
    permission_classes = [IsOwnerOrReadOnly]
    filter_backends = [DjangoFilterBackend, filters.SearchFilter, filters.OrderingFilter]
    filterset_fields = ['name', 'code']
    search_fields = ['name', 'code']
    ordering_fields = ['name', 'code', 'created_at']

    def perform_create(self, serializer):
        serializer.save(created_by=self.request.user)


# ---------------------------------------------------------------------------
# ClassCourseViewSet
# ---------------------------------------------------------------------------

class ClassCourseViewSet(viewsets.ModelViewSet):
    queryset = ClassCourse.objects.all()
    serializer_class = ClassCourseSerializer
    permission_classes = [IsStaffOrPrincipal]
    filter_backends = [DjangoFilterBackend, filters.SearchFilter, filters.OrderingFilter]
    filterset_fields = ['class_name', 'term', 'course']
    search_fields = ['class_name', 'course__name', 'course__code']
    ordering_fields = ['class_name', 'term', 'course__name']

    @action(detail=False, methods=['get'])
    def by_class_and_term(self, request):
        class_name = request.query_params.get('class_name')
        term = request.query_params.get('term')
        if not class_name or not term:
            return Response(
                {"error": "Both class_name and term parameters are required"},
                status=status.HTTP_400_BAD_REQUEST
            )
        queryset = self.get_queryset().filter(class_name=class_name, term=term)
        serializer = ClassCourseDetailSerializer(queryset, many=True)
        return Response(serializer.data)

    @action(detail=False, methods=['post'])
    def bulk_assign(self, request):
        data = request.data
        if not isinstance(data, list):
            return Response(
                {"error": "Expected a list of assignments"},
                status=status.HTTP_400_BAD_REQUEST
            )
        created_items, errors = [], []
        for item in data:
            serializer = self.get_serializer(data=item)
            if serializer.is_valid():
                try:
                    serializer.save()
                    created_items.append(serializer.data)
                except Exception as e:
                    errors.append({"data": item, "error": str(e)})
            else:
                errors.append({"data": item, "error": serializer.errors})
        response_data = {"created": created_items, "errors": errors}
        return Response(
            response_data,
            status=status.HTTP_207_MULTI_STATUS if errors else status.HTTP_201_CREATED
        )


# ---------------------------------------------------------------------------
# StudentCoursesViewSet
# ---------------------------------------------------------------------------

class StudentCoursesViewSet(viewsets.ReadOnlyModelViewSet):
    serializer_class = StudentCourseSerializer
    permission_classes = [permissions.IsAuthenticated]

    def get_queryset(self):
        return ClassCourse.objects.select_related('course').all()

    def list(self, request, *args, **kwargs):
        user = request.user

        if user.role in ['staff', 'principal']:
            class_name = request.query_params.get('class_name')
            if not class_name:
                return Response(
                    {"error": "Staff and principal users must supply a ?class_name= query parameter."},
                    status=status.HTTP_400_BAD_REQUEST,
                )
        else:
            class_name = getattr(user, 'class_name', None)
            if not class_name:
                return Response(
                    {"error": "Your account does not have a class assigned."},
                    status=status.HTTP_400_BAD_REQUEST,
                )

        term = request.query_params.get('term')
        queryset = ClassCourse.objects.select_related('course').filter(class_name=class_name)
        if term:
            queryset = queryset.filter(term=term)

        if not queryset.exists():
            return Response(
                {
                    "message": (
                        f"No courses found for class '{class_name}'"
                        + (f" in term '{term}'" if term else "") + "."
                    ),
                    "class_name": class_name,
                    "term": term,
                    "courses": [],
                },
                status=status.HTTP_200_OK,
            )

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

    @action(detail=False, methods=['get'], url_path='previous')
    def previous(self, request):
        user = request.user
        class_name = request.query_params.get('class_name')
        term = request.query_params.get('term')

        if not class_name or not term:
            return Response(
                {"error": "Both 'class_name' and 'term' query parameters are required."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        if user.role not in ['staff', 'principal']:
            current_class = getattr(user, 'class_name', None)
            if class_name == current_class:
                logger.info(
                    f"Student {user.id} requested previous/ for their current class ({class_name}). Serving anyway."
                )
            if hasattr(user, 'class_history'):
                if not user.class_history.filter(class_name=class_name).exists():
                    return Response(
                        {"detail": f"You have no enrolment history in class '{class_name}'."},
                        status=status.HTTP_403_FORBIDDEN,
                    )

        queryset = ClassCourse.objects.select_related('course').filter(class_name=class_name, term=term)

        if not queryset.exists():
            return Response(
                {
                    "message": f"No courses found for class '{class_name}' in term '{term}'.",
                    "class_name": class_name,
                    "term": term,
                    "courses": [],
                },
                status=status.HTTP_200_OK,
            )

        serializer = self.get_serializer(queryset, many=True)
        return Response({
            "class_name": class_name,
            "term": term,
            "total_courses": queryset.count(),
            "courses": serializer.data,
        })

    @action(detail=False, methods=['get'], url_path='by_class_and_term')
    def by_class_and_term(self, request):
        class_name = request.query_params.get('class_name')
        term = request.query_params.get('term')

        if not class_name or not term:
            return Response(
                {"error": "Both 'class_name' and 'term' query parameters are required."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        user = request.user
        if user.role not in ['staff', 'principal']:
            current_class = getattr(user, 'class_name', None)
            if class_name != current_class:
                return Response(
                    {"detail": "You are not permitted to view course assignments for other classes."},
                    status=status.HTTP_403_FORBIDDEN,
                )

        queryset = ClassCourse.objects.select_related('course').filter(class_name=class_name, term=term)

        if not queryset.exists():
            return Response(
                {
                    "message": f"No courses found for class '{class_name}' in term '{term}'.",
                    "class_name": class_name,
                    "term": term,
                    "courses": [],
                },
                status=status.HTTP_200_OK,
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
    queryset = Result.objects.select_related('student')
    serializer_class = ResultSerializer
    permission_classes = [PublishedResultsOnlyPrincipal]
    filter_backends = [DjangoFilterBackend, filters.SearchFilter, filters.OrderingFilter]
    filterset_class = ResultFilter
    search_fields = ['student__first_name', 'student__last_name', 'class_name']
    ordering_fields = ['student__last_name', 'class_name', 'term', 'status']

    def get_serializer_class(self):
        if self.action == 'bulk_update_status':
            return BulkResultUpdateSerializer
        return ResultCreateSerializer if self.action in ['create', 'update', 'partial_update'] else ResultSerializer

    def get_permissions(self):
        if self.action in ['list', 'retrieve', 'get_student_results', 'get_class_results',
                           'get_available_courses', 'get_students_by_class']:
            return [permissions.IsAuthenticated()]
        elif self.action == 'bulk_update_status':
            return [IsStaffOrPrincipal()]
        return [PublishedResultsOnlyPrincipal()]

    def get_queryset(self):
        return Result.objects.select_related('student').prefetch_related(
            Prefetch(
                'course_results',
                queryset=CourseResult.objects.select_related('class_course__course')
            )
        )

    def list(self, request, *args, **kwargs):
        self._auto_publish_scheduled_results()
        return super().list(request, *args, **kwargs)

    def retrieve(self, request, *args, **kwargs):
        self._auto_publish_scheduled_results()
        return super().retrieve(request, *args, **kwargs)

    def create(self, request, *args, **kwargs):
        from .tasks import generate_pdf_for_result, recalculate_positions_and_regenerate_pdfs

        self._validate_create_request(request.data)

        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        with transaction.atomic():
            instance = serializer.save()

        # Offload position recalculation + PDF fan-out to Celery
        recalculate_positions_and_regenerate_pdfs.delay(
            instance.class_name,
            instance.term,
            instance.academic_year,
        )

        # Send email synchronously (fast) if published
        if instance.status == 'PUBLISHED':
            EmailNotifier.send_result_published(instance)

        logger.info(
            f"[ResultViewSet.create] Result {instance.id} created — "
            f"position recalculation + PDF generation dispatched to Celery."
        )

        response_serializer = ResultSerializer(instance)
        return Response(response_serializer.data, status=status.HTTP_201_CREATED)

    def update(self, request, *args, **kwargs):
        from .tasks import generate_pdf_for_result, recalculate_positions_and_regenerate_pdfs

        partial = kwargs.pop('partial', False)
        instance = self.get_object()
        old_data = self._capture_old_data(instance)

        if request.data.get('status') == 'SCHEDULED':
            self._validate_scheduled_date(request.data.get('scheduled_date'))

        serializer = self.get_serializer(instance, data=request.data, partial=partial)
        serializer.is_valid(raise_exception=True)

        with transaction.atomic():
            updated_instance = serializer.save()

        status_changed = old_data['status'] != updated_instance.status
        location_changed = any([
            old_data['class_name'] != updated_instance.class_name,
            old_data['term'] != updated_instance.term,
            old_data['academic_year'] != updated_instance.academic_year,
        ])
        scores_changed = getattr(updated_instance, '_scores_changed', False)

        # Send email if newly published (fast, keep synchronous)
        if old_data['status'] != 'PUBLISHED' and updated_instance.status == 'PUBLISHED':
            EmailNotifier.send_result_published(updated_instance)

        # Determine whether to regenerate PDF for this result only, or the whole class
        if scores_changed:
            # Scores changed → positions may shift for everyone → regenerate all
            logger.info(
                f"[ResultViewSet.update] Scores changed for result {updated_instance.id} — "
                f"dispatching full class PDF regeneration."
            )
            recalculate_positions_and_regenerate_pdfs.delay(
                updated_instance.class_name,
                updated_instance.term,
                updated_instance.academic_year,
            )
        elif status_changed or getattr(updated_instance, '_regenerate_pdf', False):
            # Only this result's PDF needs updating
            logger.info(
                f"[ResultViewSet.update] Status/field changed for result {updated_instance.id} — "
                f"dispatching single PDF generation."
            )
            generate_pdf_for_result.delay(updated_instance.id)
        elif updated_instance.status == 'PUBLISHED' and not updated_instance.report_card_pdf:
            logger.info(
                f"[ResultViewSet.update] Result {updated_instance.id} published but no PDF — "
                f"dispatching single PDF generation."
            )
            generate_pdf_for_result.delay(updated_instance.id)

        # If moved to a different class/term, recalculate old location too
        if location_changed:
            logger.info(
                f"[ResultViewSet.update] Result {updated_instance.id} moved — "
                f"dispatching recalculation for old location "
                f"{old_data['class_name']} / {old_data['term']}."
            )
            recalculate_positions_and_regenerate_pdfs.delay(
                old_data['class_name'],
                old_data['term'],
                old_data['academic_year'],
            )

        response_serializer = ResultSerializer(updated_instance)
        return Response(response_serializer.data)

    def destroy(self, request, *args, **kwargs):
        from .tasks import recalculate_positions_and_regenerate_pdfs

        instance = self.get_object()
        class_info = (instance.class_name, instance.term, instance.academic_year)

        with transaction.atomic():
            super().destroy(request, *args, **kwargs)

        # Recalculate positions for the class after deletion
        recalculate_positions_and_regenerate_pdfs.delay(*class_info)
        logger.info(
            f"[ResultViewSet.destroy] Result deleted — recalculation dispatched for "
            f"{class_info[0]} / {class_info[1]}."
        )

        return Response(status=status.HTTP_204_NO_CONTENT)

    # ------------------------------------------------------------------
    # Action endpoints
    # ------------------------------------------------------------------

    @action(detail=False, methods=['get'])
    def get_student_results(self, request):
        self._auto_publish_scheduled_results()

        student_id = request.query_params.get('student')
        if not student_id:
            return Response(
                {"error": "student parameter is required"},
                status=status.HTTP_400_BAD_REQUEST
            )

        class_name = request.query_params.get('class_name')
        term = request.query_params.get('term')

        queryset = self._build_student_results_queryset(student_id, class_name, term, request.user)
        serializer = self.get_serializer(queryset, many=True)
        return Response(serializer.data)

    @action(detail=False, methods=['get'])
    def get_class_results(self, request):
        self._auto_publish_scheduled_results()

        class_name = request.query_params.get('class_name')
        if not class_name:
            return Response(
                {"error": "class_name parameter is required"},
                status=status.HTTP_400_BAD_REQUEST
            )

        term = request.query_params.get('term')
        queryset = self._build_class_results_queryset(class_name, term, request.user)
        serializer = self.get_serializer(queryset, many=True)
        return Response(serializer.data)

    @action(detail=True, methods=['get'])
    def change_log(self, request, pk=None):
        result = self.get_object()
        logs = ResultChangeLog.objects.filter(result=result).order_by('-changed_at')
        serializer = ResultChangeLogSerializer(logs, many=True)
        return Response(serializer.data)

    @action(detail=False, methods=['get'])
    def get_available_courses(self, request):
        class_name = request.query_params.get('class_name')
        term = request.query_params.get('term')

        if not class_name or not term:
            return Response(
                {"error": "Both class_name and term parameters are required"},
                status=status.HTTP_400_BAD_REQUEST
            )

        class_courses = ClassCourse.objects.filter(
            class_name=class_name, term=term
        ).select_related('course')
        serializer = ClassCourseSerializer(class_courses, many=True)
        return Response(serializer.data)

    @action(detail=False, methods=['get'])
    def get_students_by_class(self, request):
        class_name = request.query_params.get('class_name')
        if not class_name:
            return Response(
                {"error": "class_name parameter is required"},
                status=status.HTTP_400_BAD_REQUEST
            )

        students = CustomUser.objects.filter(class_name=class_name, role='student')
        serializer = StudentSerializer(students, many=True)
        return Response(serializer.data)

    @action(detail=False, methods=['post'])
    def recalculate_positions(self, request):
        from .tasks import recalculate_positions_and_regenerate_pdfs

        class_name = request.data.get('class_name')
        term = request.data.get('term')
        academic_year = request.data.get('academic_year', '2023-2024')

        if not class_name or not term:
            return Response(
                {"error": "Both class_name and term are required"},
                status=status.HTTP_400_BAD_REQUEST
            )

        results_count = Result.objects.filter(
            class_name=class_name, term=term, academic_year=academic_year
        ).count()

        if results_count == 0:
            return Response(
                {"error": f"No results found for {class_name}, {term}, {academic_year}"},
                status=status.HTTP_404_NOT_FOUND
            )

        recalculate_positions_and_regenerate_pdfs.delay(class_name, term, academic_year)

        logger.info(
            f"[ResultViewSet.recalculate_positions] Recalculation dispatched for "
            f"{class_name} / {term} / {academic_year} ({results_count} results)."
        )

        return Response({
            "message": f"Position recalculation + PDF regeneration dispatched for {results_count} results",
            "class_name": class_name,
            "term": term,
            "academic_year": academic_year,
            "results_count": results_count
        })

    @action(detail=False, methods=['post'])
    def bulk_update_status(self, request):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        data = serializer.validated_data
        return BulkStatusUpdater(data, request.user).execute()

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _validate_create_request(self, data):
        if data.get('status') == 'SCHEDULED':
            self._validate_scheduled_date(data.get('scheduled_date'))

        student_id = data.get('student')
        class_name = data.get('class_name')
        term = data.get('term')
        academic_year = data.get('academic_year', '2023-2024')

        if all([student_id, class_name, term]):
            if Result.objects.filter(
                student_id=student_id, class_name=class_name,
                term=term, academic_year=academic_year
            ).exists():
                student = CustomUser.objects.get(id=student_id)
                raise ValidationError(
                    f"Result already exists for {student.first_name} {student.last_name} "
                    f"in {class_name}, {term}, {academic_year}"
                )

    def _validate_scheduled_date(self, scheduled_date):
        if not scheduled_date:
            return None

        if isinstance(scheduled_date, str):
            try:
                scheduled_date = datetime.fromisoformat(scheduled_date.replace('Z', '+00:00'))
                if scheduled_date.tzinfo is None:
                    scheduled_date = pytz.utc.localize(scheduled_date)
            except ValueError:
                raise ValidationError({'scheduled_date': 'Invalid date format. Use ISO format'})

        if scheduled_date <= timezone.now():
            raise ValidationError({'scheduled_date': 'Scheduled date must be in the future'})

        return scheduled_date

    def _capture_old_data(self, instance):
        return {
            'status': instance.status,
            'class_name': instance.class_name,
            'term': instance.term,
            'academic_year': instance.academic_year
        }

    def _auto_publish_scheduled_results(self):
        """
        Lightweight synchronous check — publishes overdue SCHEDULED results
        and delegates the heavy PDF/position work to Celery.
        """
        from .tasks import recalculate_positions_and_regenerate_pdfs

        now = timezone.now()
        scheduled_results = Result.objects.filter(
            status='SCHEDULED', scheduled_date__lte=now
        ).select_related('student')

        if not scheduled_results.exists():
            return 0

        updated_classes_terms = set()
        count = 0

        with transaction.atomic():
            for result in scheduled_results:
                result.status = 'PUBLISHED'
                result.published_date = now
                result.save(update_fields=['status', 'published_date'])

                updated_classes_terms.add((result.class_name, result.term, result.academic_year))
                EmailNotifier.send_result_published(result)
                count += 1

                logger.info(
                    f"[_auto_publish] Published scheduled result {result.id} — "
                    f"Student: {_student_full_name(result.student)}"
                )

        for class_name, term, academic_year in updated_classes_terms:
            recalculate_positions_and_regenerate_pdfs.delay(class_name, term, academic_year)
            logger.info(
                f"[_auto_publish] Dispatched Celery recalculation for "
                f"{class_name} / {term} / {academic_year}."
            )

        logger.info(f"[_auto_publish] Auto-published {count} result(s).")
        return count

    def _build_student_results_queryset(self, student_id, class_name, term, user):
        try:
            student = CustomUser.objects.get(id=student_id)
            class_name = class_name or student.class_name
        except CustomUser.DoesNotExist:
            raise ValidationError(f"Student with ID {student_id} not found")

        queryset = Result.objects.select_related('student').prefetch_related(
            Prefetch(
                'course_results',
                queryset=CourseResult.objects.select_related('class_course__course')
            )
        ).filter(student_id=student_id, class_name=class_name)

        if term:
            queryset = queryset.filter(term=term)

        if user.role not in ['staff', 'principal']:
            queryset = queryset.filter(
                Q(status='PUBLISHED') | Q(status='SCHEDULED', scheduled_date__lte=timezone.now())
            )

        return queryset

    def _build_class_results_queryset(self, class_name, term, user):
        current_students = CustomUser.objects.filter(
            class_name=class_name, role='student'
        ).values_list('id', flat=True)

        queryset = Result.objects.select_related('student').prefetch_related(
            Prefetch(
                'course_results',
                queryset=CourseResult.objects.select_related('class_course__course')
            )
        ).filter(class_name=class_name, student_id__in=current_students)

        if term:
            queryset = queryset.filter(term=term)

        if user.role not in ['staff', 'principal']:
            queryset = queryset.filter(
                Q(status='PUBLISHED') | Q(status='SCHEDULED', scheduled_date__lte=timezone.now())
            )

        return queryset


# ---------------------------------------------------------------------------
# Service classes (synchronous — used by tasks and admin)
# ---------------------------------------------------------------------------

class PositionCalculator:
    @staticmethod
    def recalculate_positions(class_name, term, academic_year="2023-2024"):
        with transaction.atomic():
            ClassSize.update_class_size(class_name, term, academic_year)

            results = Result.objects.filter(
                class_name=class_name, term=term, academic_year=academic_year
            ).select_related('student').prefetch_related(
                Prefetch(
                    'course_results',
                    queryset=CourseResult.objects.select_related('class_course__course')
                )
            )

            if not results.exists():
                logger.info(f"[PositionCalculator] No results found for {class_name} - {term}")
                return []

            changed_result_ids = PositionCalculator._calculate_overall_positions(results)
            PositionCalculator._calculate_course_positions(class_name, term, academic_year)

            logger.info(
                f"[PositionCalculator] Recalculated positions for {class_name} / {term} — "
                f"{len(changed_result_ids)} position change(s)."
            )
            return changed_result_ids

    @staticmethod
    def _calculate_overall_positions(results):
        sorted_results = sorted(
            results,
            key=lambda r: (r.total_score, r.average_score),
            reverse=True
        )

        changed_result_ids = []
        updates = []
        current_position = 1
        previous_scores = (None, None)

        for i, result in enumerate(sorted_results):
            current_scores = (result.total_score, result.average_score)

            if previous_scores[0] is not None and current_scores < previous_scores:
                current_position = i + 1

            if result.overall_position != current_position:
                result.overall_position = current_position
                updates.append(result)
                changed_result_ids.append(result.id)

            previous_scores = current_scores

        if updates:
            Result.objects.bulk_update(updates, ['overall_position'])

        return changed_result_ids

    @staticmethod
    def _calculate_course_positions(class_name, term, academic_year):
        class_courses = ClassCourse.objects.filter(class_name=class_name, term=term)

        for class_course in class_courses:
            course_results = CourseResult.objects.filter(
                class_course=class_course,
                result__class_name=class_name,
                result__term=term,
                result__academic_year=academic_year
            ).select_related('result')

            if not course_results.exists():
                continue

            sorted_course_results = sorted(
                course_results, key=lambda cr: cr.total_score, reverse=True
            )

            updates = []
            current_position = 1
            previous_score = None

            for i, course_result in enumerate(sorted_course_results):
                if previous_score is not None and course_result.total_score < previous_score:
                    current_position = i + 1

                if course_result.position != current_position:
                    course_result.position = current_position
                    updates.append(course_result)

                previous_score = course_result.total_score

            if updates:
                CourseResult.objects.bulk_update(updates, ['position'])


class PDFGenerator:
    """
    Synchronous PDF helper — used by the signal (post_save) and admin.
    Views now call the async Celery task instead.
    """

    @staticmethod
    def generate_for_result(result):
        try:
            logger.debug(f"[PDFGenerator] Starting PDF generation for result {result.id}")
            result.refresh_from_db()

            logger.info(
                f"[PDFGenerator] Generating PDF — result {result.id}, "
                f"Student: {_student_full_name(result.student)}, "
                f"Class: {result.class_name}, Term: {result.term}"
            )

            pdf_content = generate_report_card_pdf(result)
            if not pdf_content:
                logger.error(f"[PDFGenerator] Empty PDF content for result {result.id}")
                return False

            filename = result.get_report_card_filename()
            pdf_file = ContentFile(pdf_content, name=filename)
            result.report_card_pdf.save(filename, pdf_file, save=True)

            logger.info(
                f"[PDFGenerator] PDF saved for result {result.id} at '{result.report_card_pdf.url}'"
            )
            return True

        except Exception as e:
            logger.error(
                f"[PDFGenerator] Error for result {result.id}: {e}",
                exc_info=True
            )
            return False


class EmailNotifier:
    @staticmethod
    def send_result_published(result):
        try:
            configuration = Configuration()
            configuration.api_key['api-key'] = os.getenv('BREVO_API_KEY')

            api_instance = TransactionalEmailsApi(ApiClient(configuration))
            student = result.student

            send_smtp_email = SendSmtpEmail(
                to=[{"email": student.email}],
                sender={"name": "School Administration", "email": settings.DEFAULT_FROM_EMAIL},
                subject=f"Results Published - {result.class_name} {result.term}",
                html_content=f"""
                <html>
                <body>
                    <p>Dear {student.first_name} {student.last_name},</p>
                    <p>Your results for <strong>{result.class_name} - {result.term}</strong>
                       have been published.</p>
                    <p>You can view your results by logging into your student portal.</p>
                    <p>Best regards,<br>School Administration</p>
                </body>
                </html>
                """
            )

            api_instance.send_transac_email(send_smtp_email)
            logger.info(f"[EmailNotifier] Email sent to {student.email} for result {result.id}")

        except Exception as e:
            logger.error(f"[EmailNotifier] Failed to send email for result {result.id}: {e}")


class BulkStatusUpdater:
    def __init__(self, data, user):
        self.class_name = data['class_name']
        self.term = data['term']
        self.status = data['status']
        self.scheduled_date = data.get('scheduled_date')
        self.user = user

    def execute(self):
        from .tasks import bulk_update_status_task

        if self.status == 'SCHEDULED' and not self.scheduled_date:
            return Response(
                {"error": "scheduled_date required for SCHEDULED status"},
                status=status.HTTP_400_BAD_REQUEST
            )

        if self.status != 'DRAFT':
            validation_error = self._validate_completeness()
            if validation_error:
                return validation_error

        # Count how many results will be affected (for the response message)
        students = CustomUser.objects.filter(
            class_name=self.class_name, role='student'
        ).values_list('id', flat=True)

        results_qs = Result.objects.filter(
            class_name=self.class_name,
            term=self.term,
            student_id__in=students,
        )
        if self.status == 'PUBLISHED':
            results_qs = results_qs.exclude(status='PUBLISHED')

        results_count = results_qs.count()

        if results_count == 0:
            return Response({"message": "No results to update"})

        # Dispatch async
        scheduled_date_iso = (
            self.scheduled_date.isoformat() if self.scheduled_date else None
        )
        bulk_update_status_task.delay(
            self.class_name,
            self.term,
            self.status,
            scheduled_date_iso=scheduled_date_iso,
            user_email=self.user.email,
        )

        logger.info(
            f"[BulkStatusUpdater] Dispatched bulk_update_status_task for "
            f"{self.class_name} / {self.term} → {self.status} "
            f"({results_count} result(s), triggered by {self.user.email})."
        )

        return Response({
            "message": (
                f"Bulk status update to '{self.status}' dispatched for "
                f"{results_count} result(s) in {self.class_name} / {self.term}. "
                f"PDFs will be regenerated automatically."
            ),
            "queued_count": results_count,
        })

    def _validate_completeness(self):
        students = CustomUser.objects.filter(class_name=self.class_name, role='student')
        if not students.exists():
            return Response(
                {"error": f"No students found in {self.class_name}"},
                status=status.HTTP_404_NOT_FOUND
            )

        class_courses = ClassCourse.objects.filter(
            class_name=self.class_name, term=self.term
        )
        if not class_courses.exists():
            return Response(
                {"error": f"No courses found for {self.class_name} in {self.term}"},
                status=status.HTTP_404_NOT_FOUND
            )

        missing_results, incomplete_results = [], []

        for student in students:
            result = Result.objects.filter(
                student=student, class_name=self.class_name, term=self.term
            ).first()

            if not result:
                missing_results.append({
                    "student_id": student.id,
                    "student_name": f"{student.first_name} {student.last_name}",
                    "error": "No result record found"
                })
                continue

            student_courses = set(
                result.course_results.values_list('class_course_id', flat=True)
            )
            required_courses = set(class_courses.values_list('id', flat=True))

            for missing_course_id in required_courses - student_courses:
                course = ClassCourse.objects.get(id=missing_course_id)
                incomplete_results.append({
                    "student_id": student.id,
                    "student_name": f"{student.first_name} {student.last_name}",
                    "course_name": course.course.name,
                    "error": "Missing course result"
                })

        if missing_results or incomplete_results:
            return Response({
                "error": "Cannot update status due to missing/incomplete results",
                "missing_results": missing_results,
                "incomplete_results": incomplete_results
            }, status=status.HTTP_400_BAD_REQUEST)

        return None


# ---------------------------------------------------------------------------
# StudentResultsViewSet
# ---------------------------------------------------------------------------

class StudentResultsViewSet(viewsets.ReadOnlyModelViewSet):
    serializer_class = ResultSerializer
    permission_classes = [permissions.IsAuthenticated]

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

    def send_result_published_email(self, result):
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
                f"[StudentResultsViewSet] Email sent to {student.email} "
                f"for result {result.id}: {api_response}"
            )

        except ApiException as e:
            logger.error(
                f"[StudentResultsViewSet] ApiException sending email to "
                f"{result.student.email}: {e}"
            )
        except Exception as e:
            logger.error(
                f"[StudentResultsViewSet] Unexpected error sending email to "
                f"{result.student.email}: {e}"
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

                self.send_result_published_email(result)
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