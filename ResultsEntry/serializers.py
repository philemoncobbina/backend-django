from rest_framework import serializers
from django.utils import timezone
from datetime import datetime
import pytz
from .models import Course, ClassCourse, Result, CourseResult, ResultChangeLog, ClassSize
from authapp.models import CustomUser


class CourseSerializer(serializers.ModelSerializer):
    created_by_name = serializers.SerializerMethodField(read_only=True)

    class Meta:
        model = Course
        fields = ['id', 'name', 'code', 'created_at', 'updated_at', 'created_by', 'created_by_name']
        read_only_fields = ['created_by', 'created_at', 'updated_at']

    def get_created_by_name(self, obj):
        if obj.created_by:
            return f"{obj.created_by.first_name} {obj.created_by.last_name}".strip()
        return None

    def create(self, validated_data):
        validated_data['created_by'] = self.context['request'].user
        return super().create(validated_data)


class ClassSizeSerializer(serializers.ModelSerializer):
    term_display = serializers.CharField(source='get_term_display', read_only=True)

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
            'created_at'
        ]
        read_only_fields = ['last_updated', 'created_at']


class ClassCourseSerializer(serializers.ModelSerializer):
    course_name = serializers.StringRelatedField(source='course', read_only=True)

    class Meta:
        model = ClassCourse
        fields = [
            'id',
            'course',
            'course_name',
            'class_name',
            'term',
            'teacher_name',
            'teacher_email',
            'teacher_phone',
        ]


class ClassCourseDetailSerializer(serializers.ModelSerializer):
    course = CourseSerializer(read_only=True)

    class Meta:
        model = ClassCourse
        fields = [
            'id',
            'course',
            'class_name',
            'term',
            'teacher_name',
            'teacher_email',
            'teacher_phone',
        ]


# ---------------------------------------------------------------------------
# New serializer: used exclusively by the student course-viewing endpoints.
# Flattens course + teacher info into a single, student-friendly shape.
# ---------------------------------------------------------------------------
class StudentCourseSerializer(serializers.ModelSerializer):
    """
    A flat representation of a ClassCourse record intended for students.
    Exposes course details together with the assigned teacher's information
    so that a student can see every subject they are enrolled in and who
    teaches it — without needing access to result or score data.
    """
    course_id = serializers.IntegerField(source='course.id', read_only=True)
    course_name = serializers.CharField(source='course.name', read_only=True)
    course_code = serializers.CharField(source='course.code', read_only=True)
    term_display = serializers.CharField(source='get_term_display', read_only=True)

    class Meta:
        model = ClassCourse
        fields = [
            'id',               # ClassCourse PK — useful for client-side keying
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
    course_name = serializers.CharField(source='class_course.course.name', read_only=True)
    course_code = serializers.CharField(source='class_course.course.code', read_only=True)
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
            'position_context'
        ]


class ResultSerializer(serializers.ModelSerializer):
    course_results = CourseResultSerializer(many=True, read_only=True)
    student_name = serializers.SerializerMethodField(read_only=True)
    term_display = serializers.CharField(source='get_term_display', read_only=True)
    total_score = serializers.FloatField(read_only=True)
    average_score = serializers.FloatField(read_only=True)
    attendance_percentage = serializers.FloatField(read_only=True)
    position_context = serializers.CharField(read_only=True)
    report_card_url = serializers.SerializerMethodField(read_only=True)

    class Meta:
        model = Result
        fields = '__all__'
        extra_kwargs = {
            'promoted_to': {'required': False, 'allow_null': True, 'allow_blank': True},
            'report_card_pdf': {'read_only': True}
        }

    def get_student_name(self, obj):
        if hasattr(obj, 'student') and obj.student:
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
        status = data.get('status')
        scheduled_date = data.get('scheduled_date')
        term = data.get('term', getattr(self.instance, 'term', None) if self.instance else None)

        if status == 'SCHEDULED':
            if not scheduled_date:
                raise serializers.ValidationError(
                    {'scheduled_date': 'Scheduled date is required when status is SCHEDULED'}
                )

            if isinstance(scheduled_date, str):
                try:
                    scheduled_date = datetime.fromisoformat(scheduled_date.replace('Z', '+00:00'))
                    scheduled_date = pytz.utc.localize(scheduled_date.replace(tzinfo=None))
                except ValueError:
                    raise serializers.ValidationError(
                        {'scheduled_date': 'Invalid date format. Use ISO format (YYYY-MM-DDTHH:MM:SSZ)'}
                    )

            if scheduled_date <= timezone.now():
                raise serializers.ValidationError(
                    {'scheduled_date': 'Scheduled date must be in the future'}
                )

            data['scheduled_date'] = scheduled_date

        if term == 'third':
            promoted_to = data.get('promoted_to')
            class_name = data.get(
                'class_name',
                getattr(self.instance, 'class_name', None) if self.instance else None
            )

            if not promoted_to or promoted_to.strip() == '':
                raise serializers.ValidationError(
                    {'promoted_to': 'Promoted to class must be specified for third term results'}
                )
            if promoted_to == class_name:
                raise serializers.ValidationError(
                    {'promoted_to': 'Student cannot be promoted to the same class'}
                )
        else:
            data['promoted_to'] = None

        return data


