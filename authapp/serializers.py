#authapp serializer

from django.core.exceptions import ValidationError as DjangoValidationError
from rest_framework import serializers

from .models import CustomUser, ParentGuardian


class ParentGuardianSerializer(serializers.ModelSerializer):
    class Meta:
        model = ParentGuardian
        fields = (
            "id",
            "first_name",
            "middle_name",
            "last_name",
            "suffix",
            "relationship",
            "primary_phone",
            "secondary_phone",
            "email",
            "street_address",
            "city",
            "state_region",
            "postal_code",
            "id_type",
            "id_number",
            "is_primary_contact",
            "created_at",
            "updated_at",
        )


class CustomUserSerializer(serializers.ModelSerializer):
    password = serializers.CharField(write_only=True, required=False)
    role = serializers.CharField(read_only=True)
    guardians = ParentGuardianSerializer(many=True, read_only=True)

    # Fields tied to the Graduated / Stopped statuses of `class_name`
    STATUS_FIELDS = ("graduation_year", "stopped_class", "stop_reason", "stop_year")

    class Meta:
        model = CustomUser
        fields = (
            "id",
            "email",
            "first_name",
            "last_name",
            "password",
            "is_active",
            "is_blocked",
            "date_joined",
            "class_name",
            "graduation_year",
            "stopped_class",
            "stop_reason",
            "stop_year",
            "role",
            "index_number",
            "username",
            "guardians",
        )
        extra_kwargs = {
            "role": {"required": False},
        }

    def validate(self, attrs):
        """
        Enforce the Graduated / Stopped rules:
          - Graduated -> graduation_year required
          - Stopped   -> stopped_class, stop_reason and stop_year required
          - Any regular class -> status fields are cleared automatically
        Works for create, full update and partial update (PATCH).
        """
        attrs = super().validate(attrs)

        # On updates, only run when class_name or a status field is being changed.
        touches_status = "class_name" in attrs or any(f in attrs for f in self.STATUS_FIELDS)
        if self.instance is not None and not touches_status:
            return attrs

        def current(name):
            """Incoming value if provided, otherwise the value already on the instance."""
            if name in attrs:
                return attrs[name]
            return getattr(self.instance, name, None) if self.instance else None

        try:
            cleaned = CustomUser.resolve_status_fields(
                current("class_name"),
                graduation_year=current("graduation_year"),
                stopped_class=current("stopped_class"),
                stop_reason=current("stop_reason"),
                stop_year=current("stop_year"),
            )
        except DjangoValidationError as exc:
            raise serializers.ValidationError(exc.message_dict)

        attrs.update(cleaned)
        return attrs

    def create(self, validated_data):
        password = validated_data.pop("password", None)
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


class ResendVerificationEmailSerializer(serializers.Serializer):
    email = serializers.EmailField()


class ChangePasswordRequestSerializer(serializers.Serializer):
    email = serializers.EmailField()


class ChangePasswordSerializer(serializers.Serializer):
    verification_code = serializers.CharField(max_length=6)
    old_password = serializers.CharField(write_only=True)
    new_password = serializers.CharField(write_only=True)