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
from .models import Course, ClassCourse, Result, CourseResult, ResultChangeLog

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

class ResultViewSet(viewsets.ModelViewSet):
    queryset = Result.objects.all()
    serializer_class = ResultSerializer
    permission_classes = [PublishedResultsOnlyPrincipal]
    filter_backends = [DjangoFilterBackend, filters.SearchFilter, filters.OrderingFilter]
    filterset_class = ResultFilter
    search_fields = ['student__first_name', 'student__last_name', 'class_name']
    ordering_fields = ['student__last_name', 'class_name', 'term', 'status']

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
                subject=f"Your Results for {result.class_name} - {result.term} Have Been Published",
                html_content=f"""
                <html>
                <body>
                    <p>Dear {student_name},</p>
                    <p>Your academic results for <strong>{result.class_name}</strong> - <strong>{result.term}</strong> have been published and are now available for viewing.</p>
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

    def get_serializer_class(self):
        if self.action == 'bulk_update_status':
            return BulkResultUpdateSerializer
        return ResultCreateSerializer if self.action in ['create', 'update', 'partial_update'] else ResultSerializer

    def get_permissions(self):
        # For list and retrieve actions, use less restrictive permissions
        if self.action in ['list', 'retrieve']:
            return [permissions.IsAuthenticated()]
        # For student-specific endpoints
        elif self.action in ['get_student_results', 'get_class_results', 'get_available_courses', 'get_students_by_class']:
            return [permissions.IsAuthenticated()]
        # For bulk update, require staff or principal
        elif self.action == 'bulk_update_status':
            return [IsStaffOrPrincipal()]
        # For all other actions (create, update, delete), use PublishedResultsOnlyPrincipal
        return [PublishedResultsOnlyPrincipal()]

    def perform_update(self, serializer):
        try:
            instance = self.get_object()
            old_data = ResultSerializer(instance).data
            old_status = instance.status
            
            updated_instance = serializer.save()
            
            # Check if status changed to PUBLISHED and send email
            if old_status != 'PUBLISHED' and updated_instance.status == 'PUBLISHED':
                self.send_result_published_email(updated_instance)
            
            logger.info(f"Result {updated_instance.id} updated from {old_data} to {ResultSerializer(updated_instance).data}")
        except Exception as e:
            logger.error(f"Update failed: {e}")
            raise

    def _check_scheduled_results(self):
        now = timezone.now()
        scheduled_results = Result.objects.filter(status='SCHEDULED', scheduled_date__lte=now)
        count = 0
        for result in scheduled_results:
            result.status = 'PUBLISHED'
            result.published_date = now
            result.save()
            
            # Send email notification for automatically published results
            self.send_result_published_email(result)
            
            count += 1
        return count

    def _validate_scheduled_date(self, scheduled_date):
        if not scheduled_date:
            return None
        if isinstance(scheduled_date, str):
            try:
                scheduled_date = datetime.fromisoformat(scheduled_date.replace('Z', '+00:00'))
                scheduled_date = pytz.utc.localize(scheduled_date.replace(tzinfo=None))
            except ValueError:
                raise ValidationError({'scheduled_date': 'Invalid date format. Use ISO format (YYYY-MM-DDTHH:MM:SSZ)'})
        if scheduled_date <= timezone.now():
            raise ValidationError({'scheduled_date': 'Scheduled date must be in the future'})
        return scheduled_date

    def list(self, request, *args, **kwargs):
        self._check_scheduled_results()
        return super().list(request, *args, **kwargs)
    
    def retrieve(self, request, *args, **kwargs):
        self._check_scheduled_results()
        return super().retrieve(request, *args, **kwargs)
    
    def create(self, request, *args, **kwargs):
        if request.data.get('status') == 'SCHEDULED':
            self._validate_scheduled_date(request.data.get('scheduled_date'))
        
        # Check for duplicate result
        student_id = request.data.get('student')
        class_name = request.data.get('class_name')
        term = request.data.get('term')
        
        if student_id and class_name and term:
            existing_result = Result.objects.filter(
                student_id=student_id,
                class_name=class_name,
                term=term
            ).first()
            
            if existing_result:
                student = CustomUser.objects.get(id=student_id)
                error_message = f"Result already exists for student {student.first_name} {student.last_name} in class {class_name}, term {term}"
                logger.error(error_message)
                return Response({"error": error_message}, status=status.HTTP_400_BAD_REQUEST)
        
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        self.perform_create(serializer)
        
        result = Result.objects.get(pk=serializer.instance.pk)
        
        # Send email if result is created with PUBLISHED status
        if result.status == 'PUBLISHED':
            self.send_result_published_email(result)
        
        response_serializer = ResultSerializer(result)
        
        headers = self.get_success_headers(serializer.data)
        return Response(response_serializer.data, status=status.HTTP_201_CREATED, headers=headers)
    
    def update(self, request, *args, **kwargs):
        if request.data.get('status') == 'SCHEDULED':
            self._validate_scheduled_date(request.data.get('scheduled_date'))
            
        partial = kwargs.pop('partial', False)
        instance = self.get_object()
        
        serializer = self.get_serializer(instance, data=request.data, partial=partial)
        serializer.is_valid(raise_exception=True)
        
        serializer.context['request'] = request
        self.perform_update(serializer)
        
        updated_instance = self.get_object()
        response_serializer = ResultSerializer(updated_instance)
        
        return Response(response_serializer.data)
    
    def destroy(self, request, *args, **kwargs):
        instance = self.get_object()
        self.perform_destroy(instance)
        return Response(status=status.HTTP_204_NO_CONTENT)
    
    @action(detail=False, methods=['get'])
    def get_student_results(self, request):
        self._check_scheduled_results()
        
        student_id = request.query_params.get('student')
        class_name = request.query_params.get('class_name')
        term = request.query_params.get('term')
        
        if not student_id:
            return Response(
                {"error": "student parameter is required"}, 
                status=status.HTTP_400_BAD_REQUEST
            )
        
        # Get the student's current class
        try:
            student = CustomUser.objects.get(id=student_id)
            current_class = student.class_name
        except CustomUser.DoesNotExist:
            return Response(
                {"error": f"Student with ID {student_id} not found"}, 
                status=status.HTTP_404_NOT_FOUND
            )
        
        # If class_name is not specified, use the student's current class
        if not class_name:
            class_name = current_class
        
        queryset = self.get_queryset().filter(student_id=student_id, class_name=class_name)
        
        if term:
            queryset = queryset.filter(term=term)
        
        if not request.user.role in ['staff', 'principal']:
            queryset = queryset.filter(
                Q(status='PUBLISHED') | 
                Q(status='SCHEDULED', scheduled_date__lte=timezone.now())
            )
        
        serializer = self.get_serializer(queryset, many=True)
        return Response(serializer.data)(serializer.data)
    
    @action(detail=False, methods=['get'])
    def get_class_results(self, request):
        self._check_scheduled_results()
        
        class_name = request.query_params.get('class_name')
        term = request.query_params.get('term')
        
        if not class_name:
            return Response(
                {"error": "class_name parameter is required"}, 
                status=status.HTTP_400_BAD_REQUEST
            )
        
        # Get all students currently in this class
        current_students = CustomUser.objects.filter(
            class_name=class_name, 
            role='student'
        ).values_list('id', flat=True)
        
        # Filter results by both class_name and students currently in this class
        queryset = self.get_queryset().filter(
            class_name=class_name,
            student_id__in=current_students
        )
        
        if term:
            queryset = queryset.filter(term=term)
        
        if not request.user.role in ['staff', 'principal']:
            queryset = queryset.filter(
                Q(status='PUBLISHED') | 
                Q(status='SCHEDULED', scheduled_date__lte=timezone.now())
            )
        
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
        
        class_courses = ClassCourse.objects.filter(class_name=class_name, term=term)
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
        
        # Get only students currently assigned to this class
        students = CustomUser.objects.filter(class_name=class_name, role='student')
        serializer = StudentSerializer(students, many=True)
        
        return Response(serializer.data)
        
    @action(detail=False, methods=['post'])
    def bulk_update_status(self, request):
        """
        Bulk update the status of all results for a specific class and term.
        Validates that all students have complete results before updating.
        Ignores already published results when the new status is 'PUBLISHED'.
        """
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        
        class_name = serializer.validated_data['class_name']
        term = serializer.validated_data['term']
        new_status = serializer.validated_data['status']
        scheduled_date = serializer.validated_data.get('scheduled_date')
        
        # If status is SCHEDULED, validate the scheduled_date
        if new_status == 'SCHEDULED':
            if not scheduled_date:
                return Response(
                    {"error": "scheduled_date is required when status is SCHEDULED"},
                    status=status.HTTP_400_BAD_REQUEST
                )
            scheduled_date = self._validate_scheduled_date(scheduled_date)
        
        # Get all students currently in the class
        students = CustomUser.objects.filter(class_name=class_name, role='student')
        if not students.exists():
            return Response(
                {"error": f"No students found in class {class_name}"},
                status=status.HTTP_404_NOT_FOUND
            )
        
        # Get all courses assigned to this class and term
        class_courses = ClassCourse.objects.filter(class_name=class_name, term=term)
        if not class_courses.exists():
            return Response(
                {"error": f"No courses found for class {class_name} in term {term}"},
                status=status.HTTP_404_NOT_FOUND
            )
        
        # Find missing results for any student
        missing_results = []
        incomplete_results = []
        
        for student in students:
            # Check if the student has a result record for this class and term
            student_result = Result.objects.filter(
                student=student,
                class_name=class_name,
                term=term
            ).first()
            
            if not student_result:
                missing_results.append({
                    "student_id": student.id,
                    "student_name": f"{student.first_name} {student.last_name}",
                    "error": "No result record found"
                })
                continue
                
            # Check if the student has results for all required courses
            student_course_results = CourseResult.objects.filter(result=student_result)
            student_course_ids = {cr.class_course_id for cr in student_course_results}
            
            for class_course in class_courses:
                if class_course.id not in student_course_ids:
                    incomplete_results.append({
                        "student_id": student.id,
                        "student_name": f"{student.first_name} {student.last_name}",
                        "course_id": class_course.course.id,
                        "course_name": class_course.course.name,
                        "error": "Missing course result"
                    })
        
        # If there are missing or incomplete results, return error
        if missing_results or incomplete_results:
            return Response({
                "error": "Cannot update status due to missing or incomplete results",
                "missing_results": missing_results,
                "incomplete_results": incomplete_results
            }, status=status.HTTP_400_BAD_REQUEST)
        
        # Update only results for students currently in this class
        student_ids = students.values_list('id', flat=True)
        
        # If new status is PUBLISHED, only update results that are not already published
        if new_status == 'PUBLISHED':
            results = Result.objects.filter(
                class_name=class_name, 
                term=term,
                student_id__in=student_ids
            ).exclude(status='PUBLISHED')  # Exclude already published results
        else:
            results = Result.objects.filter(
                class_name=class_name, 
                term=term,
                student_id__in=student_ids
            )
            
        updated_count = 0
        skipped_count = 0
        user_email = request.user.email
        
        for result in results:
            old_status = result.status
            old_date = result.scheduled_date
            
            # Update status
            result.status = new_status
            
            # Update dates based on new status
            if new_status == 'PUBLISHED':
                result.published_date = timezone.now()
            elif new_status == 'SCHEDULED':
                result.scheduled_date = scheduled_date
            elif new_status == 'DRAFT':
                # For draft, clear scheduled and published dates
                result.scheduled_date = None
                result.published_date = None
            
            result.save()
            updated_count += 1
            
            # Send email if status changed to PUBLISHED
            if old_status != 'PUBLISHED' and new_status == 'PUBLISHED':
                self.send_result_published_email(result)
            
            # Create log entry for the change
            ResultChangeLog.objects.create(
                result=result,
                changed_by=user_email,
                field_name="status (bulk update)",
                previous_value=old_status,
                new_value=new_status
            )
            
            if old_date != result.scheduled_date:
                # Create log entry for scheduled date change if it changed
                ResultChangeLog.objects.create(
                    result=result,
                    changed_by=user_email,
                    field_name="scheduled_date (bulk update)",
                    previous_value=str(old_date) if old_date else "None",
                    new_value=str(result.scheduled_date) if result.scheduled_date else "None"
                )
        
        # Count skipped results if new status is PUBLISHED
        if new_status == 'PUBLISHED':
            skipped_count = Result.objects.filter(
                class_name=class_name, 
                term=term,
                student_id__in=student_ids,
                status='PUBLISHED'
            ).count()
            
            response_msg = f"Successfully updated {updated_count} results to status '{new_status}'"
            if skipped_count > 0:
                response_msg += f" (skipped {skipped_count} already published results)"
        else:
            response_msg = f"Successfully updated {updated_count} results to status '{new_status}'"
        
        return Response({
            "message": response_msg,
            "class_name": class_name,
            "term": term,
            "status": new_status,
            "updated_count": updated_count,
            "skipped_count": skipped_count if new_status == 'PUBLISHED' else 0
        })
        

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
                subject=f"Your Results for {result.class_name} - {result.term} Have Been Published",
                html_content=f"""
                <html>
                <body>
                    <p>Dear {student_name},</p>
                    <p>Your academic results for <strong>{result.class_name}</strong> - <strong>{result.term}</strong> have been published and are now available for viewing.</p>
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