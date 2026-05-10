from rest_framework import serializers
from authapp.models import CustomUser, ParentGuardian


class ParentGuardianSerializer(serializers.ModelSerializer):
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
        ]
        extra_kwargs = {
            'first_name': {'required': True},
            'last_name': {'required': True},
            'relationship': {'required': True},
            'primary_phone': {'required': True},
        }

    def validate(self, attrs):
        """
        Cross-field check: guardian email must not match the student's email.
        The student email is injected via serializer context in two ways:
          - On student creation: context['student_email'] = the incoming student email.
          - On standalone guardian create/update: context['student'] = the student instance.
        """
        guardian_email = attrs.get('email', '').strip().lower()

        if guardian_email:
            student_email = None

            # Case 1 – nested inside StudentUserSerializer (new student flow)
            if 'student_email' in self.context:
                student_email = self.context['student_email'].strip().lower()

            # Case 2 – standalone guardian endpoint (student instance in context)
            elif 'student' in self.context:
                student_email = self.context['student'].email.strip().lower()

            if student_email and guardian_email == student_email:
                raise serializers.ValidationError(
                    {'email': (
                        "A guardian's email cannot be the same as the student's email. "
                        "Please provide a different email address for this guardian."
                    )}
                )

        return attrs


class StudentUserSerializer(serializers.ModelSerializer):
    password = serializers.CharField(
        write_only=True, required=True, style={'input_type': 'password'}
    )
    guardians = ParentGuardianSerializer(many=True, required=False)

    class Meta:
        model = CustomUser
        fields = [
            'email',
            'username',
            'first_name',
            'last_name',
            'password',
            'index_number',
            'class_name',
            'role',
            'guardians',
        ]
        extra_kwargs = {
            'index_number': {'required': True},
            'class_name': {'required': True},
            'first_name': {'required': True},
            'last_name': {'required': True},
        }

    def validate(self, attrs):
        # ── Index number uniqueness ────────────────────────────────────────
        index_number = attrs.get('index_number')
        if CustomUser.objects.filter(index_number=index_number).exists():
            raise serializers.ValidationError(
                {"index_number": "A student with this index number already exists."}
            )

        student_email = attrs.get('email', '').strip().lower()
        guardians = attrs.get('guardians', [])

        # ── Guardian email must not match student email ────────────────────
        for i, guardian in enumerate(guardians):
            guardian_email = guardian.get('email', '').strip().lower()
            if guardian_email and guardian_email == student_email:
                raise serializers.ValidationError(
                    {
                        "guardians": (
                            f"Guardian {i + 1}'s email cannot be the same as the "
                            "student's email. Please provide a different email address."
                        )
                    }
                )

        # ── Primary contact enforcement ────────────────────────────────────
        primary_count = sum(1 for g in guardians if g.get('is_primary_contact'))
        if guardians and primary_count == 0:
            guardians[0]['is_primary_contact'] = True
        elif primary_count > 1:
            raise serializers.ValidationError(
                {"guardians": "Only one guardian can be marked as the primary contact."}
            )

        attrs['role'] = 'student'
        return attrs

    def _get_guardian_serializers_with_context(self, guardians_data, student_email):
        """
        Re-validate each guardian dict with student_email injected into context
        so ParentGuardianSerializer.validate() can also catch the clash
        (belt-and-suspenders for any direct use of ParentGuardianSerializer).
        """
        ctx = {**self.context, 'student_email': student_email}
        validated = []
        for data in guardians_data:
            s = ParentGuardianSerializer(data=data, context=ctx)
            s.is_valid(raise_exception=True)
            validated.append(s.validated_data)
        return validated

    def create(self, validated_data):
        guardians_data = validated_data.pop('guardians', [])
        password = validated_data.pop('password')

        index_number = validated_data.get('index_number')
        class_name = validated_data.get('class_name')
        email = validated_data.get('email')
        first_name = validated_data.get('first_name')
        last_name = validated_data.get('last_name')

        if not validated_data.get('username'):
            validated_data['username'] = index_number.lower()

        extra = {
            k: v for k, v in validated_data.items()
            if k not in ('email', 'first_name', 'last_name', 'index_number',
                         'class_name', 'username', 'role')
        }

        user = CustomUser.objects.create_student(
            email=email,
            first_name=first_name,
            last_name=last_name,
            password=password,
            index_number=index_number,
            class_name=class_name,
            username=validated_data.get('username', index_number.lower()),
            **extra,
        )

        for guardian_data in guardians_data:
            ParentGuardian.objects.create(student=user, **guardian_data)

        return user