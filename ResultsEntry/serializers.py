from datetime import datetime

import pytz
from django.db import transaction
from django.utils import timezone
from rest_framework import serializers

from authapp.models import CustomUser

from .models import (
    Course,
    ClassCourse,
    Result,
    CourseResult,
    ResultChangeLog,
    ClassSize,
)


class CourseSerializer(serializers.ModelSerializer):
    created_by_name = serializers.SerializerMethodField(read_only=True)

    class Meta:
        model = Course
        fields = [
            'id',
            'name',
            'code',
            'created_at',
            'updated_at',
            'created_by',
            'created_by_name',
        ]
        read_only_fields = [
            'created_by',
            'created_at',
            'updated_at',
        ]

    def get_created_by_name(self, obj):
        if obj.created_by:
            return f"{obj.created_by.first_name} {obj.created_by.last_name}".strip()
        return None

    def create(self, validated_data):
        validated_data['created_by'] = self.context['request'].user
        return super().create(validated_data)


class ClassSizeSerializer(serializers.ModelSerializer):
    term_display = serializers.CharField(
        source='get_term_display',
        read_only=True
    )

    class Meta:
        model = ClassSize
        fields = [
            'id',
            'class_name',
            'term',
            'term_display',
            'academic_year',
            'total_students',
            'last_updated',
            'created_at',
        ]
        read_only_fields = [
            'last_updated',
            'created_at',
        ]


class ClassCourseSerializer(serializers.ModelSerializer):
    course_name = serializers.StringRelatedField(
        source='course',
        read_only=True
    )
    term_display = serializers.CharField(
        source='get_term_display',
        read_only=True
    )

    class Meta:
        model = ClassCourse
        fields = [
            'id',
            'course',
            'course_name',
            'class_name',
            'term',
            'term_display',
            'is_active',
            'teacher_name',
            'teacher_email',
            'teacher_phone',
        ]


class ClassCourseDetailSerializer(serializers.ModelSerializer):
    course = CourseSerializer(read_only=True)
    term_display = serializers.CharField(
        source='get_term_display',
        read_only=True
    )

    class Meta:
        model = ClassCourse
        fields = [
            'id',
            'course',
            'class_name',
            'term',
            'term_display',
            'is_active',
            'teacher_name',
            'teacher_email',
            'teacher_phone',
        ]


class StudentCourseSerializer(serializers.ModelSerializer):
    """
    Flat representation of ClassCourse for student course viewing.
    """
    course_id = serializers.IntegerField(
        source='course.id',
        read_only=True
    )
    course_name = serializers.CharField(
        source='course.name',
        read_only=True
    )
    course_code = serializers.CharField(
        source='course.code',
        read_only=True
    )
    term_display = serializers.CharField(
        source='get_term_display',
        read_only=True
    )

    class Meta:
        model = ClassCourse
        fields = [
            'id',
            'course_id',
            'course_name',
            'course_code',
            'class_name',
            'term',
            'term_display',
            'teacher_name',
            'teacher_email',
            'teacher_phone',
        ]


class CourseResultSerializer(serializers.ModelSerializer):
    course_name = serializers.CharField(
        source='class_course.course.name',
        read_only=True
    )
    course_code = serializers.CharField(
        source='class_course.course.code',
        read_only=True
    )
    total_score = serializers.FloatField(read_only=True)
    grade = serializers.CharField(read_only=True)
    position = serializers.IntegerField(read_only=True)
    position_context = serializers.CharField(read_only=True)

    class Meta:
        model = CourseResult
        fields = [
            'id',
            'class_course',
            'course_name',
            'course_code',
            'class_score',
            'exam_score',
            'total_score',
            'grade',
            'remarks',
            'position',
            'position_context',
        ]


