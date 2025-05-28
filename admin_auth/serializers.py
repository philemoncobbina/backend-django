# admin_auth/serializers.py
from rest_framework import serializers
from authapp.models import CustomUser

  # Ensure this import points to the unified model

class AdminUserSerializer(serializers.ModelSerializer):
    class Meta:
        model = CustomUser  # Refers to the CustomUser from authapp
        fields = '__all__'
        extra_kwargs = {
            'password': {'write_only': True},
        }

    def create(self, validated_data):
        return CustomUser.objects.create_user(**validated_data)

    def update(self, instance, validated_data):
        instance.role = validated_data.get('role', instance.role)
        return super().update(instance, validated_data)




