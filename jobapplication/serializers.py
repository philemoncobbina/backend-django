# serializers.py
from rest_framework import serializers
from .models import JobApplication , JobApplicationLog

class JobApplicationSerializer(serializers.ModelSerializer):
    class Meta:
        model = JobApplication
        fields = '__all__'
        read_only_fields = ['applied_at', 'resume']

    def validate(self, data):
        # Check if user already applied to this job
        if JobApplication.objects.filter(
            email=data.get('email'),
            job_post=data.get('job_post')
        ).exists():
            raise serializers.ValidationError(
                "You have already submitted an application for this position."
            )
        return data



class JobApplicationLogSerializer(serializers.ModelSerializer):
    """
    Serializer for JobApplicationLog model to track changes.
    """
    user_email = serializers.EmailField(read_only=True)
    timestamp = serializers.DateTimeField(read_only=True)
    
    class Meta:
        model = JobApplicationLog
        fields = ['id', 'application', 'user_email', 'changed_fields', 'timestamp']
        read_only_fields = ['id', 'application', 'user_email', 'changed_fields', 'timestamp']