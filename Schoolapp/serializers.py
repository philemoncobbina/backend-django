# serializers.py
from rest_framework import serializers
from .models import Contact, ContactLog

class ContactSerializer(serializers.ModelSerializer):
    class Meta:
        model = Contact
        fields = '__all__'

    def validate(self, data):
        if 'status' in data and data['status'] in ['in_progress', 'resolved']:
            if not data.get('action_taken'):
                raise serializers.ValidationError("Action taken is required when status is 'In Progress' or 'Resolved'.")
        return data

class ContactLogSerializer(serializers.ModelSerializer):
    class Meta:
        model = ContactLog
        fields = '__all__'