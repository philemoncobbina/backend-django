from rest_framework import serializers
from .models import CustomUser, ParentGuardian


class ParentGuardianSerializer(serializers.ModelSerializer):
    class Meta:
        model = ParentGuardian
        fields = (
            'id',
            'first_name',
            'middle_name',
            'last_name',
            'suffix',
            'relationship',
            'primary_phone',
            'secondary_phone',
            'email',
            'street_address',
            'city',
            'state_region',
            'postal_code',
            'id_type',
            'id_number',
            'is_primary_contact',
            'created_at',
            'updated_at',
        )


class CustomUserSerializer(serializers.ModelSerializer):
    password = serializers.CharField(write_only=True, required=False)
    role = serializers.CharField(read_only=True)
    guardians = ParentGuardianSerializer(many=True, read_only=True)

    class Meta:
        model = CustomUser
        fields = (
            'id',
            'email',
            'first_name',
            'last_name',
            'password',
            'is_active',
            'is_blocked',
            'date_joined',
            'class_name',
            'role',
            'index_number',
            'username',
            'guardians',        # ← added
        )
        extra_kwargs = {
            'role': {'required': False}
        }

    def create(self, validated_data):
        password = validated_data.pop('password', None)
        instance = self.Meta.model(**validated_data)
        if password is not None:
            instance.set_password(password)
        instance.save()
        return instance


class GoogleSignInSerializer(serializers.Serializer):
    access_token = serializers.CharField(required=True)

    def validate_access_token(self, value):
        if not value:
            raise serializers.ValidationError("Access token is required.")
        return value


class PasswordResetSerializer(serializers.Serializer):
    email = serializers.EmailField()


class PasswordResetConfirmSerializer(serializers.Serializer):
    email = serializers.EmailField()
    verification_code = serializers.CharField()
    new_password = serializers.CharField(write_only=True)


class SendVerificationCodeSerializer(serializers.Serializer):
    email = serializers.EmailField()


class ChangePasswordRequestSerializer(serializers.Serializer):
    email = serializers.EmailField()


class ChangePasswordSerializer(serializers.Serializer):
    verification_code = serializers.CharField(max_length=6)
    old_password = serializers.CharField(write_only=True)
    new_password = serializers.CharField(write_only=True)