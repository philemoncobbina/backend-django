from rest_framework import serializers
from .models import JobApplication, JobApplicationLog

class JobApplicationSerializer(serializers.ModelSerializer):
    resume_url = serializers.SerializerMethodField()

    class Meta:
        model = JobApplication
        fields = '__all__'
        read_only_fields = ['applied_at']  # ← removed 'resume' from here

    def get_resume_url(self, obj):
        """Return a full absolute URL for the resume file."""
        if not obj.resume:
            return None
        request = self.context.get('request')
        if request:
            return request.build_absolute_uri(obj.resume.url)
        return obj.resume.url  # fallback to relative URL

    def validate(self, data):
        # On update (instance exists), skip duplicate check
        if self.instance:
            return data

        # On create, check for duplicate application
        if JobApplication.objects.filter(
            email=data.get('email'),
            job_post=data.get('job_post')
        ).exists():
            raise serializers.ValidationError(
                "You have already submitted an application for this position."
            )
        return data


class JobApplicationLogSerializer(serializers.ModelSerializer):
    user_email = serializers.EmailField(read_only=True)
    timestamp = serializers.DateTimeField(read_only=True)

    class Meta:
        model = JobApplicationLog
        fields = ['id', 'application', 'user_email', 'changed_fields', 'timestamp']
        read_only_fields = ['id', 'application', 'user_email', 'changed_fields', 'timestamp']