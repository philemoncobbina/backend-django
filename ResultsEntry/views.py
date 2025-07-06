from rest_framework import viewsets, status, generics, filters
from rest_framework.response import Response
from rest_framework.decorators import action
from django.utils import timezone
from rest_framework import permissions
from django.shortcuts import get_object_or_404
from django.db.models import Q
from django_filters.rest_framework import DjangoFilterBackend, FilterSet
from rest_framework.exceptions import ValidationError
import logging
from datetime import datetime
from django_filters import CharFilter, ChoiceFilter
import pytz
import os
from django.conf import settings
from sib_api_v3_sdk import Configuration, ApiClient, SendSmtpEmail
from sib_api_v3_sdk.api.transactional_emails_api import TransactionalEmailsApi
from sib_api_v3_sdk.rest import ApiException
from authapp.models import CustomUser
from .permissions import IsStaffOrPrincipal, IsOwnerOrReadOnly, IsOwnerOrStaffOrPrincipal, IsPrincipal, PublishedResultsOnlyPrincipal
from .models import Course, ClassCourse, Result, CourseResult, ResultChangeLog, ClassSize
from django.db import transaction
from django.http import HttpResponse
from .utils.pdf_generator import generate_report_card_pdf
from django.core.files.base import ContentFile

from .serializers import (
    CourseSerializer, ClassCourseSerializer, ClassCourseDetailSerializer, ResultSerializer, ResultCreateSerializer, CourseResultSerializer, ResultChangeLogSerializer, StudentSerializer,
    BulkResultUpdateSerializer
)


logger = logging.getLogger(__name__)

class ResultFilter(FilterSet):
    student = CharFilter(field_name='student')
    class_name = CharFilter(field_name='class_name')
    term = CharFilter(field_name='term')
    status = CharFilter(field_name='status')

    class Meta:
        model = Result
        fields = ['student', 'class_name', 'term', 'status']

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
            return Response({"error": "Both class_name and term parameters are required"}, status=status.HTTP_400_BAD_REQUEST)
        queryset = self.get_queryset().filter(class_name=class_name, term=term)
        serializer = ClassCourseDetailSerializer(queryset, many=True)
        return Response(serializer.data)

    @action(detail=False, methods=['post'])
    def bulk_assign(self, request):
        data = request.data
        if not isinstance(data, list):
            return Response({"error": "Expected a list of assignments"}, status=status.HTTP_400_BAD_REQUEST)
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
        return Response(response_data, status=status.HTTP_207_MULTI_STATUS if errors else status.HTTP_201_CREATED)


from django.db import transaction
from django.db.models import Q, F, Prefetch
from django.utils import timezone
from django.core.files.base import ContentFile
from django.conf import settings
from django_filters.rest_framework import DjangoFilterBackend
from rest_framework import viewsets, permissions, status, filters
from rest_framework.decorators import action
from rest_framework.response import Response
from rest_framework.exceptions import ValidationError
from datetime import datetime
import pytz
import os
import logging

logger = logging.getLogger(__name__)


from django.db import transaction
from django.db.models import Q, F, Prefetch
from django.utils import timezone
from django.core.files.base import ContentFile
from django.conf import settings
from django_filters.rest_framework import DjangoFilterBackend
from rest_framework import viewsets, permissions, status, filters
from rest_framework.decorators import action
from rest_framework.response import Response
from rest_framework.exceptions import ValidationError
from datetime import datetime
import pytz
import os
import logging

