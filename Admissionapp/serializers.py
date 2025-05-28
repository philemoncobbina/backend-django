from rest_framework import serializers
from .models import Admission, AdmissionLog

class AdmissionSerializer(serializers.ModelSerializer):
    admission_number = serializers.CharField(read_only=True)

    class Meta:
        model = Admission
        fields = '__all__'

class AdmissionLogSerializer(serializers.ModelSerializer):
    class Meta:
        model = AdmissionLog
        fields = '__all__'
