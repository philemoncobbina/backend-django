# jobs/serializers.py
from rest_framework import serializers
from .models import JobPost, JobPostLog

class JobPostSerializer(serializers.ModelSerializer):
    class Meta:
        model = JobPost
        fields = '__all__'
        read_only_fields = ['reference_number', 'created_by_email']

class JobPostLogSerializer(serializers.ModelSerializer):
    class Meta:
        model = JobPostLog
        fields = '__all__'