from django.db import transaction
from rest_framework import serializers

from authapp.models import CustomUser
from .models import (
    AcademicTerm, Holiday, ClassAttendanceRegister, AttendanceRecord,
    AttendanceChangeLog, describe_changes,
)
from .cache_utils import invalidate_roster, invalidate_summaries


class HolidaySerializer(serializers.ModelSerializer):
    class Meta:
        model = Holiday
        fields = ['id', 'term', 'date', 'reason', 'created_at']
        read_only_fields = ['created_at']


class AcademicTermSerializer(serializers.ModelSerializer):
    term_display = serializers.CharField(source='get_term_display', read_only=True)
    total_school_days = serializers.IntegerField(read_only=True)
    holidays = HolidaySerializer(many=True, read_only=True)

    class Meta:
        model = AcademicTerm
        fields = [
            'id', 'academic_year', 'term', 'term_display',
            'start_date', 'end_date', 'exclude_weekends', 'is_current',
            'total_school_days', 'holidays', 'created_at', 'updated_at',
        ]
        read_only_fields = ['created_at', 'updated_at']

    def validate(self, attrs):
        start = attrs.get('start_date', getattr(self.instance, 'start_date', None))
        end = attrs.get('end_date', getattr(self.instance, 'end_date', None))
        if start and end and start >= end:
            raise serializers.ValidationError("Start date must be before end date.")
        return attrs


class StudentMiniSerializer(serializers.ModelSerializer):
    full_name = serializers.SerializerMethodField()

    class Meta:
        model = CustomUser
        fields = ['id', 'index_number', 'first_name', 'last_name', 'full_name', 'class_name']

    def get_full_name(self, obj):
        return f"{obj.first_name} {obj.last_name}".strip() or obj.username


class AttendanceRecordSerializer(serializers.ModelSerializer):
    student_detail = StudentMiniSerializer(source='student', read_only=True)
    status_display = serializers.CharField(source='get_status_display', read_only=True)

    class Meta:
        model = AttendanceRecord
        fields = [
            'id', 'register', 'student', 'student_detail',
            'status', 'status_display', 'absence_reason',
            'created_at', 'updated_at',
        ]
        read_only_fields = ['created_at', 'updated_at']

    def validate(self, attrs):
        status_value = attrs.get('status', getattr(self.instance, 'status', None))
        reason = attrs.get('absence_reason', getattr(self.instance, 'absence_reason', None))
        if status_value == 'absent' and not (reason and reason.strip()):
            raise serializers.ValidationError(
                {'absence_reason': "A reason is required when marking a student absent."}
            )
        return attrs

    def create(self, validated_data):
        if validated_data.get('status') == 'present':
            validated_data['absence_reason'] = None
        return super().create(validated_data)

    def update(self, instance, validated_data):
        if validated_data.get('status', instance.status) == 'present':
            validated_data['absence_reason'] = None
        return super().update(instance, validated_data)


class AttendanceChangeLogSerializer(serializers.ModelSerializer):
    student_name = serializers.SerializerMethodField()
    action_type_display = serializers.CharField(source='get_action_type_display', read_only=True)

    class Meta:
        model = AttendanceChangeLog
        fields = [
            'id', 'register', 'record', 'student', 'student_name',
            'user', 'user_email', 'action_type', 'action_type_display',
            'changed_fields', 'timestamp',
        ]
        read_only_fields = fields

    def get_student_name(self, obj):
        if obj.student:
            return f"{obj.student.first_name} {obj.student.last_name}".strip()
        return None


class ClassAttendanceRegisterSerializer(serializers.ModelSerializer):
    class_name_display = serializers.CharField(source='get_class_name_display', read_only=True)
    marked_by_name = serializers.SerializerMethodField()
    present_count = serializers.IntegerField(read_only=True)
    absent_count = serializers.IntegerField(read_only=True)

    class Meta:
        model = ClassAttendanceRegister
        fields = [
            'id', 'class_name', 'class_name_display', 'term', 'date',
            'marked_by', 'marked_by_name', 'is_finalized', 'finalized_at',
            'present_count', 'absent_count', 'created_at', 'updated_at',
        ]
        read_only_fields = ['marked_by', 'finalized_at', 'created_at', 'updated_at']

    def get_marked_by_name(self, obj):
        if obj.marked_by:
            return f"{obj.marked_by.first_name} {obj.marked_by.last_name}".strip()
        return None


class ClassAttendanceRegisterDetailSerializer(ClassAttendanceRegisterSerializer):
    records = AttendanceRecordSerializer(many=True, read_only=True)
    logs = AttendanceChangeLogSerializer(many=True, read_only=True)

    class Meta(ClassAttendanceRegisterSerializer.Meta):
        fields = ClassAttendanceRegisterSerializer.Meta.fields + ['records', 'logs']