class ResultCreateSerializer(serializers.ModelSerializer):
    course_results = CourseResultSerializer(many=True)

    class Meta:
        model = Result
        fields = '__all__'
        extra_kwargs = {
            'promoted_to': {'required': False, 'allow_null': True, 'allow_blank': True},
            'report_card_pdf': {'read_only': True}
        }

    def validate(self, data):
        status = data.get('status')
        scheduled_date = data.get('scheduled_date')
        term = data.get('term')

        if status == 'SCHEDULED':
            if not scheduled_date:
                raise serializers.ValidationError(
                    {'scheduled_date': 'Scheduled date is required when status is SCHEDULED'}
                )

            if isinstance(scheduled_date, str):
                try:
                    scheduled_date = datetime.fromisoformat(scheduled_date.replace('Z', '+00:00'))
                    scheduled_date = pytz.utc.localize(scheduled_date.replace(tzinfo=None))
                except ValueError:
                    raise serializers.ValidationError(
                        {'scheduled_date': 'Invalid date format. Use ISO format (YYYY-MM-DDTHH:MM:SSZ)'}
                    )

            if scheduled_date <= timezone.now():
                raise serializers.ValidationError(
                    {'scheduled_date': 'Scheduled date must be in the future'}
                )

            data['scheduled_date'] = scheduled_date

        if status == 'PUBLISHED' and not self.instance:
            data['published_date'] = timezone.now()

        if term == 'third':
            promoted_to = data.get('promoted_to')
            class_name = data.get('class_name')

            if promoted_to is None or str(promoted_to).strip() == '':
                raise serializers.ValidationError({
                    'promoted_to': 'Promoted to class must be specified for third term results'
                })

            if str(promoted_to).strip() == str(class_name).strip():
                raise serializers.ValidationError({
                    'promoted_to': 'Student cannot be promoted to the same class'
                })
        else:
            data['promoted_to'] = None

        return data

    def save(self, **kwargs):
        instance = super().save(**kwargs)
        if hasattr(self, 'initial_data') and 'course_results' in self.initial_data:
            instance._scores_changed = True
        return instance

    def create(self, validated_data):
        course_results_data = validated_data.pop('course_results', [])

        term = validated_data.get('term')
        if term == 'third':
            promoted_to = validated_data.get('promoted_to')
            if promoted_to is None or str(promoted_to).strip() == '':
                raise serializers.ValidationError({
                    'promoted_to': 'Promoted to class must be specified for third term results'
                })
            validated_data['promoted_to'] = str(promoted_to).strip()
        else:
            validated_data['promoted_to'] = None

        result = Result.objects.create(**validated_data)

        for course_result_data in course_results_data:
            CourseResult.objects.create(result=result, **course_result_data)

        if course_results_data:
            result._scores_changed = True

        return result

    def _create_log_entry(self, result, user_email, field_name, previous_value, new_value):
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
            'student', 'class_name', 'term', 'academic_year', 'days_present',
            'days_absent', 'conduct', 'promoted_to', 'general_remarks'
        ]
        for field in significant_fields:
            if field in validated_data and getattr(instance, field) != validated_data[field]:
                return True
        return False

    def update(self, instance, validated_data):
        course_results_data = validated_data.pop('course_results', [])
        user_email = self.context['request'].user.email

        scores_changed = bool(course_results_data)

        term = validated_data.get('term', instance.term)
        if term == 'third':
            promoted_to = validated_data.get('promoted_to')
            if promoted_to is None or str(promoted_to).strip() == '':
                raise serializers.ValidationError({
                    'promoted_to': 'Promoted to class must be specified for third term results'
                })
            validated_data['promoted_to'] = str(promoted_to).strip()
        else:
            validated_data['promoted_to'] = None

        has_significant_changes = self._has_significant_changes(validated_data, instance)
        has_course_changes = len(course_results_data) > 0

        for field in ['student', 'class_name', 'term', 'status', 'promoted_to', 'days_present',
                      'days_absent', 'conduct', 'general_remarks']:
            if field in validated_data and getattr(instance, field) != validated_data[field]:
                self._create_log_entry(
                    instance, user_email, field,
                    getattr(instance, field), validated_data[field]
                )

        if validated_data.get('status') == 'PUBLISHED' and instance.status != 'PUBLISHED':
            validated_data['published_date'] = timezone.now()
            self._create_log_entry(
                instance, user_email, 'published_date',
                instance.published_date, validated_data['published_date']
            )

        for key, value in validated_data.items():
            setattr(instance, key, value)
        instance.save()

        existing_course_results = {cr.class_course_id: cr for cr in instance.course_results.all()}
        course_results_modified = False

        for course_result_data in course_results_data:
            class_course = course_result_data.get('class_course')

            if class_course.id in existing_course_results:
                course_result = existing_course_results[class_course.id]
                course_name = course_result.class_course.course.name
                course_data_changed = False

                for field, value in course_result_data.items():
                    if field != 'class_course' and getattr(course_result, field) != value:
                        course_data_changed = True
                        scores_changed = True
                        field_display = field.replace('_', ' ').title()
                        self._create_log_entry(
                            instance, user_email,
                            f"{course_name} - {field_display}",
                            getattr(course_result, field), value
                        )

                if course_data_changed:
                    course_results_modified = True
                    for key, value in course_result_data.items():
                        setattr(course_result, key, value)
                    course_result.save()
            else:
                course_results_modified = True
                scores_changed = True
                new_course_result = CourseResult.objects.create(result=instance, **course_result_data)
                course_name = new_course_result.class_course.course.name
                self._create_log_entry(
                    instance, user_email, f"{course_name}",
                    "Not present",
                    f"Added with scores: {new_course_result.class_score}/{new_course_result.exam_score}"
                )

        for class_course_id, course_result in existing_course_results.items():
            if class_course_id not in [cr_data.get('class_course').id for cr_data in course_results_data]:
                course_results_modified = True
                scores_changed = True
                course_name = course_result.class_course.course.name
                self._create_log_entry(
                    instance, user_email, f"{course_name}",
                    f"Scores: {course_result.class_score}/{course_result.exam_score}", "Removed"
                )
                course_result.delete()

        if has_significant_changes or course_results_modified:
            instance._regenerate_pdf = True
        if scores_changed:
            instance._scores_changed = True

        return instance


