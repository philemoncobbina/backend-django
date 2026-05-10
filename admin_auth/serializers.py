# admin_auth/serializers.py
from rest_framework import serializers
from authapp.models import CustomUser, ParentGuardian


class ParentGuardianSerializer(serializers.ModelSerializer):
    # Explicitly declare `id` as a writable integer field so we can use it
    # as the lookup key during updates. Without this, DRF treats the auto-
    # generated PK as read-only and strips it before validation.
    id = serializers.IntegerField(required=False, allow_null=True)
    full_name = serializers.ReadOnlyField()

    class Meta:
        model = ParentGuardian
        fields = [
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
            'full_name',
        ]
        read_only_fields = ['created_at', 'updated_at']


class AdminUserSerializer(serializers.ModelSerializer):
    # Mark guardians as read_only=False but handle all writes manually.
    # Setting read_only=False + required=False lets the data pass validation
    # without DRF trying to do its own nested-write handling.
    guardians = ParentGuardianSerializer(many=True, required=False)

    class Meta:
        model = CustomUser
        fields = '__all__'
        extra_kwargs = {
            'password': {'write_only': True},
        }

    def validate_guardians(self, value):
        """
        Cross-field validation: if any guardian carries an `id`, make sure
        it actually belongs to the user being updated (prevents IDOR on guardians).
        """
        # During create there is no instance yet — skip the ownership check.
        if self.instance is None:
            return value

        existing_ids = set(
            self.instance.guardians.values_list('id', flat=True)
        )
        for guardian_data in value:
            guardian_id = guardian_data.get('id')
            if guardian_id and guardian_id not in existing_ids:
                raise serializers.ValidationError(
                    f"Guardian with id={guardian_id} does not belong to this user."
                )
        return value

    def validate(self, attrs):
        """
        Object-level validation: ensure no guardian's email matches the
        student's email.  Mirrors the same constraint enforced in
        ParentGuardian.clean() so that the API returns a clean 400 with a
        readable message rather than a 500 from an IntegrityError / ValidationError
        raised deep inside model.save().

        Checked in three scenarios:
          1. Creating a new student — student email comes from attrs.
          2. Updating a student's email only — guardians come from the existing instance.
          3. Updating guardians only (or both at once) — student email comes from
             attrs if present, else from the existing instance.
        """
        guardians_data = attrs.get('guardians')

        # ── Determine the student email we should validate against ────────
        student_email = attrs.get('email')
        if not student_email and self.instance:
            student_email = self.instance.email

        if not student_email:
            # Nothing to validate against yet (incomplete create payload handled
            # elsewhere by required-field checks).
            return attrs

        # ── Collect guardian emails to check ──────────────────────────────
        # Case A: guardians are present in this request payload.
        if guardians_data is not None:
            conflicting = [
                gd.get('email', '')
                for gd in guardians_data
                if gd.get('email', '').lower() == student_email.lower()
            ]
            if conflicting:
                raise serializers.ValidationError(
                    {
                        'guardians': (
                            "A guardian's email cannot be the same as the student's "
                            f"email ({student_email}). Please use a different email "
                            "address for the guardian."
                        )
                    }
                )

        # Case B: only the student's email is being changed — check existing
        # guardians on the instance so a rename doesn't silently create a
        # conflict with an already-saved guardian.
        elif self.instance and 'email' in attrs:
            conflicting_existing = list(
                self.instance.guardians
                .filter(email__iexact=student_email)
                .values_list('email', flat=True)
            )
            if conflicting_existing:
                raise serializers.ValidationError(
                    {
                        'email': (
                            f"The new student email ({student_email}) conflicts with "
                            "an existing guardian email on this account. Update or "
                            "remove the guardian's email first."
                        )
                    }
                )

        return attrs

    def create(self, validated_data):
        guardians_data = validated_data.pop('guardians', [])
        user = CustomUser.objects.create_user(**validated_data)
        for guardian_data in guardians_data:
            guardian_data.pop('id', None)   # PK is meaningless on create
            ParentGuardian.objects.create(student=user, **guardian_data)
        return user

    def update(self, instance, validated_data):
        # Pop guardians BEFORE calling super() so DRF never sees the nested
        # data and cannot raise its "writable nested fields" AssertionError.
        guardians_data = validated_data.pop('guardians', None)

        # Let DRF handle all the flat user fields normally.
        instance = super().update(instance, validated_data)

        # Sync guardians only when the key was actually present in the payload.
        # A PATCH request that omits "guardians" entirely leaves them untouched.
        if guardians_data is not None:
            self._sync_guardians(instance, guardians_data)

        return instance

    def _sync_guardians(self, user, guardians_data):
        """
        Full replace / upsert for the guardian list.

        Rules
        -----
        • Dict has an `id` that belongs to this user  → UPDATE in place.
        • Dict has no `id` (or id=None / unknown id)  → CREATE new record.
        • Existing guardian whose `id` is absent       → DELETE (removed by caller).
        """
        existing_map = {g.id: g for g in user.guardians.all()}
        incoming_ids = set()

        for guardian_data in guardians_data:
            guardian_id = guardian_data.pop('id', None)

            if guardian_id and guardian_id in existing_map:
                # ── UPDATE ────────────────────────────────────────────────
                guardian = existing_map[guardian_id]
                for attr, value in guardian_data.items():
                    setattr(guardian, attr, value)
                guardian.save()
                incoming_ids.add(guardian_id)
            else:
                # ── CREATE ────────────────────────────────────────────────
                new_guardian = ParentGuardian.objects.create(
                    student=user, **guardian_data
                )
                incoming_ids.add(new_guardian.id)

        # ── DELETE guardians removed from the list ────────────────────────
        ids_to_delete = set(existing_map.keys()) - incoming_ids
        if ids_to_delete:
            ParentGuardian.objects.filter(id__in=ids_to_delete).delete()