logger = logging.getLogger(__name__)
class ResultViewSet(viewsets.ModelViewSet):
    # Base queryset without prefetch - we'll add it in get_queryset
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
        """Optimize queryset with proper prefetching"""
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
        self._validate_create_request(request.data)
        
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        
        with transaction.atomic():
            instance = serializer.save()
            self._handle_post_create_tasks(instance)
        
        response_serializer = ResultSerializer(instance)
        return Response(response_serializer.data, status=status.HTTP_201_CREATED)
    
    def update(self, request, *args, **kwargs):
        partial = kwargs.pop('partial', False)
        instance = self.get_object()
        old_data = self._capture_old_data(instance)
        
        if request.data.get('status') == 'SCHEDULED':
            self._validate_scheduled_date(request.data.get('scheduled_date'))
            
        serializer = self.get_serializer(instance, data=request.data, partial=partial)
        serializer.is_valid(raise_exception=True)
        
        with transaction.atomic():
            updated_instance = serializer.save()
            self._handle_post_update_tasks(updated_instance, old_data)
        
        response_serializer = ResultSerializer(updated_instance)
        return Response(response_serializer.data)
    
    def destroy(self, request, *args, **kwargs):
        instance = self.get_object()
        class_info = (instance.class_name, instance.term, instance.academic_year)
        
        with transaction.atomic():
            super().destroy(request, *args, **kwargs)
            PositionCalculator.recalculate_positions(*class_info)
        
        return Response(status=status.HTTP_204_NO_CONTENT)

    # Action methods
    @action(detail=False, methods=['get'])
    def get_student_results(self, request):
        self._auto_publish_scheduled_results()
        
        student_id = request.query_params.get('student')
        if not student_id:
            return Response({"error": "student parameter is required"}, 
                          status=status.HTTP_400_BAD_REQUEST)
        
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
            return Response({"error": "class_name parameter is required"}, 
                          status=status.HTTP_400_BAD_REQUEST)
        
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
            return Response({"error": "Both class_name and term parameters are required"}, 
                          status=status.HTTP_400_BAD_REQUEST)
        
        class_courses = ClassCourse.objects.filter(
            class_name=class_name, term=term
        ).select_related('course')
        serializer = ClassCourseSerializer(class_courses, many=True)
        return Response(serializer.data)
    
    @action(detail=False, methods=['get'])
    def get_students_by_class(self, request):
        class_name = request.query_params.get('class_name')
        if not class_name:
            return Response({"error": "class_name parameter is required"}, 
                          status=status.HTTP_400_BAD_REQUEST)
        
        students = CustomUser.objects.filter(class_name=class_name, role='student')
        serializer = StudentSerializer(students, many=True)
        return Response(serializer.data)
    
    @action(detail=False, methods=['post'])
    def recalculate_positions(self, request):
        class_name = request.data.get('class_name')
        term = request.data.get('term')
        academic_year = request.data.get('academic_year', '2023-2024')
        
        if not class_name or not term:
            return Response({"error": "Both class_name and term are required"},
                          status=status.HTTP_400_BAD_REQUEST)
        
        results_count = Result.objects.filter(
            class_name=class_name, term=term, academic_year=academic_year
        ).count()
        
        if results_count == 0:
            return Response({"error": f"No results found for {class_name}, {term}, {academic_year}"},
                          status=status.HTTP_404_NOT_FOUND)
        
        PositionCalculator.recalculate_positions(class_name, term, academic_year)
        
        return Response({
            "message": f"Positions recalculated successfully for {results_count} results",
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

    # Helper methods
    def _validate_create_request(self, data):
        if data.get('status') == 'SCHEDULED':
            self._validate_scheduled_date(data.get('scheduled_date'))
        
        # Check for duplicate
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

    def _handle_post_create_tasks(self, instance):
        """Handle tasks after result creation"""
        # Generate PDF
        PDFGenerator.generate_for_result(instance)
        
        # Recalculate positions
        PositionCalculator.recalculate_positions(
            instance.class_name, instance.term, instance.academic_year
        )
        
        # Send email if published
        if instance.status == 'PUBLISHED':
            EmailNotifier.send_result_published(instance)

    def _handle_post_update_tasks(self, instance, old_data):
        """Handle tasks after result update"""
        status_changed = old_data['status'] != instance.status
        location_changed = any([
            old_data['class_name'] != instance.class_name,
            old_data['term'] != instance.term,
            old_data['academic_year'] != instance.academic_year
        ])
        
        # Check if scores changed (which would affect positions)
        scores_changed = getattr(instance, '_scores_changed', False)
        
        # Handle PDF regeneration for the current instance
        if status_changed or getattr(instance, '_regenerate_pdf', False):
            PDFGenerator.generate_for_result(instance)
        elif instance.status == 'PUBLISHED' and not instance.report_card_pdf:
            PDFGenerator.generate_for_result(instance)
        
        # Send email if newly published
        if old_data['status'] != 'PUBLISHED' and instance.status == 'PUBLISHED':
            EmailNotifier.send_result_published(instance)
        
        # Recalculate positions and get changed result IDs
        changed_result_ids = PositionCalculator.recalculate_positions(
            instance.class_name, instance.term, instance.academic_year
        )
        
        # If scores changed, regenerate PDFs for ALL results in the class/term
        if scores_changed:
            self._regenerate_pdfs_for_all_results_in_class(
                instance.class_name, 
                instance.term, 
                instance.academic_year,
                exclude_result_id=instance.id,  # Don't regenerate the current result again
                changed_result_ids=changed_result_ids  # Pass for logging purposes
            )
        
        # Recalculate positions for old location if changed
        if location_changed:
            changed_result_ids_old = PositionCalculator.recalculate_positions(
                old_data['class_name'], old_data['term'], old_data['academic_year']
            )
            # Regenerate PDFs for ALL results in old location
            self._regenerate_pdfs_for_all_results_in_class(
                old_data['class_name'], 
                old_data['term'], 
                old_data['academic_year'],
                changed_result_ids=changed_result_ids_old
            )

    def _regenerate_pdfs_for_all_results_in_class(self, class_name, term, academic_year, 
                                                 exclude_result_id=None, changed_result_ids=None):
        """
        Regenerate PDFs for ALL results in a class/term regardless of status.
        This ensures all students have current positions on their report cards.
        """
        logger.info(f"Starting PDF regeneration for ALL results in {class_name} - {term} - {academic_year}")
        
        # Get ALL results in the class/term
        all_results = Result.objects.filter(
            class_name=class_name,
            term=term,
            academic_year=academic_year
        ).select_related('student')
        
        # Exclude the current result if specified (already regenerated)
        if exclude_result_id:
            all_results = all_results.exclude(id=exclude_result_id)
        
        if not all_results.exists():
            logger.info(f"No results found for PDF regeneration in {class_name} - {term} - {academic_year}")
            return
        
        total_results = all_results.count()
        logger.info(f"Found {total_results} results for PDF regeneration")
        
        # Convert changed_result_ids to set for faster lookup
        changed_ids_set = set(changed_result_ids) if changed_result_ids else set()
        
        success_count = 0
        failed_count = 0
        position_changed_count = 0
        position_unchanged_count = 0
        
        # Regenerate PDFs for ALL results
        for result in all_results:
            try:
                # Check if this result had a position change
                had_position_change = result.id in changed_ids_set
                
                if had_position_change:
                    position_changed_count += 1
                    logger.info(
                        f"Regenerating PDF for result ID {result.id} "
                        f"(Student: {result.student.first_name} {result.student.last_name}) "
                        f"- POSITION CHANGED - Status: {result.status}"
                    )
                else:
                    position_unchanged_count += 1
                    logger.info(
                        f"Regenerating PDF for result ID {result.id} "
                        f"(Student: {result.student.first_name} {result.student.last_name}) "
                        f"- Position unchanged but ensuring current class positions - Status: {result.status}"
                    )
                
                # Generate PDF regardless of status
                if PDFGenerator.generate_for_result(result):
                    success_count += 1
                    logger.info(f"Successfully regenerated PDF for result ID {result.id}")
                else:
                    failed_count += 1
                    logger.error(f"PDF generation failed for result ID {result.id}")
                    
            except Exception as e:
                failed_count += 1
                logger.error(
                    f"Error regenerating PDF for result ID {result.id}: {str(e)}",
                    exc_info=True
                )
        
        # Summary logging
        logger.info(
            f"PDF regeneration completed for {class_name} - {term} - {academic_year}: "
            f"Total: {total_results}, Success: {success_count}, Failed: {failed_count}, "
            f"Position Changed: {position_changed_count}, Position Unchanged: {position_unchanged_count}"
        )
        
        if failed_count > 0:
            logger.warning(
                f"PDF regeneration had {failed_count} failures out of {total_results} attempts "
                f"for {class_name} - {term} - {academic_year}"
            )
        
        # Log specific results that had position changes
        if changed_result_ids:
            logger.info(
                f"Results with position changes in {class_name} - {term} - {academic_year}: "
                f"{len(changed_result_ids)} results (IDs: {changed_result_ids})"
            )

    def _auto_publish_scheduled_results(self):
        """Auto-publish scheduled results that are due"""
        now = timezone.now()
        scheduled_results = Result.objects.filter(
            status='SCHEDULED', scheduled_date__lte=now
        ).select_related('student')
        
        if not scheduled_results.exists():
            return 0
        
        updated_classes_terms = set()
        
        with transaction.atomic():
            for result in scheduled_results:
                result.status = 'PUBLISHED'
                result.published_date = now
                result.save(update_fields=['status', 'published_date'])
                
                updated_classes_terms.add((result.class_name, result.term, result.academic_year))
                EmailNotifier.send_result_published(result)
        
        # Recalculate positions for affected classes
        for class_info in updated_classes_terms:
            PositionCalculator.recalculate_positions(*class_info)
        
        count = len(scheduled_results)
        if count > 0:
            logger.info(f"Auto-published {count} scheduled results")
        
        return count

    def _build_student_results_queryset(self, student_id, class_name, term, user):
        """Build queryset for student results"""
        try:
            student = CustomUser.objects.get(id=student_id)
            class_name = class_name or student.class_name
        except CustomUser.DoesNotExist:
            raise ValidationError(f"Student with ID {student_id} not found")
        
        # Build queryset with proper prefetch
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
        """Build queryset for class results"""
        current_students = CustomUser.objects.filter(
            class_name=class_name, role='student'
        ).values_list('id', flat=True)
        
        # Build queryset with proper prefetch
        queryset = Result.objects.select_related('student').prefetch_related(
            Prefetch(
                'course_results', 
                queryset=CourseResult.objects.select_related('class_course__course')
            )
        ).filter(
            class_name=class_name, student_id__in=current_students
        )
        
        if term:
            queryset = queryset.filter(term=term)
        
        if user.role not in ['staff', 'principal']:
            queryset = queryset.filter(
                Q(status='PUBLISHED') | Q(status='SCHEDULED', scheduled_date__lte=timezone.now())
            )
        
        return queryset


# Separate service classes for better organization
class PositionCalculator:
    @staticmethod
    def recalculate_positions(class_name, term, academic_year="2023-2024"):
        """Recalculate positions for all results in a class and term"""
        with transaction.atomic():
            # Update class size
            ClassSize.update_class_size(class_name, term, academic_year)
            
            # Get all results with optimized query
            results = Result.objects.filter(
                class_name=class_name, term=term, academic_year=academic_year
            ).select_related('student').prefetch_related(
                Prefetch(
                    'course_results', 
                    queryset=CourseResult.objects.select_related('class_course__course')
                )
            )
            
            if not results.exists():
                logger.info(f"No results found for {class_name} - {term}")
                return []
            
            # Calculate overall positions and track changes
            changed_result_ids = PositionCalculator._calculate_overall_positions(results)
            
            # Calculate course positions
            PositionCalculator._calculate_course_positions(class_name, term, academic_year)
            
            return changed_result_ids

    @staticmethod
    def _calculate_overall_positions(results):
        """Calculate overall positions for results and return IDs of changed results"""
        # Sort by total score, then average score (descending)
        sorted_results = sorted(
            results, 
            key=lambda r: (r.total_score, r.average_score), 
            reverse=True
        )
        
        # Track which results have position changes
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
        
        # Bulk update positions
        if updates:
            Result.objects.bulk_update(updates, ['overall_position'])
        
        return changed_result_ids

    @staticmethod
    def _calculate_course_positions(class_name, term, academic_year):
        """Calculate course positions for all courses in a class and term"""
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
            
            # Sort by total score (descending)
            sorted_course_results = sorted(
                course_results, key=lambda cr: cr.total_score, reverse=True
            )
            
            # Calculate positions and prepare for bulk update
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
            
            # Bulk update
            if updates:
                CourseResult.objects.bulk_update(updates, ['position'])


class PDFGenerator:
    @staticmethod
    def generate_for_result(result):
        """Generate PDF for a result with detailed logging"""
        try:
            logger.debug(f"Starting PDF generation for result ID {result.id}")
            result.refresh_from_db()
            
            logger.info(f"Generating PDF for result {result.id} - Student: {result.student.first_name}, "
                      f"Class: {result.class_name}, Term: {result.term}")
            
            pdf_content = generate_report_card_pdf(result)
            if not pdf_content:
                logger.error(f"PDF generation returned empty content for result {result.id}")
                return False
            
            filename = result.get_report_card_filename()
            logger.debug(f"Generated filename: {filename}")
            
            pdf_file = ContentFile(pdf_content, name=filename)
            
            # Save the PDF file
            result.report_card_pdf.save(filename, pdf_file, save=True)
            logger.info(f"PDF saved successfully for result {result.id} at {result.report_card_pdf.url}")
            
            return True
            
        except Exception as e:
            logger.error(
                f"PDF generation error for result {result.id}: {str(e)}",
                exc_info=True
            )
            return False

class EmailNotifier:
    @staticmethod
    def send_result_published(result):
        """Send email notification when result is published"""
        try:
            from sib_api_v3_sdk import Configuration, ApiClient, TransactionalEmailsApi, SendSmtpEmail
            from sib_api_v3_sdk.rest import ApiException
            
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
            logger.info(f"Email sent to {student.email} for result {result.id}")
            
        except Exception as e:
            logger.error(f"Failed to send email for result {result.id}: {e}")


class BulkStatusUpdater:
    def __init__(self, data, user):
        self.class_name = data['class_name']
        self.term = data['term']
        self.status = data['status']
        self.scheduled_date = data.get('scheduled_date')
        self.user = user
        
    def execute(self):
        """Execute bulk status update"""
        if self.status == 'SCHEDULED' and not self.scheduled_date:
            return Response(
                {"error": "scheduled_date required for SCHEDULED status"},
                status=status.HTTP_400_BAD_REQUEST
            )
        
        # Validate completeness if not updating to DRAFT
        if self.status != 'DRAFT':
            validation_error = self._validate_completeness()
            if validation_error:
                return validation_error
        
        # Perform bulk update
        return self._perform_bulk_update()
    
    def _validate_completeness(self):
        """Validate that all results are complete"""
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
        
        # Check for missing/incomplete results
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
            
            # Check course completeness
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
    
    def _perform_bulk_update(self):
        """Perform the actual bulk update"""
        students = CustomUser.objects.filter(
            class_name=self.class_name, role='student'
        ).values_list('id', flat=True)
        
        # Get results to update
        results_query = Result.objects.filter(
            class_name=self.class_name, term=self.term, student_id__in=students
        )
        
        if self.status == 'PUBLISHED':
            results_query = results_query.exclude(status='PUBLISHED')
        
        results = list(results_query.select_related('student'))
        
        if not results:
            return Response({"message": "No results to update"})
        
        # Perform updates
        with transaction.atomic():
            updated_count = self._update_results(results)
            
            # Recalculate positions for the entire class
            changed_result_ids = PositionCalculator.recalculate_positions(
                self.class_name, self.term
            )
            
            # Regenerate PDFs for ALL results in the class (not just updated ones)
            self._regenerate_all_pdfs_in_class(changed_result_ids)
        
        return Response({
            "message": f"Successfully updated {updated_count} results to {self.status}",
            "updated_count": updated_count
        })
    
    def _regenerate_all_pdfs_in_class(self, changed_result_ids):
        """Regenerate PDFs for ALL results in the class"""
        logger.info(f"Starting PDF regeneration for ALL results in {self.class_name} - {self.term} (Bulk Update)")
        
        # Get ALL results in the class/term
        all_results = Result.objects.filter(
            class_name=self.class_name,
            term=self.term
        ).select_related('student')
        
        if not all_results.exists():
            logger.info(f"No results found for PDF regeneration in {self.class_name} - {self.term}")
            return
        
        changed_ids_set = set(changed_result_ids) if changed_result_ids else set()
        total_results = all_results.count()
        success_count = 0
        failed_count = 0
        
        logger.info(f"Regenerating PDFs for ALL {total_results} results in {self.class_name} - {self.term}")
        
        for result in all_results:
            try:
                had_position_change = result.id in changed_ids_set
                
                logger.info(
                    f"Regenerating PDF for result ID {result.id} "
                    f"(Student: {result.student.first_name} {result.student.last_name}) "
                    f"- Status: {result.status} - Position Changed: {had_position_change}"
                )
                
                if PDFGenerator.generate_for_result(result):
                    success_count += 1
                else:
                    failed_count += 1
                    
            except Exception as e:
                failed_count += 1
                logger.error(f"Error regenerating PDF for result ID {result.id}: {str(e)}")
        
        logger.info(
            f"Bulk PDF regeneration completed for {self.class_name} - {self.term}: "
            f"Total: {total_results}, Success: {success_count}, Failed: {failed_count}"
        )
    
    def _update_results(self, results):
        """Update individual results"""
        updated_count = 0
        
        for result in results:
            old_status = result.status
            
            # Update status and related fields
            result.status = self.status
            if self.status == 'PUBLISHED':
                result.published_date = timezone.now()
            elif self.status == 'SCHEDULED':
                result.scheduled_date = self.scheduled_date
            elif self.status == 'DRAFT':
                result.scheduled_date = None
                result.published_date = None
            
            result.save(update_fields=['status', 'published_date', 'scheduled_date'])
            updated_count += 1
            
            # Handle side effects
            if old_status != 'PUBLISHED' and self.status == 'PUBLISHED':
                EmailNotifier.send_result_published(result)
            
            # NOTE: We don't regenerate PDFs here individually anymore
            # They will be regenerated for ALL results in _regenerate_all_pdfs_in_class()
            
            # Log the change
            ResultChangeLog.objects.create(
                result=result,
                changed_by=self.user.email,
                field_name="status (bulk update)",
                previous_value=old_status,
                new_value=self.status
            )
        
        return updated_count
            
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
        """
        Send email notification when result is published
        """
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
                    <p>Your academic results for <strong>{result.class_name}</strong> - <strong>{result.term} term </strong> have been published and are now available for viewing.</p>
                    <p>You can access your results by logging into your student portal.</p>
                    <p>If you have any questions about your results, please contact your class teacher or the school administration.</p>
                    <p>Best regards,<br>School Administration</p>
                </body>
                </html>
                """
            )
            
            api_response = api_instance.send_transac_email(send_smtp_email)
            logger.info(f"Result published email sent successfully to {student.email}: {api_response}")
            
        except ApiException as e:
            logger.error(f"Exception when sending result published email to {student.email}: {e}")
        except Exception as e:
            logger.error(f"Unexpected error when sending result published email to {student.email}: {e}")
    
    def _check_scheduled_results(self):
        try:
            now = timezone.now()
            scheduled_results = Result.objects.filter(
                status='SCHEDULED',
                scheduled_date__lte=now
            )
            
            published_count = 0
            for result in scheduled_results:
                logger.info(f"Publishing scheduled result: {result.id} - {result.student.first_name} {result.student.last_name}")
                result.status = 'PUBLISHED'
                result.published_date = now
                result.save()
                
                # Send email notification for automatically published results
                self.send_result_published_email(result)
                
                published_count += 1
                
            return published_count
        except Exception as e:
            logger.error(f"Error while checking scheduled results: {str(e)}")
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
        """
        Get only the results for the student's current class.
        """
        user = request.user
        
        if not user.role == 'student':
            return Response(
                {"detail": "Only students can access this endpoint."}, 
                status=status.HTTP_403_FORBIDDEN
            )
        
        self._check_scheduled_results()
        
        # Get results for current class only
        queryset = Result.objects.filter(
            student=user,
            class_name=user.class_name,  # Use the student's current class
            status__in=['PUBLISHED', 'SCHEDULED']
        ).filter(
            Q(status='PUBLISHED') | 
            Q(status='SCHEDULED', scheduled_date__lte=timezone.now())
        )
        
        # Allow filtering by term
        term = request.query_params.get('term')
        if term:
            queryset = queryset.filter(term=term)
        
        serializer = self.get_serializer(queryset, many=True)
        return Response(serializer.data)
    
    @action(detail=False, methods=['get'])
    def previous_classes(self, request):
        """
        Get results for the student's previous classes (excluding current class).
        """
        user = request.user
        
        if not user.role == 'student':
            return Response(
                {"detail": "Only students can access this endpoint."}, 
                status=status.HTTP_403_FORBIDDEN
            )
        
        # Check for required parameters
        class_name = request.query_params.get('class_name')
        term = request.query_params.get('term')
        
        if not class_name or not term:
            return Response(
                {"error": "Both class_name and term parameters are required"}, 
                status=status.HTTP_400_BAD_REQUEST
            )
        
        self._check_scheduled_results()
        
        # Get current class name
        current_class = user.class_name
        
        # Check if student has class_history attribute
        if not hasattr(user, 'class_history'):
            return Response(
                {"detail": "No class history available for this student."}, 
                status=status.HTTP_404_NOT_FOUND
            )
        
        # Get previous classes from history, excluding current class
        previous_classes = user.class_history.exclude(
            class_name=current_class
        ).values_list('class_name', flat=True).distinct()
        
        if not previous_classes:
            return Response(
                {"detail": "No previous class history found (excluding current class)."}, 
                status=status.HTTP_404_NOT_FOUND
            )
        
        # Check if requested class is in previous classes
        if class_name not in previous_classes:
            return Response(
                {"detail": f"Student has no history in class {class_name}."}, 
                status=status.HTTP_404_NOT_FOUND
            )
        
        # Get results for specific class and term only
        queryset = Result.objects.filter(
            student=user,
            class_name=class_name,
            term=term,
            status__in=['PUBLISHED', 'SCHEDULED']
        ).filter(
            Q(status='PUBLISHED') | 
            Q(status='SCHEDULED', scheduled_date__lte=timezone.now())
        )
        
        # If no results found for this specific class and term
        if not queryset.exists():
            return Response(
                {"detail": f"No results found for class {class_name}, term {term}."}, 
                status=status.HTTP_404_NOT_FOUND
            )
        
        serializer = self.get_serializer(queryset, many=True)
        return Response(serializer.data)