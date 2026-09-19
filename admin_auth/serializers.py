#admin_auth serializer
import json

from django.db import transaction
from rest_framework import serializers

from authapp.models import CustomUser, ParentGuardian
from .models import UserChangeLog


SENSITIVE_LOG_FIELDS = {
    "password",
    "last_login",
}


def _normalise_log_value(value):
    """
    Convert model and serializer values into safe, readable text.

    Passwords and other sensitive values must be filtered before this
    function is called.
    """
    if value is None:
        return None

    if isinstance(value, bool):
        return "true" if value else "false"

    if isinstance(value, (dict, list, tuple)):
        return json.dumps(value, default=str, sort_keys=True)

    return str(value)


def _get_client_ip(request):
    if request is None:
        return None

    forwarded_for = request.META.get("HTTP_X_FORWARDED_FOR")
    if forwarded_for:
        return forwarded_for.split(",")[0].strip()

    return request.META.get("REMOTE_ADDR")


def create_user_change_log(
    *,
    target_user,
    changed_by,
    action,
    field_name="",
    previous_value=None,
    new_value=None,
    request=None,
    target_user_email=None,
):
    """
    Central helper for inserting a user audit record.

    target_user may be None when preserving a deletion record.
    target_user_email can be supplied explicitly for deletion logging.
    """
    if target_user_email is None:
        target_user_email = getattr(target_user, "email", "") or ""

    changed_by_email = (
        getattr(changed_by, "email", "")
        if changed_by is not None
        else ""
    )

    UserChangeLog.objects.create(
        target_user=target_user,
        target_user_email=target_user_email,
        changed_by=changed_by,
        changed_by_email=changed_by_email,
        action=action,
        field_name=field_name,
        previous_value=_normalise_log_value(previous_value),
        new_value=_normalise_log_value(new_value),
        request_method=request.method if request else "",
        request_path=request.path if request else "",
        ip_address=_get_client_ip(request),
    )


class ParentGuardianSerializer(serializers.ModelSerializer):
    # The ID must be writable because guardian updates use it as the lookup key.
    id = serializers.IntegerField(required=False, allow_null=True)
    full_name = serializers.ReadOnlyField()

    class Meta:
        model = ParentGuardian
        fields = [
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
            "full_name",
            "created_at",
            "updated_at",
        ]
        read_only_fields = [
            "created_at",
            "updated_at",
            "full_name",
        ]


