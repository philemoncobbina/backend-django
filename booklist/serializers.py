# serializers.py
from rest_framework import serializers
from .models import BookList, BookListItem, StudentClassHistory
import django.utils.timezone as timezone    

class BookListItemSerializer(serializers.ModelSerializer):
    total_price = serializers.DecimalField(max_digits=10, decimal_places=2, read_only=True, source='total_item_price')
    
    class Meta:
        model = BookListItem
        fields = ['id', 'name', 'description', 'price', 'quantity', 'is_required', 'order', 'total_price']

class BookListSerializer(serializers.ModelSerializer):
    items = BookListItemSerializer(many=True, read_only=True)
    class_name_display = serializers.CharField(source='get_class_name_display', read_only=True)
    status_display = serializers.CharField(source='get_status_display', read_only=True)
    calculated_total_price = serializers.DecimalField(max_digits=10, decimal_places=2, read_only=True, source='total_price')
    created_by_name = serializers.SerializerMethodField()
    
    class Meta:
        model = BookList
        fields = [
            'id', 'title', 'academic_year', 'class_name', 
            'class_name_display', 'status', 'status_display', 'created_by', 
            'created_by_name', 'created_at', 'updated_at', 'publish_date',
            'description', 'calculated_total_price', 'items', 'scheduled_date'
        ]
        read_only_fields = ['created_by', 'created_at', 'updated_at', 'publish_date']
    
    def get_created_by_name(self, obj):
        return f"{obj.created_by.first_name} {obj.created_by.last_name}".strip() or obj.created_by.username
    
    def create(self, validated_data):
        # Set the current user as the creator
        validated_data['created_by'] = self.context['request'].user
        
        # If status is being set to published, ensure publish_date is set
        if validated_data.get('status') == 'published' and not validated_data.get('publish_date'):
            validated_data['publish_date'] = timezone.now()
        
        return super().create(validated_data)
    
    def update(self, instance, validated_data):
        # If status is being changed to published and publish_date is not set
        if (validated_data.get('status') == 'published' and 
            instance.status != 'published' and 
            not instance.publish_date):
            validated_data['publish_date'] = timezone.now()
        
        return super().update(instance, validated_data)

class BookListDetailSerializer(BookListSerializer):
    items = BookListItemSerializer(many=True)
    
    def create(self, validated_data):
        items_data = validated_data.pop('items', [])
        
        # Set the current user as the creator
        validated_data['created_by'] = self.context['request'].user
        
        # If status is being set to published, ensure publish_date is set
        if validated_data.get('status') == 'published' and not validated_data.get('publish_date'):
            validated_data['publish_date'] = timezone.now()
        
        book_list = BookList.objects.create(**validated_data)
        
        # Create all book list items
        for item_data in items_data:
            BookListItem.objects.create(book_list=book_list, **item_data)
            
        return book_list
    
    def update(self, instance, validated_data):
        items_data = validated_data.pop('items', None)
        
        # Check if status is being changed to published
        if (validated_data.get('status') == 'published' and 
            instance.status != 'published' and 
            not instance.publish_date):
            validated_data['publish_date'] = timezone.now()
        
        # Update the book list instance
        for attr, value in validated_data.items():
            setattr(instance, attr, value)
        instance.save()
        
        # If items were provided, update them
        if items_data is not None:
            # Delete existing items and create new ones
            instance.items.all().delete()
            for item_data in items_data:
                BookListItem.objects.create(book_list=instance, **item_data)
                
        return instance

class StudentBookListSerializer(serializers.ModelSerializer):
    """Simplified serializer for student view of book lists"""
    items = BookListItemSerializer(many=True, read_only=True)
    class_name_display = serializers.CharField(source='get_class_name_display', read_only=True)
    calculated_total_price = serializers.DecimalField(max_digits=10, decimal_places=2, read_only=True, source='total_price')
    
    class Meta:
        model = BookList
        fields = [
            'id', 'title', 'publish_date', 'academic_year', 'class_name', 'class_name_display',
            'description', 'calculated_total_price', 'items'
        ]

class StudentClassHistorySerializer(serializers.ModelSerializer):
    class_name_display = serializers.CharField(source='get_class_name_display', read_only=True)
    
    class Meta:
        model = StudentClassHistory
        fields = ['id', 'academic_year', 'class_name', 'class_name_display']