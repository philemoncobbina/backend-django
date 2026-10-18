import logging

from django.db import transaction
from rest_framework import generics, permissions, status
from rest_framework.exceptions import ValidationError
from rest_framework.response import Response

from .models import JobApplication, JobApplicationLog
from .serializers import JobApplicationLogSerializer, JobApplicationSerializer
from .tasks import send_application_received_email, send_application_rejection_email

logger = logging.getLogger(__name__)

DUPLICATE_MESSAGE = "You have already submitted an application for this position"


def queue_email(task, application_pk, label):
    """
    Queue a Celery email task after the DB transaction commits. Emails are best-effort:
    any failure here (Redis down, etc.) is logged and never reaches the user.
    """

    def _enqueue():
        try:
            task.delay(application_pk)
            logger.info("Queued %s email for application %s", label, application_pk)
        except Exception as e:
            logger.error(
                "Could not queue %s email for application %s: %s",
                label,
                application_pk,
                e,
                exc_info=True,
            )

    try:
        transaction.on_commit(_enqueue)
    except Exception as e:
        logger.error(
            "Could not schedule %s email for application %s: %s",
            label,
            application_pk,
            e,
            exc_info=True,
        )


class ApplyToJobView(generics.CreateAPIView):
    queryset = JobApplication.objects.all()
    serializer_class = JobApplicationSerializer

    def create(self, request, *args, **kwargs):
        serializer = self.get_serializer(data=request.data)

        try:
            serializer.is_valid(raise_exception=True)

            # Application + audit log are saved together; the email is queued only
            # once both are committed.
            with transaction.atomic():
                job_application = serializer.save()
                self._log_creation(request, job_application)
                queue_email(send_application_received_email, job_application.pk, "application received")

            return Response(
                {
                    "success": True,
                    "message": "Application submitted successfully",
                    "application": serializer.data,
                },
                status=status.HTTP_201_CREATED,
            )

        except ValidationError as e:
            if DUPLICATE_MESSAGE in str(e.detail):
                return Response(
                    {
                        "success": False,
                        "message": "Duplicate application",
                        "error": "You have already submitted an application for this position.",
                        "error_code": "DUPLICATE_APPLICATION",
                    },
                    status=status.HTTP_400_BAD_REQUEST,
                )

            return Response(
                {
                    "success": False,
                    "message": "Please correct the errors in your application",
                    "error": self._format_errors(e.detail),
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        except Exception as e:
            logger.error("Failed to create job application: %s", e, exc_info=True)
            return Response(
                {
                    "success": False,
                    "message": "An unexpected error occurred",
                    "error": "Please try again later.",
                },
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )

    def _log_creation(self, request, job_application):
        user = getattr(request, "user", None)
        if user is not None and user.is_authenticated:
            JobApplicationLog.objects.create(
                application=job_application,
                user=user,
                user_email=user.email or "anonymous@system.com",
                changed_fields="Initial application created",
            )
        else:
            JobApplicationLog.objects.create(
                application=job_application,
                user_email=job_application.email,
                changed_fields="Initial application created by applicant",
            )
        logger.info("JobApplicationLog created for new application %s", job_application.pk)

    def _format_errors(self, errors):
        """Convert DRF error format to a more user-friendly structure"""
        if isinstance(errors, str):
            return [errors]
        elif isinstance(errors, list):
            return [str(e) for e in errors]
        elif isinstance(errors, dict):
            return [
                f"{field}: {', '.join(str(e) for e in error) if isinstance(error, (list, tuple)) else error}"
                for field, error in errors.items()
            ]
        return ["An unknown error occurred"]


class JobApplicationDetailView(generics.RetrieveUpdateDestroyAPIView):
    queryset = JobApplication.objects.all()
    serializer_class = JobApplicationSerializer
    permission_classes = [permissions.IsAuthenticated]
    lookup_field = "id"

    # DRF's partial_update() calls update(partial=True), so one method covers both
    # PUT and PATCH.
    def update(self, request, *args, **kwargs):
        partial = kwargs.pop("partial", False)
        instance = self.get_object()
        logger.info("Updating job application %s (partial=%s)", instance.pk, partial)

        # Serialize with the same context as the updated data, otherwise URL fields
        # (e.g. resume_url) differ between "before" and "after" and show as changed.
        original_data = self.get_serializer(instance).data
        original_status = instance.status

        serializer = self.get_serializer(instance, data=request.data, partial=partial)
        if not serializer.is_valid():
            logger.info("Job application %s update invalid: %s", instance.pk, serializer.errors)
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

        with transaction.atomic():
            instance.last_modified_by = request.user
            updated_instance = serializer.save()

            updated_data = serializer.data
            changed_fields = self._get_changed_fields(original_data, updated_data)
            logger.info("Job application %s changed fields: %s", instance.pk, changed_fields)

            JobApplicationLog.objects.create(
                application=instance,
                user=request.user,
                user_email=request.user.email,
                changed_fields=changed_fields,
            )

            if original_status != "REJECTED" and updated_instance.status == "REJECTED":
                queue_email(send_application_rejection_email, updated_instance.pk, "rejection")

        return Response(serializer.data, status=status.HTTP_200_OK)

    def _get_changed_fields(self, original_data, updated_data):
        """
        Compare original and updated data and return a string of fields that changed.
        Format: "field1: old_value -> new_value, field2: old_value -> new_value"
        """
        changed_fields = []
        exclude_fields = ["updated_at", "last_modified_by"]

        # Resume: compare file names only, to avoid URL format differences
        if "resume" in original_data and "resume" in updated_data:
            original_resume = original_data["resume"]
            updated_resume = updated_data["resume"]

            if original_resume and updated_resume:
                if original_resume.split("/")[-1] != updated_resume.split("/")[-1]:
                    changed_fields.append(f"resume: {original_resume} -> {updated_resume}")

        for key, original_value in original_data.items():
            if key == "resume" or key in exclude_fields:
                continue

            updated_value = updated_data.get(key)
            if original_value != updated_value:
                changed_fields.append(f"{key}: {original_value} -> {updated_value}")

        return ", ".join(changed_fields)

    def destroy(self, request, *args, **kwargs):
        instance = self.get_object()

        JobApplicationLog.objects.create(
            application=instance,
            user=request.user,
            user_email=request.user.email,
            changed_fields="Application deleted",
        )
        logger.info(
            "JobApplicationLog created for deletion of application %s by %s",
            instance.pk,
            request.user.email,
        )

        self.perform_destroy(instance)
        return Response(status=status.HTTP_204_NO_CONTENT)


class JobApplicationListView(generics.ListAPIView):
    serializer_class = JobApplicationSerializer
    permission_classes = [permissions.IsAuthenticated]

    def get_queryset(self):
        return JobApplication.objects.all()


class JobApplicationLogListView(generics.ListAPIView):
    """
    View to retrieve logs for a specific job application
    """

    serializer_class = JobApplicationLogSerializer
    permission_classes = [permissions.IsAuthenticated]

    def get_queryset(self):
        application_id = self.kwargs["application_id"]
        return JobApplicationLog.objects.filter(application_id=application_id)