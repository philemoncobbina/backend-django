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
        # Set the user automatically
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
        fields = ['id', 'course', 'course_name', 'class_name', 'term']

class ClassCourseDetailSerializer(serializers.ModelSerializer):
    course = CourseSerializer(read_only=True)
    
    class Meta:
        model = ClassCourse
        fields = ['id', 'course', 'class_name', 'term']

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
            'report_card_pdf': {'read_only': True}  # Make report_card_pdf read-only in the serializer
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
        """
        Validate that scheduled_date is provided and in the future if status is SCHEDULED
        Also validate promoted_to for third term
        """
        status = data.get('status')
        scheduled_date = data.get('scheduled_date')
        term = data.get('term', getattr(self.instance, 'term', None) if self.instance else None)
        
        if status == 'SCHEDULED':
            if not scheduled_date:
                raise serializers.ValidationError({'scheduled_date': 'Scheduled date is required when status is SCHEDULED'})
            
            # Convert string to datetime if necessary
            if isinstance(scheduled_date, str):
                try:
                    scheduled_date = datetime.fromisoformat(scheduled_date.replace('Z', '+00:00'))
                    scheduled_date = pytz.utc.localize(scheduled_date.replace(tzinfo=None))
                except ValueError:
                    raise serializers.ValidationError({'scheduled_date': 'Invalid date format. Use ISO format (YYYY-MM-DDTHH:MM:SSZ)'})
            
            # Ensure scheduled date is in the future
            if scheduled_date <= timezone.now():
                raise serializers.ValidationError({'scheduled_date': 'Scheduled date must be in the future'})
            
            data['scheduled_date'] = scheduled_date
        
        # Validate promoted_to for third term - moved and improved validation
        if term == 'third':
            promoted_to = data.get('promoted_to')
            class_name = data.get('class_name', getattr(self.instance, 'class_name', None) if self.instance else None)
            
            # Check if promoted_to is provided and not empty/None
            if not promoted_to or promoted_to.strip() == '':
                raise serializers.ValidationError({'promoted_to': 'Promoted to class must be specified for third term results'})
            if promoted_to == class_name:
                raise serializers.ValidationError({'promoted_to': 'Student cannot be promoted to the same class'})
        else:
            # For non-third terms, explicitly set to None (not empty string)
            data['promoted_to'] = None
        
        return data

