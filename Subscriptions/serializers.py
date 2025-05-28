from rest_framework import serializers
from .models import Subscription, EmailList

class SubscriptionSerializer(serializers.ModelSerializer):
    class Meta:
        model = Subscription
        fields = ['id', 'full_name', 'email', 'created_at', 'updated_at']

class EmailListSerializer(serializers.ModelSerializer):
    class Meta:
        model = EmailList
        fields = ['emails']
