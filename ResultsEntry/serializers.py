from rest_framework import serializers
from django.utils import timezone
from datetime import datetime
import pytz
from .models import Course, ClassCourse, Result, CourseResult, ResultChangeLog
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
    
    class Meta:
        model = CourseResult
        fields = ['id', 'class_course', 'course_name', 'course_code', 'class_score', 'exam_score', 'total_score', 'grade', 'remarks']

class ResultSerializer(serializers.ModelSerializer):
    course_results = CourseResultSerializer(many=True, read_only=True)
    student_name = serializers.SerializerMethodField(read_only=True)
    term_display = serializers.CharField(source='get_term_display', read_only=True)
    
    class Meta:
        model = Result
        fields = '__all__'
    
    def get_student_name(self, obj):
        if obj.student:
            return f"{obj.student.first_name} {obj.student.last_name}".strip()
        return None
    
    def validate(self, data):
        """
        Validate that scheduled_date is provided and in the future if status is SCHEDULED
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

class ResultCreateSerializer(serializers.ModelSerializer):
    course_results = CourseResultSerializer(many=True)
    
    class Meta:
        model = Result
        fields = '__all__'
    
    def validate(self, data):
        """
        Validate that scheduled_date is provided and in the future if status is SCHEDULED
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
        
        # If status is PUBLISHED, set the published_date
        if status == 'PUBLISHED' and not self.instance:
            data['published_date'] = timezone.now()
        
        return data
    
    def create(self, validated_data):
        course_results_data = validated_data.pop('course_results', [])
        result = Result.objects.create(**validated_data)
        
        for course_result_data in course_results_data:
            CourseResult.objects.create(result=result, **course_result_data)
        
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
    
    def update(self, instance, validated_data):
        course_results_data = validated_data.pop('course_results', [])
        user_email = self.context['request'].user.email  # Get user's email
        
        # Log changes to main result fields
        for field in ['student', 'class_name', 'term', 'status']:
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
        
        # Update or create course results
        for course_result_data in course_results_data:
            class_course = course_result_data.get('class_course')
            
            # Try to find existing course result
            if class_course.id in existing_course_results:
                course_result = existing_course_results[class_course.id]
                course_name = course_result.class_course.course.name
                
                # Log changes to course result fields
                for field, value in course_result_data.items():
                    if field != 'class_course' and getattr(course_result, field) != value:
                        field_display = field.replace('_', ' ').title()
                        self._create_log_entry(
                            instance,
                            user_email,
                            f"{course_name} - {field_display}",
                            getattr(course_result, field),
                            value
                        )
                
                # Update course result
                for key, value in course_result_data.items():
                    setattr(course_result, key, value)
                course_result.save()
            else:
                # Create new course result
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
                course_name = course_result.class_course.course.name
                self._create_log_entry(
                    instance,
                    user_email,
                    f"{course_name}",
                    f"Scores: {course_result.class_score}/{course_result.exam_score}",
                    "Removed"
                )
                course_result.delete()
        
        return instance

class ResultChangeLogSerializer(serializers.ModelSerializer):
    class Meta:
        model = ResultChangeLog
        fields = ['id', 'result', 'changed_by', 'changed_at', 'field_name', 'previous_value', 'new_value']
        read_only_fields = fields

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