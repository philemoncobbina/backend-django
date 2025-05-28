import logging
from rest_framework import viewsets, status, permissions
from rest_framework.decorators import action
from rest_framework.response import Response
from rest_framework.exceptions import ValidationError
from django.utils import timezone
from django.db.models import Q
from rest_framework import generics
from django.shortcuts import get_object_or_404
from django.forms.models import model_to_dict
from .models import JobPost, JobPostLog
from .serializers import JobPostSerializer, JobPostLogSerializer
from datetime import datetime
import pytz

# Set up logging
logger = logging.getLogger(__name__)

class JobPostViewSet(viewsets.ModelViewSet):
    """
    ViewSet for managing job posts with enhanced logging and automated publishing.
    """
    queryset = JobPost.objects.all()
    serializer_class = JobPostSerializer

    def _log_job_post_changes(self, job_post, user, action_type, original_data=None):
        """
        Create a log entry for job post changes
        """
        try:
            # If it's an update, find changed fields
            changed_fields = []
            if original_data:
                current_data = model_to_dict(job_post)
                for key, original_value in original_data.items():
                    current_value = current_data.get(key)
                    if str(original_value) != str(current_value):
                        changed_fields.append(f"{key}: {original_value} -> {current_value}")

            JobPostLog.objects.create(
                job_post=job_post,
                user=user,
                user_email=user.email if user else "Anonymous",
                changed_fields=', '.join(changed_fields) if changed_fields else "Initial creation",
                action_type=action_type
            )
        except Exception as e:
            logger.error(f"Error creating job post log: {str(e)}")

    def get_permissions(self):
        """
        Custom permissions:
        - Public listings, get_published_post, and list_published_posts are accessible to anyone
        - Other actions require authentication
        """
        if self.action in ['public_listings', 'get_published_post', 'list_published_posts', 'get_applications_count']:
            permission_classes = [permissions.AllowAny]
        else:
            permission_classes = [permissions.IsAuthenticated]
        return [permission() for permission in permission_classes]

    def get_queryset(self):
        """
        Filter queryset based on action and user permissions.
        Also checks for scheduled posts that need to be published.
        """
        try:
            # Check for scheduled posts that need to be published
            published_count = self._check_scheduled_posts()
            if published_count > 0:
                logger.info(f"Successfully published {published_count} scheduled job posts")

            queryset = JobPost.objects.all()

            if self.action in ['public_listings', 'list_published_posts']:
                logger.debug("Retrieving public listings")
                return queryset.filter(
                    status='PUBLISHED',
                    published_date__lte=timezone.now()
                ).order_by('-published_date')

            # Only filter by created_by if the user is authenticated and not a staff member
            if self.request.user.is_authenticated and not self.request.user.is_staff:
                logger.debug(f"Filtering posts for user: {self.request.user.id}")
                queryset = queryset.filter(created_by=self.request.user)

            return queryset.order_by('-updated_at')

        except Exception as e:
            logger.error(f"Error in get_queryset: {str(e)}")
            raise

    def _check_scheduled_posts(self):
        """
        Check and publish any scheduled posts that have reached their publication time.
        Returns the number of posts published.
        """
        try:
            now = timezone.now()
            scheduled_jobs = JobPost.objects.filter(
                status='SCHEDULED',
                scheduled_date__lte=now
            )
            
            published_count = 0
            for job in scheduled_jobs:
                logger.info(f"Publishing scheduled job post: {job.id} - {job.title}")
                job.status = 'PUBLISHED'
                job.published_date = now
                job.save()
                published_count += 1
                
            return published_count

        except Exception as e:
            logger.error(f"Error while checking scheduled posts: {str(e)}")
            raise

    def _validate_scheduled_date(self, scheduled_date):
        """
        Validate that the scheduled date is in the future.
        """
        try:
            if not scheduled_date:
                raise ValidationError({'scheduled_date': 'Scheduled date is required'})

            if isinstance(scheduled_date, str):
                try:
                    scheduled_date = datetime.fromisoformat(scheduled_date.replace('Z', '+00:00'))
                    scheduled_date = pytz.utc.localize(scheduled_date.replace(tzinfo=None))
                except ValueError as e:
                    logger.error(f"Invalid date format: {str(e)}")
                    raise ValidationError({'scheduled_date': 'Invalid date format. Use ISO format (YYYY-MM-DDTHH:MM:SSZ)'})

            if scheduled_date <= timezone.now():
                logger.warning(f"Attempted to schedule post for past date: {scheduled_date}")
                raise ValidationError({'scheduled_date': 'Scheduled date must be in the future'})

            return scheduled_date

        except ValidationError:
            raise
        except Exception as e:
            logger.error(f"Error validating scheduled date: {str(e)}")
            raise

    def create(self, request, *args, **kwargs):
        """
        Create a new job post with initial draft status and reference number.
        """
        try:
            data = request.data.copy()
            data['status'] = 'DRAFT'  # Always create as draft
            data['created_by'] = request.user.id
            
            logger.info(f"Creating new job post by user: {request.user.id}")
            
            serializer = self.get_serializer(data=data)
            serializer.is_valid(raise_exception=True)
            
            # Save the instance to generate reference number
            instance = serializer.save()
            
            # Update the created_by_email
            instance.created_by_email = request.user.email
            instance.save()
            
            # Log the creation
            self._log_job_post_changes(
                job_post=instance, 
                user=request.user, 
                action_type='CREATE'
            )
            
            logger.info(f"Successfully created job post: {instance.reference_number}")
            
            # Refresh serializer data to include reference number
            serializer = self.get_serializer(instance)
            
            headers = self.get_success_headers(serializer.data)
            return Response(
                serializer.data,
                status=status.HTTP_201_CREATED,
                headers=headers
            )

        except Exception as e:
            logger.error(f"Error creating job post: {str(e)}")
            raise

    def update(self, request, *args, **kwargs):
        """
        Update a job post with comprehensive logging.
        """
        try:
            # Get original data before update
            instance = self.get_object()
            original_data = model_to_dict(instance)

            # Perform update
            serializer = self.get_serializer(instance, data=request.data, partial=False)
            serializer.is_valid(raise_exception=True)
            updated_instance = serializer.save()

            # Log the changes
            self._log_job_post_changes(
                job_post=updated_instance, 
                user=request.user, 
                action_type='UPDATE',
                original_data=original_data
            )

            logger.info(f"Updated job post: {updated_instance.reference_number}")

            return Response(serializer.data)

        except Exception as e:
            logger.error(f"Error updating job post: {str(e)}")
            raise

    def partial_update(self, request, *args, **kwargs):
        """
        Partially update a job post with comprehensive logging.
        """
        try:
            # Get original data before update
            instance = self.get_object()
            original_data = model_to_dict(instance)

            # Perform partial update
            serializer = self.get_serializer(instance, data=request.data, partial=True)
            serializer.is_valid(raise_exception=True)
            updated_instance = serializer.save()

            # Log the changes
            self._log_job_post_changes(
                job_post=updated_instance, 
                user=request.user, 
                action_type='UPDATE',
                original_data=original_data
            )

            logger.info(f"Partially updated job post: {updated_instance.reference_number}")

            return Response(serializer.data)

        except Exception as e:
            logger.error(f"Error partially updating job post: {str(e)}")
            raise

    @action(detail=True, methods=['post'])
    def publish(self, request, pk=None):
        """
        Immediately publish a job post with logging.
        """
        try:
            job = self.get_object()
            original_status = job.status

            if job.status not in ['DRAFT', 'SCHEDULED']:
                logger.warning(f"Invalid publish attempt for job {job.id} with status {job.status}")
                raise ValidationError({'status': 'Only draft or scheduled posts can be published'})

            job.status = 'PUBLISHED'
            job.published_date = timezone.now()
            job.scheduled_date = None
            job.save()
            
            # Log status change
            self._log_job_post_changes(
                job_post=job, 
                user=request.user, 
                action_type='PUBLISH',
                original_data={'status': original_status}
            )
            
            logger.info(f"Published job post: {job.reference_number}")
            
            serializer = self.serializer_class(job)
            return Response(serializer.data)

        except Exception as e:
            logger.error(f"Error publishing job post: {str(e)}")
            raise

    @action(detail=True, methods=['post'])
    def schedule(self, request, pk=None):
        """
        Schedule a job post for future publication with logging.
        """
        try:
            job = self.get_object()
            original_status = job.status
            scheduled_date = request.data.get('scheduled_date')
            
            logger.info(f"Scheduling job post {job.id} for publication at {scheduled_date}")
            
            if job.status not in ['DRAFT', 'SCHEDULED']:
                logger.warning(f"Invalid schedule attempt for job {job.id} with status {job.status}")
                raise ValidationError({'status': 'Only draft posts can be scheduled'})

            validated_date = self._validate_scheduled_date(scheduled_date)
            
            job.status = 'SCHEDULED'
            job.scheduled_date = validated_date
            job.published_date = None
            job.save()
            
            # Log status change
            self._log_job_post_changes(
                job_post=job, 
                user=request.user, 
                action_type='SCHEDULE',
                original_data={'status': original_status}
            )
            
            logger.info(f"Successfully scheduled job post {job.reference_number} for {validated_date}")
            
            serializer = self.serializer_class(job)
            return Response(serializer.data)

        except Exception as e:
            logger.error(f"Error scheduling job post: {str(e)}")
            raise

    @action(detail=False, methods=['get'])
    def get_draft_posts(self, request):
        """
        Get all draft posts for the current user.
        """
        try:
            queryset = self.get_queryset().filter(status='DRAFT')
            logger.debug(f"Retrieving draft posts for user: {request.user.id}")
            
            serializer = self.serializer_class(queryset, many=True)
            return Response(serializer.data)

        except Exception as e:
            logger.error(f"Error retrieving draft posts: {str(e)}")
            raise

    @action(detail=True, methods=['get'])
    def get_published_post(self, request, pk=None):
        """
        Get a single published post by ID.
        URL: /api/jobposts/{id}/get_published_post/
        """
        try:
            job_post = get_object_or_404(JobPost, id=pk, status='PUBLISHED')
            logger.debug(f"Retrieving PUBLISHED post with ID: {pk}")
            serializer = self.serializer_class(job_post)
            return Response(serializer.data)
        except Exception as e:
            logger.error(f"Error retrieving PUBLISHED post: {str(e)}")
            return Response(
                {"error": "An error occurred while fetching the job post."}, 
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )

    @action(detail=False, methods=['get'])
    def list_published_posts(self, request):
        """
        Get all published posts.
        URL: /api/jobposts/list_published_posts/
        """
        try:
            queryset = JobPost.objects.filter(status='PUBLISHED')
            logger.debug("Retrieving all PUBLISHED posts")
            serializer = self.serializer_class(queryset, many=True)
            return Response(serializer.data)
        except Exception as e:
            logger.error(f"Error retrieving PUBLISHED posts: {str(e)}")
            return Response(
                {"error": "An error occurred while fetching job posts."}, 
                status=status.HTTP_500_INTERNAL_SERVER_ERROR   
            )

    @action(detail=True, methods=['get'])
    def get_applications_count(self, request, pk=None):
        """
        Get the number of applications received for a specific job post.
        URL: /api/jobposts/{id}/get_applications_count/
        """
        try:
            job_post = get_object_or_404(JobPost, id=pk)
            logger.debug(f"Retrieving applications count for job post: {job_post.id}")
            
            # Return the applications_count field from the JobPost model
            return Response({
                "job_post_id": job_post.id,
                "job_post_title": job_post.title,
                "applications_count": job_post.applications_count
            })
        
        except Exception as e:
            logger.error(f"Error retrieving applications count: {str(e)}")
            return Response(
                {"error": "An error occurred while fetching the applications count."}, 
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )


class JobPostLogListView(generics.ListAPIView):
    """
    API view to retrieve logs for a specific job post.
    URL: /api/jobposts/{job_post_id}/logs/
    """
    serializer_class = JobPostLogSerializer
    
    def get_queryset(self):
        job_post_id = self.kwargs.get('job_post_id')
        logger.debug(f"Retrieving logs for job post: {job_post_id}")
        return JobPostLog.objects.filter(job_post_id=job_post_id).order_by('-timestamp')

    def get_permissions(self):
        """
        Only authenticated users can view logs
        """
        permission_classes = [permissions.IsAuthenticated]
        return [permission() for permission in permission_classes]