class AdminUserSerializer(serializers.ModelSerializer):
    guardians = ParentGuardianSerializer(
        many=True,
        required=False,
    )

    password = serializers.CharField(
        write_only=True,
        required=False,
        allow_blank=False,
        trim_whitespace=False,
    )

    class Meta:
        model = CustomUser
        fields = [
            "id",
            "username",
            "email",
            "first_name",
            "last_name",
            "password",
            "is_active",
            "is_staff",
            "is_blocked",
            "date_joined",
            "role",
            "is_google_account",
            "index_number",
            "class_name",
            "guardians",
        ]
        read_only_fields = [
            "id",
            "date_joined",
            "is_google_account",
        ]
        extra_kwargs = {
            "email": {"required": True},
            "username": {"required": True},
        }

    def _request(self):
        return self.context.get("request")

    def _actor(self):
        request = self._request()

        if request and request.user and request.user.is_authenticated:
            return request.user

        return None

    def validate_guardians(self, value):
        """
        If a guardian carries an ID during an update, verify that the guardian
        belongs to the user being updated.

        Unknown IDs are rejected rather than silently treated as new records.
        """
        if self.instance is None:
            for guardian_data in value:
                if guardian_data.get("id"):
                    raise serializers.ValidationError(
                        "Guardian IDs must not be supplied when creating a user."
                    )
            return value

        existing_ids = set(
            self.instance.guardians.values_list("id", flat=True)
        )

        incoming_ids = set()

        for guardian_data in value:
            guardian_id = guardian_data.get("id")

            if guardian_id is None:
                continue

            if guardian_id in incoming_ids:
                raise serializers.ValidationError(
                    f"Guardian id={guardian_id} appears more than once."
                )

            incoming_ids.add(guardian_id)

            if guardian_id not in existing_ids:
                raise serializers.ValidationError(
                    f"Guardian with id={guardian_id} does not belong to this user."
                )

        return value

    def validate(self, attrs):
        """
        Ensure no guardian email matches the student's email.
        """
        guardians_data = attrs.get("guardians")

        student_email = attrs.get("email")
        if not student_email and self.instance:
            student_email = self.instance.email

        if not student_email:
            return attrs

        normalised_student_email = student_email.strip().lower()

        if guardians_data is not None:
            for guardian_data in guardians_data:
                guardian_email = (
                    guardian_data.get("email") or ""
                ).strip().lower()

                if (
                    guardian_email
                    and guardian_email == normalised_student_email
                ):
                    raise serializers.ValidationError(
                        {
                            "guardians": (
                                "A guardian's email cannot be the same as the "
                                f"student's email ({student_email}). Please use "
                                "a different email address for the guardian."
                            )
                        }
                    )

        elif self.instance and "email" in attrs:
            conflict_exists = self.instance.guardians.filter(
                email__iexact=student_email.strip()
            ).exists()

            if conflict_exists:
                raise serializers.ValidationError(
                    {
                        "email": (
                            f"The new student email ({student_email}) conflicts "
                            "with an existing guardian email on this account. "
                            "Update or remove the guardian's email first."
                        )
                    }
                )

        return attrs

    @transaction.atomic
    def create(self, validated_data):
        guardians_data = validated_data.pop("guardians", [])
        password = validated_data.pop("password", None)

        if not password:
            raise serializers.ValidationError(
                {"password": "A password is required when creating a user."}
            )

        user = CustomUser.objects.create_user(
            password=password,
            **validated_data,
        )

        actor = self._actor()
        request = self._request()

        create_user_change_log(
            target_user=user,
            changed_by=actor,
            action=UserChangeLog.Action.CREATE,
            field_name="account",
            previous_value=None,
            new_value=f"User account created with role={user.role}",
            request=request,
        )

        for guardian_data in guardians_data:
            guardian_data.pop("id", None)

            guardian = ParentGuardian.objects.create(
                student=user,
                **guardian_data,
            )

            create_user_change_log(
                target_user=user,
                changed_by=actor,
                action=UserChangeLog.Action.GUARDIAN_CREATE,
                field_name=f"guardian[{guardian.id}]",
                previous_value=None,
                new_value={
                    "id": guardian.id,
                    "full_name": guardian.full_name,
                    "relationship": guardian.relationship,
                },
                request=request,
            )

        return user

    @transaction.atomic
    def update(self, instance, validated_data):
        guardians_data = validated_data.pop("guardians", None)
        password = validated_data.pop("password", None)

        actor = self._actor()
        request = self._request()

        original_values = {}

        for field_name in validated_data:
            if field_name in SENSITIVE_LOG_FIELDS:
                continue

            original_values[field_name] = getattr(
                instance,
                field_name,
                None,
            )

        old_email = instance.email

        # Update ordinary model fields manually. This prevents nested data
        # from reaching DRF's default ModelSerializer update implementation.
        for attr, value in validated_data.items():
            setattr(instance, attr, value)

        update_fields = list(validated_data.keys())

        if password is not None:
            instance.set_password(password)
            update_fields.append("password")

        if update_fields:
            instance.save(update_fields=list(dict.fromkeys(update_fields)))

        for field_name, previous_value in original_values.items():
            new_value = getattr(instance, field_name, None)

            if _normalise_log_value(previous_value) != _normalise_log_value(
                new_value
            ):
                create_user_change_log(
                    target_user=instance,
                    target_user_email=instance.email or old_email,
                    changed_by=actor,
                    action=UserChangeLog.Action.UPDATE,
                    field_name=field_name,
                    previous_value=previous_value,
                    new_value=new_value,
                    request=request,
                )

        if password is not None:
            create_user_change_log(
                target_user=instance,
                changed_by=actor,
                action=UserChangeLog.Action.UPDATE,
                field_name="password",
                previous_value="[PROTECTED]",
                new_value="[CHANGED]",
                request=request,
            )

        if guardians_data is not None:
            self._sync_guardians(
                user=instance,
                guardians_data=guardians_data,
                actor=actor,
                request=request,
            )

        return instance

    def _sync_guardians(self, user, guardians_data, actor, request):
        """
        Full replacement and upsert of a user's guardian list.

        A guardian with an ID is updated.
        A guardian without an ID is created.
        Existing guardians omitted from the submitted list are deleted.
        """
        existing_map = {
            guardian.id: guardian
            for guardian in user.guardians.all()
        }

        incoming_ids = set()

        for raw_guardian_data in guardians_data:
            guardian_data = dict(raw_guardian_data)
            guardian_id = guardian_data.pop("id", None)

            if guardian_id is not None:
                guardian = existing_map[guardian_id]
                incoming_ids.add(guardian_id)

                for attr, new_value in guardian_data.items():
                    previous_value = getattr(guardian, attr, None)

                    if _normalise_log_value(
                        previous_value
                    ) == _normalise_log_value(new_value):
                        continue

                    setattr(guardian, attr, new_value)

                    create_user_change_log(
                        target_user=user,
                        changed_by=actor,
                        action=UserChangeLog.Action.GUARDIAN_UPDATE,
                        field_name=f"guardian[{guardian.id}].{attr}",
                        previous_value=previous_value,
                        new_value=new_value,
                        request=request,
                    )

                guardian.save()

            else:
                guardian = ParentGuardian.objects.create(
                    student=user,
                    **guardian_data,
                )
                incoming_ids.add(guardian.id)

                create_user_change_log(
                    target_user=user,
                    changed_by=actor,
                    action=UserChangeLog.Action.GUARDIAN_CREATE,
                    field_name=f"guardian[{guardian.id}]",
                    previous_value=None,
                    new_value={
                        "id": guardian.id,
                        "full_name": guardian.full_name,
                        "relationship": guardian.relationship,
                    },
                    request=request,
                )

        ids_to_delete = set(existing_map.keys()) - incoming_ids

        for guardian_id in ids_to_delete:
            guardian = existing_map[guardian_id]

            guardian_snapshot = {
                "id": guardian.id,
                "full_name": guardian.full_name,
                "relationship": guardian.relationship,
                "primary_phone": guardian.primary_phone,
                "email": guardian.email,
            }

            create_user_change_log(
                target_user=user,
                changed_by=actor,
                action=UserChangeLog.Action.GUARDIAN_DELETE,
                field_name=f"guardian[{guardian.id}]",
                previous_value=guardian_snapshot,
                new_value=None,
                request=request,
            )

            guardian.delete()


class UserChangeLogSerializer(serializers.ModelSerializer):
    action_display = serializers.CharField(
        source="get_action_display",
        read_only=True,
    )

    class Meta:
        model = UserChangeLog
        fields = [
            "id",
            "target_user",
            "target_user_email",
            "changed_by",
            "changed_by_email",
            "action",
            "action_display",
            "field_name",
            "previous_value",
            "new_value",
            "changed_at",
            "request_method",
            "request_path",
            "ip_address",
        ]
        read_only_fields = fields