# views.py
import os
import logging
from django.conf import settings
from rest_framework import viewsets, permissions, status, filters
from rest_framework.decorators import action
from rest_framework.response import Response
from django.utils import timezone
from django.db.models import Q
from sib_api_v3_sdk import Configuration, ApiClient, SendSmtpEmail
from sib_api_v3_sdk.api.transactional_emails_api import TransactionalEmailsApi
from sib_api_v3_sdk.rest import ApiException

from .models import BookList, BookListItem
from .serializers import (
    BookListSerializer, 
    BookListDetailSerializer,
    BookListItemSerializer,
    StudentBookListSerializer
)
from .permissions import IsStaffOrPrincipal, IsOwnerOrReadOnly

# Set up logging
logger = logging.getLogger(__name__)


class BookListViewSet(viewsets.ModelViewSet):
    """
    API endpoint for book list management
    """
    queryset = BookList.objects.all()
    filter_backends = [filters.SearchFilter, filters.OrderingFilter]
    search_fields = ['title', 'class_name', 'academic_year']
    ordering_fields = ['created_at', 'title', 'class_name', 'academic_year']
    
    def get_serializer_class(self):
        if self.action in ['retrieve', 'create', 'update', 'partial_update']:
            return BookListDetailSerializer
        return BookListSerializer
    
    def get_permissions(self):
        """
        - List and retrieve actions are accessible to all authenticated users
        - Create, update, delete actions require owner or read-only permissions
        - Schedule and publish actions require staff or principal permissions
        """
        if self.action in ['schedule', 'publish']:
            permission_classes = [permissions.IsAuthenticated, IsStaffOrPrincipal]
        else:
            permission_classes = [permissions.IsAuthenticated, IsOwnerOrReadOnly]
        return [permission() for permission in permission_classes]
    
    def send_publication_email(self, book_list):
        """
        Send email notification when a book list is published
        """
        logger.info(f"Starting email sending process for book list: {book_list.title} (ID: {book_list.id})")
        
        # Get all students in the class that this book list belongs to
        from django.contrib.auth import get_user_model
        User = get_user_model()
        
        try:
            students = User.objects.filter(
                class_name=book_list.class_name,
                role='student'  # Assuming you have a role field to identify students
            )
            
            logger.info(f"Found {students.count()} students in class {book_list.class_name}")
            
            if not students.exists():
                logger.warning(f"No students found for class {book_list.class_name}")
                return
            
            # Check if BREVO_API_KEY is set
            brevo_api_key = os.getenv('BREVO_API_KEY')
            if not brevo_api_key:
                logger.error("BREVO_API_KEY environment variable is not set")
                return
            
            configuration = Configuration()
            configuration.api_key['api-key'] = brevo_api_key
            api_instance = TransactionalEmailsApi(ApiClient(configuration))
            
            email_count = 0
            for student in students:
                if student.email:  # Only send if student has an email
                    try:
                        send_smtp_email = SendSmtpEmail(
                            to=[{"email": student.email}],
                            sender={"name": "School Administration", "email": settings.DEFAULT_FROM_EMAIL},
                            subject=f"Book List Published - {book_list.title}",
                            html_content=f"""
                            <html>
                            <body>
                                <p>Dear {student.get_full_name() if hasattr(student, 'get_full_name') else student.username},</p>
                                <p>A new book list has been published for your class:</p>
                                <ul>
                                    <li><strong>Title:</strong> {book_list.title}</li>
                                    <li><strong>Class:</strong> {book_list.class_name}</li>
                                    <li><strong>Academic Year:</strong> {book_list.academic_year}</li>
                                    <li><strong>Published Date:</strong> {book_list.publish_date.strftime('%B %d, %Y at %I:%M %p') if book_list.publish_date else 'N/A'}</li>
                                </ul>
                                <p>Please log in to your student portal to view the complete book list.</p>
                                <p>Best regards,<br>School Administration</p>
                            </body>
                            </html>
                            """
                        )
                        
                        api_response = api_instance.send_transac_email(send_smtp_email)
                        email_count += 1
                        logger.info(f"Email sent successfully to {student.email}. Response: {api_response}")
                        print(f"Email sent successfully to {student.email}: {api_response}")
                        
                    except ApiException as e:
                        logger.error(f"Exception when sending email to {student.email}: {e}")
                        print(f"Exception when sending email to {student.email}: {e}")
                    except Exception as e:
                        logger.error(f"Unexpected error when sending email to {student.email}: {e}")
                        print(f"Unexpected error when sending email to {student.email}: {e}")
                else:
                    logger.warning(f"Student {student.username} has no email address")
            
            logger.info(f"Email sending process completed. Sent {email_count} emails for book list: {book_list.title}")
            print(f"Email sending process completed. Sent {email_count} emails for book list: {book_list.title}")
            
        except Exception as e:
            logger.error(f"Error in send_publication_email: {e}")
            print(f"Error in send_publication_email: {e}")
    
    def check_and_send_scheduled_emails(self):
        """
        Check for scheduled book lists that should be published and send emails
        """
        scheduled_lists = BookList.objects.filter(
            status='scheduled', 
            scheduled_date__lte=timezone.now()
        )
        
        for book_list in scheduled_lists:
            # Store the old status before checking and updating
            old_status = book_list.status
            book_list.check_and_update_status()
            
            # If status changed from scheduled to published, send email
            if old_status == 'scheduled' and book_list.status == 'published':
                logger.info(f"Book list {book_list.title} automatically published from scheduled status")
                self.send_publication_email(book_list)
    
    def get_queryset(self):
        user = self.request.user
        
        # Check for scheduled book lists that should be published
        self.check_and_send_scheduled_emails()
        
        # For staff and principal, show all book lists
        if user.role in ['staff', 'principal']:
            return BookList.objects.all()
        
        # For students, show only published book lists
        # (scheduled ones will be converted to published by check_and_update_status)
        elif user.is_student:
            return BookList.objects.filter(
                status='published',
                class_name=user.class_name
            )
        
        # Default: show nothing
        return BookList.objects.none()
    
    def perform_create(self, serializer):
        """Set the current user as creator when creating a new book list"""
        # The serializer now handles setting publish_date when status is 'published'
        serializer.save(created_by=self.request.user)
    
    def perform_update(self, serializer):
        """Check if status changed to published during update and send email"""
        # Get the current instance before update
        old_instance = self.get_object()
        old_status = old_instance.status
        
        # Save the updated instance (serializer handles setting publish_date)
        updated_instance = serializer.save()
        
        # If status changed to published, send email
        if old_status != 'published' and updated_instance.status == 'published':
            logger.info(f"Book list {updated_instance.title} status changed to published via update")
            print(f"Book list {updated_instance.title} status changed to published via update")
            self.send_publication_email(updated_instance)
    
    @action(detail=False, methods=['get'])
    def my_class(self, request):
        """Get book lists ONLY for the current student's class"""
        user = request.user
        
        if not user.is_student:
            return Response({"detail": "Only students can access this endpoint."}, 
                          status=status.HTTP_403_FORBIDDEN)
        
        # Check for scheduled book lists that should be published
        self.check_and_send_scheduled_emails()
        
        # Get ONLY book lists for current class
        queryset = BookList.objects.filter(
            status='published',
            class_name=user.class_name
        )
        
        # Optional: Allow filtering by academic year
        academic_year = request.query_params.get('academic_year')
        if academic_year:
            queryset = queryset.filter(academic_year=academic_year)
        
        serializer = StudentBookListSerializer(queryset, many=True)
        return Response(serializer.data)

    @action(detail=False, methods=['get'])
    def previous_classes(self, request):
        """Get student's book lists from previous classes only (excluding current class)"""
        user = request.user
        
        if not user.is_student:
            return Response({"detail": "Only students can access this endpoint."}, 
                          status=status.HTTP_403_FORBIDDEN)
        
        # Get current class name
        current_class = user.class_name
        
        # Get previous classes of the student from history, excluding current class
        previous_classes = user.class_history.exclude(
            class_name=current_class
        ).values_list('class_name', flat=True).distinct()
        
        if not previous_classes:
            return Response({"detail": "No previous class history found (excluding current class)."}, 
                          status=status.HTTP_200_OK)
        
        # Check for scheduled book lists that should be published
        self.check_and_send_scheduled_emails()
        
        # Get book lists from previous classes only
        queryset = BookList.objects.filter(
            status='published',
            class_name__in=previous_classes
        )
        
        # Allow filtering by academic year
        academic_year = request.query_params.get('academic_year')
        if academic_year:
            queryset = queryset.filter(academic_year=academic_year)
        
        # Allow filtering by class
        class_name = request.query_params.get('class_name')
        if class_name:
            queryset = queryset.filter(class_name=class_name)
        
        serializer = StudentBookListSerializer(queryset, many=True)
        return Response(serializer.data)

    @action(detail=False, methods=['get'])
    def history(self, request):
        """Get student's book lists from previous academic years"""
        user = request.user
        
        if not user.is_student:
            return Response({"detail": "Only students can access this endpoint."}, 
                          status=status.HTTP_403_FORBIDDEN)
        
        # Check for scheduled book lists that should be published
        self.check_and_send_scheduled_emails()
        
        # Get current academic year from user's current book list
        current_year = user.class_history.first().academic_year if user.class_history.exists() else None
        
        if not current_year:
            return Response({"detail": "No current academic year found for this student."}, 
                          status=status.HTTP_404_NOT_FOUND)
        
        # Get book lists from previous academic years for the student's current class
        queryset = BookList.objects.filter(
            status='published',
            class_name=user.class_name
        ).exclude(academic_year=current_year).order_by('-created_at')
        
        serializer = StudentBookListSerializer(queryset, many=True)
        return Response(serializer.data)
    
    @action(detail=False, methods=['get'])
    def draft(self, request):
        """Get draft book lists (staff and principal only)"""
        user = request.user
        
        if user.role not in ['staff', 'principal']:
            return Response({"detail": "Only staff and principal can access drafts."}, 
                          status=status.HTTP_403_FORBIDDEN)
        
        queryset = BookList.objects.filter(status='draft')
        
        serializer = self.get_serializer(queryset, many=True)
        return Response(serializer.data)
    
    @action(detail=True, methods=['post'])
    def publish(self, request, pk=None):
        """Publish a book list"""
        booklist = self.get_object()
        
        if booklist.status == 'published':
            return Response({"detail": "Book list is already published."}, 
                          status=status.HTTP_400_BAD_REQUEST)
        
        # Store old status
        old_status = booklist.status
        
        booklist.status = 'published'
        if not booklist.publish_date:  # Only set if not already set
            booklist.publish_date = timezone.now()
        booklist.scheduled_date = None  # Clear scheduled date when manually published
        booklist.save()
        
        # Send email notification when manually published
        logger.info(f"Book list {booklist.title} manually published from {old_status} status")
        print(f"Book list {booklist.title} manually published from {old_status} status")
        self.send_publication_email(booklist)
        
        serializer = self.get_serializer(booklist)
        return Response(serializer.data)
    
    @action(detail=True, methods=['post'])
    def schedule(self, request, pk=None):
        """Schedule a book list for future publication"""
        booklist = self.get_object()
        
        # Ensure scheduled_date is provided
        scheduled_date = request.data.get('scheduled_date')
        if not scheduled_date:
            return Response({"detail": "Scheduled date is required."}, 
                          status=status.HTTP_400_BAD_REQUEST)
        
        # Store the old status before updating
        old_status = booklist.status
        
        booklist.status = 'scheduled'
        booklist.scheduled_date = scheduled_date
        booklist.save()
        
        # Check if scheduled date is in the past, which would trigger immediate publication
        booklist.check_and_update_status()
        
        # If status changed from scheduled to published due to past date, send email
        if old_status != 'published' and booklist.status == 'published':
            logger.info(f"Book list {booklist.title} immediately published due to past scheduled date")
            self.send_publication_email(booklist)
        
        serializer = self.get_serializer(booklist)
        return Response(serializer.data)
    
    @action(detail=False, methods=['get'])
    def current_class(self, request):
        """
        Get only the book lists for the student's current class in the current academic year.
        This is a simplified version of my_class that doesn't include previous classes.
        """
        user = request.user
        
        if not user.is_student:
            return Response({"detail": "Only students can access this endpoint."}, 
                          status=status.HTTP_403_FORBIDDEN)
        
        # Check for scheduled book lists that should be published
        self.check_and_send_scheduled_emails()
        
        # Get current academic year from user's most recent class history
        current_year = user.class_history.first().academic_year if user.class_history.exists() else None
        
        # Get published book lists for current class and current academic year
        queryset = BookList.objects.filter(
            status='published',
            class_name=user.class_name
        )
        
        if current_year:
            queryset = queryset.filter(academic_year=current_year)
        
        serializer = StudentBookListSerializer(queryset, many=True)
        return Response(serializer.data)


class BookListItemViewSet(viewsets.ModelViewSet):
    """
    API endpoint for book list items
    """
    queryset = BookListItem.objects.all()
    serializer_class = BookListItemSerializer
    permission_classes = [permissions.IsAuthenticated, IsStaffOrPrincipal]
    
    def get_queryset(self):
        booklist_id = self.kwargs.get('booklist_pk')
        if booklist_id:
            return BookListItem.objects.filter(book_list_id=booklist_id)
        return BookListItem.objects.all()
    
    def perform_create(self, serializer):
        booklist_id = self.kwargs.get('booklist_pk')
        book_list = BookList.objects.get(pk=booklist_id)
        serializer.save(book_list=book_list)