class ResultSerializer(serializers.ModelSerializer):
    course_results = CourseResultSerializer(many=True, read_only=True)

    student_name = serializers.SerializerMethodField(read_only=True)
    term_display = serializers.CharField(
        source='get_term_display',
        read_only=True
    )

    total_score = serializers.FloatField(read_only=True)
    average_score = serializers.FloatField(read_only=True)
    position_context = serializers.CharField(read_only=True)

    days_present = serializers.IntegerField(read_only=True)
    days_absent = serializers.IntegerField(read_only=True)
    total_days = serializers.IntegerField(read_only=True)
    total_school_days = serializers.IntegerField(read_only=True)
    total_marked_attendance_days = serializers.IntegerField(read_only=True)
    days_unmarked = serializers.IntegerField(read_only=True)
    attendance_percentage = serializers.FloatField(read_only=True)
    marked_attendance_percentage = serializers.FloatField(read_only=True)

    report_card_url = serializers.SerializerMethodField(read_only=True)

    class Meta:
        model = Result
        fields = [
            'id',
            'student',
            'student_name',
            'class_name',
            'term',
            'term_display',
            'academic_year',
            'status',
            'scheduled_date',
            'published_date',
            'overall_position',
            'position_context',
            'class_teacher_remarks',
            'promoted_to',
            'next_term_begins',
            'report_card_pdf',
            'report_card_url',
            'created_at',
            'updated_at',

            'days_present',
            'days_absent',
            'total_days',
            'total_school_days',
            'total_marked_attendance_days',
            'days_unmarked',
            'attendance_percentage',
            'marked_attendance_percentage',

            'total_score',
            'average_score',
            'course_results',
        ]
        read_only_fields = [
            'published_date',
            'report_card_pdf',
            'created_at',
            'updated_at',
            'days_present',
            'days_absent',
            'total_days',
            'total_school_days',
            'total_marked_attendance_days',
            'days_unmarked',
            'attendance_percentage',
            'marked_attendance_percentage',
            'total_score',
            'average_score',
            'overall_position',
            'position_context',
        ]
        extra_kwargs = {
            'promoted_to': {
                'required': False,
                'allow_null': True,
                'allow_blank': True,
            },
        }

    def get_student_name(self, obj):
        if obj.student:
            return f"{obj.student.first_name} {obj.student.last_name}".strip()
        return None

    def get_report_card_url(self, obj):
        if obj.report_card_pdf and hasattr(obj.report_card_pdf, 'url'):
            request = self.context.get('request')

            if request is not None:
                return request.build_absolute_uri(obj.report_card_pdf.url)

            return obj.report_card_pdf.url

        return None

    def validate(self, data):
        status_value = data.get(
            'status',
            getattr(self.instance, 'status', None) if self.instance else None
        )
        scheduled_date = data.get(
            'scheduled_date',
            getattr(self.instance, 'scheduled_date', None) if self.instance else None
        )
        term = data.get(
            'term',
            getattr(self.instance, 'term', None) if self.instance else None
        )
        class_name = data.get(
            'class_name',
            getattr(self.instance, 'class_name', None) if self.instance else None
        )

        if status_value == 'SCHEDULED':
            scheduled_date = self._validate_scheduled_date(scheduled_date)
            data['scheduled_date'] = scheduled_date

        if term == 'third':
            promoted_to = data.get(
                'promoted_to',
                getattr(self.instance, 'promoted_to', None) if self.instance else None
            )

            if promoted_to is None or str(promoted_to).strip() == '':
                raise serializers.ValidationError({
                    'promoted_to': 'Promoted to class must be specified for third term results.'
                })

            if str(promoted_to).strip() == str(class_name).strip():
                raise serializers.ValidationError({
                    'promoted_to': 'Student cannot be promoted to the same class.'
                })

            data['promoted_to'] = str(promoted_to).strip()
        else:
            data['promoted_to'] = None

        return data

    def _validate_scheduled_date(self, scheduled_date):
        if not scheduled_date:
            raise serializers.ValidationError({
                'scheduled_date': 'Scheduled date is required when status is SCHEDULED.'
            })

        if isinstance(scheduled_date, str):
            try:
                scheduled_date = datetime.fromisoformat(
                    scheduled_date.replace('Z', '+00:00')
                )
                scheduled_date = pytz.utc.localize(
                    scheduled_date.replace(tzinfo=None)
                )
            except ValueError:
                raise serializers.ValidationError({
                    'scheduled_date': 'Invalid date format. Use ISO format, e.g. YYYY-MM-DDTHH:MM:SSZ.'
                })

        if timezone.is_naive(scheduled_date):
            scheduled_date = timezone.make_aware(
                scheduled_date,
                timezone.get_current_timezone()
            )

        if scheduled_date <= timezone.now():
            raise serializers.ValidationError({
                'scheduled_date': 'Scheduled date must be in the future.'
            })

        return scheduled_date