class ResultCreateSerializer(serializers.ModelSerializer):
    course_results = CourseResultSerializer(many=True)
    
    class Meta:
        model = Result
        fields = '__all__'
        extra_kwargs = {
            'promoted_to': {'required': False, 'allow_null': True, 'allow_blank': True},
            'report_card_pdf': {'read_only': True}  # Make report_card_pdf read-only in the serializer
        }
    
    def validate(self, data):
        """
        Validate that scheduled_date is provided and in the future if status is SCHEDULED
        Also validate promoted_to for third term
        """
        status = data.get('status')
        scheduled_date = data.get('scheduled_date')
        term = data.get('term')
        
        if status == 'SCHEDULED':
            if not scheduled_date:
                raise serializers.ValidationError({'scheduled_date': 'Scheduled date is required when status is SCHEDULED'})
            
            # Convert string to datetime if necessary
            if isinstance(scheduled_date, str):
                try:
                    scheduled_date = datetime.fromisoformat(scheduled_date.replace('Z', '+00:00'))
                    scheduled_date = pytz.utc.localize(scheduled_date.replace(tzinfo=None))
                except ValueError:
                    raise serializers.ValidationError({'scheduled_date': 'Invalid date format. Use ISO format (YYYY-MM-DDTHH:MM:SSZ)'})
            
            # Ensure scheduled date is in the future
            if scheduled_date <= timezone.now():
                raise serializers.ValidationError({'scheduled_date': 'Scheduled date must be in the future'})
            
            data['scheduled_date'] = scheduled_date
        
        # If status is PUBLISHED, set the published_date
        if status == 'PUBLISHED' and not self.instance:
            data['published_date'] = timezone.now()
        
        # CRITICAL FIX: Validate promoted_to for third term with better logic
        if term == 'third':
            promoted_to = data.get('promoted_to')
            class_name = data.get('class_name')
            
            # More robust validation - check for None, empty string, and whitespace
            if promoted_to is None or str(promoted_to).strip() == '':
                raise serializers.ValidationError({
                    'promoted_to': 'Promoted to class must be specified for third term results'
                })
            
            # Ensure it's not the same as current class
            if str(promoted_to).strip() == str(class_name).strip():
                raise serializers.ValidationError({
                    'promoted_to': 'Student cannot be promoted to the same class'
                })
        else:
            # For non-third terms, explicitly set to None (not empty string)
            data['promoted_to'] = None
        
        return data
    
    def save(self, **kwargs):
        """Override save to detect score changes"""
        instance = super().save(**kwargs)
        
        # Check if any course results were updated (indicating score changes)
        if hasattr(self, 'initial_data') and 'course_results' in self.initial_data:
            # Set a flag to indicate scores changed
            instance._scores_changed = True
        
        return instance
    
    def create(self, validated_data):
        course_results_data = validated_data.pop('course_results', [])
        
        # CRITICAL FIX: Ensure promoted_to is properly handled before model creation
        term = validated_data.get('term')
        if term == 'third':
            promoted_to = validated_data.get('promoted_to')
            if promoted_to is None or str(promoted_to).strip() == '':
                raise serializers.ValidationError({
                    'promoted_to': 'Promoted to class must be specified for third term results'
                })
            # Ensure it's stored as a clean string
            validated_data['promoted_to'] = str(promoted_to).strip()
        else:
            # Explicitly set to None for non-third terms
            validated_data['promoted_to'] = None
        
        # Create the result with validated data
        result = Result.objects.create(**validated_data)
        
        # Create course results
        for course_result_data in course_results_data:
            CourseResult.objects.create(result=result, **course_result_data)
        
        # Set flag to indicate scores changed for new results
        if course_results_data:
            result._scores_changed = True
        
        return result
    
    def _create_log_entry(self, result, user_email, field_name, previous_value, new_value):
        """
        Helper method to create a log entry for a change
        """
        if str(previous_value) != str(new_value):  # Only log if values are different
            ResultChangeLog.objects.create(
                result=result,
                changed_by=user_email,  # Now accepts email string directly
                field_name=field_name,
                previous_value=str(previous_value),
                new_value=str(new_value)
            )
    
    def _has_significant_changes(self, validated_data, instance):
        """
        Check if there are significant changes that would require PDF regeneration.
        This includes changes to scores, student info, class info, or course results.
        """
        # Check main result fields that affect the report card
        significant_fields = [
            'student', 'class_name', 'term', 'academic_year', 'days_present', 
            'days_absent', 'conduct', 'promoted_to', 'general_remarks'
        ]
        
        for field in significant_fields:
            if field in validated_data and getattr(instance, field) != validated_data[field]:
                return True
        
        return False
    
    def update(self, instance, validated_data):
        """Override update to detect score changes"""
        course_results_data = validated_data.pop('course_results', [])
        user_email = self.context['request'].user.email  # Get user's email
        
        # Check if course results are being updated
        scores_changed = bool(course_results_data)
        
        # CRITICAL FIX: Handle promoted_to before updating
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
        
        # Check if there are significant changes that require PDF regeneration
        has_significant_changes = self._has_significant_changes(validated_data, instance)
        has_course_changes = len(course_results_data) > 0
        
        # Log changes to main result fields
        for field in ['student', 'class_name', 'term', 'status', 'promoted_to', 'days_present', 
                     'days_absent', 'conduct', 'general_remarks']:
            if field in validated_data and getattr(instance, field) != validated_data[field]:
                self._create_log_entry(
                    instance, 
                    user_email,  # Pass the email string
                    field, 
                    getattr(instance, field), 
                    validated_data[field]
                )
        
        # Check if status is changing to PUBLISHED
        if validated_data.get('status') == 'PUBLISHED' and instance.status != 'PUBLISHED':
            validated_data['published_date'] = timezone.now()
            self._create_log_entry(
                instance,
                user_email,
                'published_date',
                instance.published_date,
                validated_data['published_date']
            )
        
        # Update the Result instance
        for key, value in validated_data.items():
            setattr(instance, key, value)
        instance.save()
        
        # Create a dictionary of existing course results for easy lookup
        existing_course_results = {cr.class_course_id: cr for cr in instance.course_results.all()}
        
        # Track if any course results were modified
        course_results_modified = False
        
        # Update or create course results
        for course_result_data in course_results_data:
            class_course = course_result_data.get('class_course')
            
            # Try to find existing course result
            if class_course.id in existing_course_results:
                course_result = existing_course_results[class_course.id]
                course_name = course_result.class_course.course.name
                
                # Check if course result data has changed
                course_data_changed = False
                for field, value in course_result_data.items():
                    if field != 'class_course' and getattr(course_result, field) != value:
                        course_data_changed = True
                        scores_changed = True  # Mark scores as changed
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
                    # Update course result
                    for key, value in course_result_data.items():
                        setattr(course_result, key, value)
                    course_result.save()
            else:
                # Create new course result
                course_results_modified = True
                scores_changed = True  # Mark scores as changed
                new_course_result = CourseResult.objects.create(result=instance, **course_result_data)
                course_name = new_course_result.class_course.course.name
                
                # Log creation of new course result
                self._create_log_entry(
                    instance,
                    user_email,
                    f"{course_name}",
                    "Not present",
                    f"Added with scores: {new_course_result.class_score}/{new_course_result.exam_score}"
                )
        
        # Check for deleted course results
        for class_course_id, course_result in existing_course_results.items():
            if class_course_id not in [cr_data.get('class_course').id for cr_data in course_results_data]:
                course_results_modified = True
                scores_changed = True  # Mark scores as changed
                course_name = course_result.class_course.course.name
                self._create_log_entry(
                    instance,
                    user_email,
                    f"{course_name}",
                    f"Scores: {course_result.class_score}/{course_result.exam_score}",
                    "Removed"
                )
                course_result.delete()
        
        # Set flag to indicate PDF should be regenerated if there were significant changes
        if has_significant_changes or course_results_modified:
            instance._regenerate_pdf = True
        
        # Set flag to indicate scores changed
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
    """
    Serializer for bulk updating result status for a class/term
    """
    class_name = serializers.CharField(required=True)
    term = serializers.CharField(required=True)
    status = serializers.ChoiceField(choices=['DRAFT', 'SCHEDULED', 'PUBLISHED'], required=True)
    scheduled_date = serializers.DateTimeField(required=False, allow_null=True)
    
    def validate(self, data):
        """
        Validate that scheduled_date is provided if status is SCHEDULED
        """
        status = data.get('status')
        scheduled_date = data.get('scheduled_date')
        
        if status == 'SCHEDULED':
            if not scheduled_date:
                raise serializers.ValidationError({'scheduled_date': 'Scheduled date is required when status is SCHEDULED'})
            
            # Convert string to datetime if necessary
            if isinstance(scheduled_date, str):
                try:
                    scheduled_date = datetime.fromisoformat(scheduled_date.replace('Z', '+00:00'))
                    scheduled_date = pytz.utc.localize(scheduled_date.replace(tzinfo=None))
                except ValueError:
                    raise serializers.ValidationError({'scheduled_date': 'Invalid date format. Use ISO format (YYYY-MM-DDTHH:MM:SSZ)'})
            
            # Ensure scheduled date is in the future
            if scheduled_date <= timezone.now():
                raise serializers.ValidationError({'scheduled_date': 'Scheduled date must be in the future'})
            
            data['scheduled_date'] = scheduled_date
        
        return data