class MarkAttendanceEntrySerializer(serializers.Serializer):
    """One student's mark within a bulk mark-attendance request."""
    student = serializers.PrimaryKeyRelatedField(queryset=CustomUser.objects.filter(role='student'))
    status = serializers.ChoiceField(choices=AttendanceRecord._meta.get_field('status').choices)
    absence_reason = serializers.CharField(required=False, allow_blank=True, allow_null=True)

    def validate(self, attrs):
        if attrs['status'] == 'absent' and not (attrs.get('absence_reason') or '').strip():
            raise serializers.ValidationError(
                {'absence_reason': "A reason is required when marking a student absent."}
            )
        if attrs['status'] == 'present':
            attrs['absence_reason'] = None
        return attrs


class MarkAttendanceSerializer(serializers.Serializer):
    """Bulk-mark attendance for a whole class register on a given date."""
    class_name = serializers.ChoiceField(choices=CustomUser.CLASS_CHOICES)
    term = serializers.PrimaryKeyRelatedField(queryset=AcademicTerm.objects.all())
    date = serializers.DateField()
    records = MarkAttendanceEntrySerializer(many=True)

    def validate_records(self, value):
        if not value:
            raise serializers.ValidationError("At least one student record is required.")
        student_ids = [entry['student'].id for entry in value]
        if len(student_ids) != len(set(student_ids)):
            raise serializers.ValidationError("Duplicate student entries are not allowed in the same submission.")
        return value

    def validate(self, attrs):
        term = attrs['term']
        date = attrs['date']
        if not term.is_school_day(date):
            raise serializers.ValidationError(
                "This date is not a valid school day for the selected term "
                "(outside term dates, a weekend, or a holiday)."
            )
        return attrs

    @transaction.atomic
    def save(self, marked_by):
        class_name = self.validated_data['class_name']
        term = self.validated_data['term']
        date = self.validated_data['date']
        records = self.validated_data['records']

        register, _ = ClassAttendanceRegister.objects.get_or_create(
            class_name=class_name, term=term, date=date,
            defaults={'marked_by': marked_by},
        )
        if register.is_finalized:
            raise serializers.ValidationError(
                "This register has been finalized. Ask a principal to reopen it before making changes."
            )

        changed_student_ids = []

        for entry in records:
            existing = AttendanceRecord.objects.filter(
                register=register, student=entry['student']
            ).first()
            old_values = {
                'status': existing.status if existing else None,
                'absence_reason': existing.absence_reason if existing else None,
            }

            record, created = AttendanceRecord.objects.update_or_create(
                register=register,
                student=entry['student'],
                defaults={
                    'status': entry['status'],
                    'absence_reason': entry.get('absence_reason'),
                },
            )

            new_values = {'status': record.status, 'absence_reason': record.absence_reason}
            if created:
                action_type = 'CREATE'
            elif old_values['status'] != new_values['status']:
                action_type = 'STATUS_CHANGE'
            else:
                action_type = 'UPDATE'

            if created or old_values != new_values:
                AttendanceChangeLog.objects.create(
                    register=register,
                    record=record,
                    student=record.student,
                    user=marked_by,
                    user_email=marked_by.email,
                    action_type=action_type,
                    changed_fields=describe_changes(old_values, new_values),
                )
                changed_student_ids.append(record.student_id)

        register.marked_by = marked_by
        register.save()

        # FIX (race condition): this whole method runs inside
        # @transaction.atomic. Calling invalidate_roster/invalidate_summaries
        # directly here — as the old code did — fires them *before* the
        # transaction actually commits. A concurrent read that lands in
        # that window will see the cache empty, re-read the still-old
        # (uncommitted-write) data from the database, and cache *that*
        # stale snapshot — which then sits there for the full 5-minute
        # TTL even after this write finishes committing.
        #
        # transaction.on_commit() defers these calls until Django knows
        # the transaction has actually succeeded, so no reader can ever
        # observe a "cache empty, DB stale" gap. If this method is later
        # called from inside an even-larger enclosing transaction, this
        # will correctly wait for *that* outer transaction to commit too.
        transaction.on_commit(lambda: invalidate_roster(class_name, term.id, date))
        transaction.on_commit(lambda: invalidate_summaries(changed_student_ids, term.id))

        return register


class StudentAttendanceSummarySerializer(serializers.Serializer):
    student = serializers.IntegerField()
    student_name = serializers.CharField()
    term = serializers.IntegerField()
    total_school_days = serializers.IntegerField()
    days_present = serializers.IntegerField()
    days_absent = serializers.IntegerField()
    days_unmarked = serializers.IntegerField()
    attendance_percentage = serializers.FloatField()