class ResultCreateSerializer(serializers.ModelSerializer):
    course_results = CourseResultSerializer(
        many=True,
        required=False
    )

    student_name = serializers.SerializerMethodField(read_only=True)
    term_display = serializers.CharField(
        source='get_term_display',
        read_only=True
    )

    days_present = serializers.IntegerField(read_only=True)
    days_absent = serializers.IntegerField(read_only=True)
    total_days = serializers.IntegerField(read_only=True)
    total_school_days = serializers.IntegerField(read_only=True)
    total_marked_attendance_days = serializers.IntegerField(read_only=True)
    days_unmarked = serializers.IntegerField(read_only=True)
    attendance_percentage = serializers.FloatField(read_only=True)
    marked_attendance_percentage = serializers.FloatField(read_only=True)

    total_score = serializers.FloatField(read_only=True)
    average_score = serializers.FloatField(read_only=True)
    position_context = serializers.CharField(read_only=True)
    report_card_url = serializers.SerializerMethodField(read_only=True)

    class Meta:
        model = Result
        fields = [
            'id',
            'student',
            'student_name',
            'class_name',
            'term',
            'term_display',
            'academic_year',
            'status',
            'scheduled_date',
            'published_date',
            'overall_position',
            'position_context',
            'class_teacher_remarks',
            'promoted_to',
            'next_term_begins',
            'report_card_pdf',
            'report_card_url',
            'created_at',
            'updated_at',

            'days_present',
            'days_absent',
            'total_days',
            'total_school_days',
            'total_marked_attendance_days',
            'days_unmarked',
            'attendance_percentage',
            'marked_attendance_percentage',

            'total_score',
            'average_score',
            'course_results',
        ]
        read_only_fields = [
            'id',
            'published_date',
            'overall_position',
            'position_context',
            'report_card_pdf',
            'report_card_url',
            'created_at',
            'updated_at',
            'days_present',
            'days_absent',
            'total_days',
            'total_school_days',
            'total_marked_attendance_days',
            'days_unmarked',
            'attendance_percentage',
            'marked_attendance_percentage',
            'total_score',
            'average_score',
        ]
        extra_kwargs = {
            'promoted_to': {
                'required': False,
                'allow_null': True,
                'allow_blank': True,
            },
        }

    def get_student_name(self, obj):
        if obj.student:
            return f"{obj.student.first_name} {obj.student.last_name}".strip()
        return None

    def get_report_card_url(self, obj):
        if obj.report_card_pdf and hasattr(obj.report_card_pdf, 'url'):
            request = self.context.get('request')

            if request is not None:
                return request.build_absolute_uri(obj.report_card_pdf.url)

            return obj.report_card_pdf.url

        return None

    def validate(self, data):
        status_value = data.get(
            'status',
            getattr(self.instance, 'status', None) if self.instance else None
        )
        scheduled_date = data.get(
            'scheduled_date',
            getattr(self.instance, 'scheduled_date', None) if self.instance else None
        )
        term = data.get(
            'term',
            getattr(self.instance, 'term', None) if self.instance else None
        )
        class_name = data.get(
            'class_name',
            getattr(self.instance, 'class_name', None) if self.instance else None
        )

        if status_value == 'SCHEDULED':
            scheduled_date = self._validate_scheduled_date(scheduled_date)
            data['scheduled_date'] = scheduled_date

        if status_value == 'PUBLISHED' and not self.instance:
            data['published_date'] = timezone.now()

        if term == 'third':
            promoted_to = data.get(
                'promoted_to',
                getattr(self.instance, 'promoted_to', None) if self.instance else None
            )

            if promoted_to is None or str(promoted_to).strip() == '':
                raise serializers.ValidationError({
                    'promoted_to': 'Promoted to class must be specified for third term results.'
                })

            if str(promoted_to).strip() == str(class_name).strip():
                raise serializers.ValidationError({
                    'promoted_to': 'Student cannot be promoted to the same class.'
                })

            data['promoted_to'] = str(promoted_to).strip()
        else:
            data['promoted_to'] = None

        return data

    def _validate_scheduled_date(self, scheduled_date):
        if not scheduled_date:
            raise serializers.ValidationError({
                'scheduled_date': 'Scheduled date is required when status is SCHEDULED.'
            })

        if isinstance(scheduled_date, str):
            try:
                scheduled_date = datetime.fromisoformat(
                    scheduled_date.replace('Z', '+00:00')
                )
                scheduled_date = pytz.utc.localize(
                    scheduled_date.replace(tzinfo=None)
                )
            except ValueError:
                raise serializers.ValidationError({
                    'scheduled_date': 'Invalid date format. Use ISO format, e.g. YYYY-MM-DDTHH:MM:SSZ.'
                })

        if timezone.is_naive(scheduled_date):
            scheduled_date = timezone.make_aware(
                scheduled_date,
                timezone.get_current_timezone()
            )

        if scheduled_date <= timezone.now():
            raise serializers.ValidationError({
                'scheduled_date': 'Scheduled date must be in the future.'
            })

        return scheduled_date

    def save(self, **kwargs):
        instance = super().save(**kwargs)

        if hasattr(self, 'initial_data') and 'course_results' in self.initial_data:
            instance._scores_changed = True

        return instance

    def create(self, validated_data):
        course_results_data = validated_data.pop('course_results', [])

        with transaction.atomic():
            result = Result.objects.create(**validated_data)

            for course_result_data in course_results_data:
                course_result = CourseResult(
                    result=result,
                    **course_result_data
                )
                course_result.save(calculate_positions=False)

            if course_results_data:
                result._scores_changed = True
                result.calculate_positions()

            return result

    def _create_log_entry(
        self,
        result,
        user_email,
        field_name,
        previous_value,
        new_value
    ):
        if str(previous_value) != str(new_value):
            ResultChangeLog.objects.create(
                result=result,
                changed_by=user_email,
                field_name=field_name,
                previous_value=str(previous_value),
                new_value=str(new_value)
            )

    def _has_significant_changes(self, validated_data, instance):
        significant_fields = [
            'student',
            'class_name',
            'term',
            'academic_year',
            'status',
            'scheduled_date',
            'class_teacher_remarks',
            'promoted_to',
            'next_term_begins',
        ]

        for field in significant_fields:
            if field in validated_data and getattr(instance, field) != validated_data[field]:
                return True

        return False

    def update(self, instance, validated_data):
        course_results_data = validated_data.pop('course_results', None)
        request = self.context.get('request')
        user_email = request.user.email if request and request.user else ''

        scores_changed = course_results_data is not None
        has_significant_changes = self._has_significant_changes(
            validated_data,
            instance
        )

        if (
            validated_data.get('status') == 'PUBLISHED'
            and instance.status != 'PUBLISHED'
        ):
            validated_data['published_date'] = timezone.now()

        tracked_fields = [
            'student',
            'class_name',
            'term',
            'academic_year',
            'status',
            'scheduled_date',
            'published_date',
            'class_teacher_remarks',
            'promoted_to',
            'next_term_begins',
        ]

        for field in tracked_fields:
            if field in validated_data and getattr(instance, field) != validated_data[field]:
                self._create_log_entry(
                    instance,
                    user_email,
                    field,
                    getattr(instance, field),
                    validated_data[field]
                )

        with transaction.atomic():
            for key, value in validated_data.items():
                setattr(instance, key, value)

            instance.save()

            course_results_modified = False

            if course_results_data is not None:
                existing_course_results = {
                    cr.class_course_id: cr
                    for cr in instance.course_results.select_related(
                        'class_course',
                        'class_course__course'
                    )
                }

                incoming_class_course_ids = []

                for course_result_data in course_results_data:
                    class_course = course_result_data.get('class_course')

                    if not class_course:
                        raise serializers.ValidationError({
                            'course_results': 'Each course result must include class_course.'
                        })

                    incoming_class_course_ids.append(class_course.id)

                    if class_course.id in existing_course_results:
                        course_result = existing_course_results[class_course.id]
                        course_name = course_result.class_course.course.name
                        course_data_changed = False

                        for field, value in course_result_data.items():
                            if field == 'class_course':
                                continue

                            if getattr(course_result, field) != value:
                                course_data_changed = True
                                scores_changed = True
                                field_display = field.replace('_', ' ').title()

                                self._create_log_entry(
                                    instance,
                                    user_email,
                                    f"{course_name} - {field_display}",
                                    getattr(course_result, field),
                                    value
                                )

                        if course_data_changed:
                            course_results_modified = True

                            for key, value in course_result_data.items():
                                setattr(course_result, key, value)

                            course_result.save(calculate_positions=False)

                    else:
                        new_course_result = CourseResult(
                            result=instance,
                            **course_result_data
                        )
                        new_course_result.save(calculate_positions=False)

                        course_results_modified = True
                        scores_changed = True

                        course_name = new_course_result.class_course.course.name

                        self._create_log_entry(
                            instance,
                            user_email,
                            course_name,
                            "Not present",
                            (
                                "Added with scores: "
                                f"{new_course_result.class_score}/"
                                f"{new_course_result.exam_score}"
                            )
                        )

                for class_course_id, course_result in existing_course_results.items():
                    if class_course_id not in incoming_class_course_ids:
                        course_results_modified = True
                        scores_changed = True

                        course_name = course_result.class_course.course.name

                        self._create_log_entry(
                            instance,
                            user_email,
                            course_name,
                            (
                                "Scores: "
                                f"{course_result.class_score}/"
                                f"{course_result.exam_score}"
                            ),
                            "Removed"
                        )

                        course_result.delete()

            if has_significant_changes or course_results_modified:
                instance._regenerate_pdf = True

            if scores_changed:
                instance._scores_changed = True
                instance.calculate_positions()

        return instance


