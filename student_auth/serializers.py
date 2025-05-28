from rest_framework import serializers
from authapp.models import CustomUser

class StudentUserSerializer(serializers.ModelSerializer):
    password = serializers.CharField(write_only=True, required=True, style={'input_type': 'password'})
    
    class Meta:
        model = CustomUser
        fields = ['email', 'username', 'first_name', 'last_name', 'password', 
                  'index_number', 'class_name']
        extra_kwargs = {
            'index_number': {'required': True},
            'class_name': {'required': True},
            'first_name': {'required': True},
            'last_name': {'required': True},
        }
    
    def validate(self, attrs):
        # Validate index number is unique
        index_number = attrs.get('index_number')
        if CustomUser.objects.filter(index_number=index_number).exists():
            raise serializers.ValidationError({"index_number": "A student with this index number already exists."})
        
        # Set role to student
        attrs['role'] = 'student'
        
        return attrs
    
    def create(self, validated_data):
        # Handle password hashing correctly using CustomUser's manager
        password = validated_data.pop('password')
        
        # Use the create_student method from CustomUserManager if using index_number
        index_number = validated_data.get('index_number')
        class_name = validated_data.get('class_name')
        email = validated_data.get('email')
        first_name = validated_data.get('first_name')
        last_name = validated_data.get('last_name')
        
        # Generate username from index number if not provided
        if not validated_data.get('username'):
            validated_data['username'] = index_number.lower()
        
        # Use the manager's create_student method
        user = CustomUser.objects.create_student(
            email=email,
            first_name=first_name,
            last_name=last_name,
            password=password,
            index_number=index_number,
            class_name=class_name,
            **{k: v for k, v in validated_data.items() if k not in ['email', 'first_name', 'last_name', 'index_number', 'class_name', 'username']}
        )
        
        return user