class ResultChangeLogSerializer(serializers.ModelSerializer):
    changed_by_name = serializers.SerializerMethodField()

    class Meta:
        model = ResultChangeLog
        fields = ['id', 'result', 'changed_by', 'changed_by_name', 'changed_at', 'field_name', 'previous_value', 'new_value']
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
    """Serializer for bulk updating result status for a class/term"""
    class_name = serializers.CharField(required=True)
    term = serializers.CharField(required=True)
    status = serializers.ChoiceField(choices=['DRAFT', 'SCHEDULED', 'PUBLISHED'], required=True)
    scheduled_date = serializers.DateTimeField(required=False, allow_null=True)

    def validate(self, data):
        status = data.get('status')
        scheduled_date = data.get('scheduled_date')

        if status == 'SCHEDULED':
            if not scheduled_date:
                raise serializers.ValidationError(
                    {'scheduled_date': 'Scheduled date is required when status is SCHEDULED'}
                )

            if isinstance(scheduled_date, str):
                try:
                    scheduled_date = datetime.fromisoformat(scheduled_date.replace('Z', '+00:00'))
                    scheduled_date = pytz.utc.localize(scheduled_date.replace(tzinfo=None))
                except ValueError:
                    raise serializers.ValidationError(
                        {'scheduled_date': 'Invalid date format. Use ISO format (YYYY-MM-DDTHH:MM:SSZ)'}
                    )

            if scheduled_date <= timezone.now():
                raise serializers.ValidationError(
                    {'scheduled_date': 'Scheduled date must be in the future'}
                )

            data['scheduled_date'] = scheduled_date

        return data