class ResultChangeLogSerializer(serializers.ModelSerializer):
    changed_by_name = serializers.SerializerMethodField()

    class Meta:
        model = ResultChangeLog
        fields = [
            'id',
            'result',
            'changed_by',
            'changed_by_name',
            'changed_at',
            'field_name',
            'previous_value',
            'new_value',
        ]
        read_only_fields = fields

    def get_changed_by_name(self, obj):
        try:
            user = CustomUser.objects.get(email=obj.changed_by)
            return f"{user.first_name} {user.last_name}".strip()
        except CustomUser.DoesNotExist:
            return obj.changed_by


class StudentSerializer(serializers.ModelSerializer):
    class Meta:
        model = CustomUser
        fields = '__all__'


class BulkResultUpdateSerializer(serializers.Serializer):
    """Serializer for bulk updating result status for a class and term."""
    class_name = serializers.CharField(required=True)
    term = serializers.CharField(required=True)
    academic_year = serializers.CharField(required=False)
    status = serializers.ChoiceField(
        choices=['DRAFT', 'SCHEDULED', 'PUBLISHED'],
        required=True
    )
    scheduled_date = serializers.DateTimeField(
        required=False,
        allow_null=True
    )

    def validate(self, data):
        status_value = data.get('status')
        scheduled_date = data.get('scheduled_date')

        if status_value == 'SCHEDULED':
            if not scheduled_date:
                raise serializers.ValidationError({
                    'scheduled_date': 'Scheduled date is required when status is SCHEDULED.'
                })

            if timezone.is_naive(scheduled_date):
                scheduled_date = timezone.make_aware(
                    scheduled_date,
                    timezone.get_current_timezone()
                )

            if scheduled_date <= timezone.now():
                raise serializers.ValidationError({
                    'scheduled_date': 'Scheduled date must be in the future.'
                })

            data['scheduled_date'] = scheduled_date

        return data