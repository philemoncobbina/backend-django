from rest_framework import serializers
from .models import BlogPost, Category, Author, BlogText
from authapp.models import CustomUser

class CategoryInputSerializer(serializers.Serializer):
    """Serializer for category input (name only)"""
    name = serializers.CharField(max_length=100, required=True)
    
    def validate_name(self, value):
        if not value.strip():
            raise serializers.ValidationError("Category name cannot be empty")
        return value.strip()

class AuthorInputSerializer(serializers.Serializer):
    """Serializer for author input"""
    name = serializers.CharField(max_length=200, required=True)
    bio = serializers.CharField(required=False, allow_blank=True, default="")
    profile_image = serializers.ImageField(required=False, allow_null=True)
    
    def validate_name(self, value):
        if not value.strip():
            raise serializers.ValidationError("Author name cannot be empty")
        return value.strip()

class CategorySerializer(serializers.ModelSerializer):
    """Serializer for category display"""
    created_by = serializers.StringRelatedField()
    
    class Meta:
        model = Category
        fields = ['id', 'name', 'created_by', 'created_at']
        read_only_fields = ['id', 'created_by', 'created_at']

class AuthorSerializer(serializers.ModelSerializer):
    """Serializer for author display"""
    created_by = serializers.StringRelatedField()
    
    class Meta:
        model = Author
        fields = ['id', 'name', 'bio', 'profile_image', 'created_by', 'created_at', 'updated_at']
        read_only_fields = ['id', 'created_by', 'created_at', 'updated_at']
    
    def to_representation(self, instance):
        data = super().to_representation(instance)
        request = self.context.get('request')
        if request and data.get('profile_image'):
            data['profile_image'] = request.build_absolute_uri(data['profile_image'])
        return data

class BlogTextSerializer(serializers.ModelSerializer):
    class Meta:
        model = BlogText
        fields = ['id', 'content', 'order', 'created_at', 'updated_at']
        read_only_fields = ['id', 'created_at', 'updated_at']

class BlogPostSerializer(serializers.ModelSerializer):
    categories = CategorySerializer(many=True, read_only=True)
    author = AuthorSerializer(read_only=True)
    text_blocks = BlogTextSerializer(many=True, read_only=True)
    created_by = serializers.StringRelatedField()
    
    # Input fields for creation/update
    author_data = serializers.JSONField(
        write_only=True, 
        required=True,
        help_text="Author details as JSON (name, bio)"
    )
    categories_data = serializers.JSONField(
        write_only=True, 
        required=True,
        help_text="List of category objects as JSON"
    )
    text_blocks_data = serializers.JSONField(
        write_only=True,
        required=True,
        help_text="List of text content blocks as JSON array"
    )
    
    class Meta:
        model = BlogPost
        fields = [
            'id', 'title', 'slug', 'image', 
            'categories', 'categories_data',
            'author', 'author_data',
            'text_blocks', 'text_blocks_data',
            'status', 'scheduled_date', 'published_date',
            'created_by', 'created_at', 'updated_at'
        ]
        read_only_fields = ['slug', 'published_date', 'created_by', 'created_at', 'updated_at']
    
    def to_representation(self, instance):
        data = super().to_representation(instance)
        request = self.context.get('request')
        if request and data.get('image'):
            data['image'] = request.build_absolute_uri(data['image'])
        return data
    
    def validate(self, data):
        # Validate required fields
        if 'author_data' not in data:
            raise serializers.ValidationError({"author_data": "Author data is required"})
        if 'categories_data' not in data:
            raise serializers.ValidationError({"categories_data": "At least one category is required"})
        if 'text_blocks_data' not in data or len(data['text_blocks_data']) == 0:
            raise serializers.ValidationError({"text_blocks_data": "At least one text block is required"})
        
        # Validate that author_data is a dict
        if not isinstance(data.get('author_data'), dict):
            raise serializers.ValidationError({"author_data": "Author data must be a JSON object"})
        
        # Validate that categories_data is a list
        if not isinstance(data.get('categories_data'), list):
            raise serializers.ValidationError({"categories_data": "Categories data must be a JSON array"})
        
        # Validate that text_blocks_data is a list
        if not isinstance(data.get('text_blocks_data'), list):
            raise serializers.ValidationError({"text_blocks_data": "Text blocks data must be a JSON array"})
        
        # Validate status and dates
        status = data.get('status', 'DRAFT')
        scheduled_date = data.get('scheduled_date')
        
        if status == 'SCHEDULED' and not scheduled_date:
            raise serializers.ValidationError({
                "scheduled_date": "Scheduled date is required for scheduled posts"
            })
        
        if status == 'PUBLISHED' and scheduled_date:
            raise serializers.ValidationError({
                "scheduled_date": "Published posts cannot have a scheduled date"
            })
        
        return data
    
    def create(self, validated_data):
        request = self.context.get('request')
        user = request.user if request and request.user.is_authenticated else None
        
        # Extract input data
        author_data = validated_data.pop('author_data')
        categories_data = validated_data.pop('categories_data')
        text_blocks_data = validated_data.pop('text_blocks_data')
        
        # Remove created_by from validated_data to avoid duplicate keyword argument
        validated_data.pop('created_by', None)
        
        # Handle published_date for immediate publishing
        if validated_data.get('status') == 'PUBLISHED' and not validated_data.get('published_date'):
            from django.utils import timezone
            validated_data['published_date'] = timezone.now()
        
        # Get profile image from request.FILES if present
        profile_image = None
        if request and 'author_profile_image' in request.FILES:
            profile_image = request.FILES['author_profile_image']
        
        # Handle author creation/retrieval
        author_name = author_data.get('name')
        
        # Try to get existing author (case-insensitive)
        try:
            author = Author.objects.get(name__iexact=author_name)
            # Update author details if provided
            if 'bio' in author_data and author_data['bio']:
                author.bio = author_data['bio']
            # Update profile image if provided
            if profile_image:
                author.profile_image = profile_image
            author.save()
        except Author.DoesNotExist:
            # Create new author
            author = Author.objects.create(
                name=author_name,
                bio=author_data.get('bio', ''),
                profile_image=profile_image,
                created_by=user
            )
        
        validated_data['author'] = author
        
        # Create the BlogPost instance - pass created_by separately
        blog_post = BlogPost.objects.create(
            created_by=user,
            **validated_data
        )
        
        # Handle categories - create if they don't exist
        for category_data in categories_data:
            category_name = category_data.get('name')
            if category_name:
                category, created = Category.objects.get_or_create(
                    name__iexact=category_name,
                    defaults={
                        'name': category_name,
                        'created_by': user
                    }
                )
                blog_post.categories.add(category)
        
        # Create text blocks with order
        for order, content in enumerate(text_blocks_data, start=1):
            BlogText.objects.create(
                blog_post=blog_post,
                content=content,
                order=order
            )
        
        return blog_post
    
    def update(self, instance, validated_data):
        request = self.context.get('request')
        user = request.user if request and request.user.is_authenticated else None
        
        # Extract input data
        author_data = validated_data.pop('author_data', None)
        categories_data = validated_data.pop('categories_data', None)
        text_blocks_data = validated_data.pop('text_blocks_data', None)
        
        # Get profile image from request.FILES if present
        profile_image = None
        if request and 'author_profile_image' in request.FILES:
            profile_image = request.FILES['author_profile_image']
        
        # Update author if provided
        if author_data:
            author_name = author_data.get('name')
            if author_name and author_name.lower() != instance.author.name.lower():
                # Check if author with this name exists
                try:
                    author = Author.objects.get(name__iexact=author_name)
                    # Update author details if provided
                    if 'bio' in author_data and author_data['bio'] is not None:
                        author.bio = author_data['bio']
                    # Update profile image if provided
                    if profile_image:
                        author.profile_image = profile_image
                    author.save()
                    instance.author = author
                except Author.DoesNotExist:
                    # Create new author
                    author = Author.objects.create(
                        name=author_name,
                        bio=author_data.get('bio', ''),
                        profile_image=profile_image,
                        created_by=user
                    )
                    instance.author = author
            elif instance.author:
                # Update existing author details
                if 'bio' in author_data and author_data['bio'] is not None:
                    instance.author.bio = author_data['bio']
                if profile_image:
                    instance.author.profile_image = profile_image
                instance.author.save()
        
        # Handle published_date for immediate publishing when status changes to PUBLISHED
        if validated_data.get('status') == 'PUBLISHED' and instance.status != 'PUBLISHED':
            from django.utils import timezone
            if not validated_data.get('published_date'):
                validated_data['published_date'] = timezone.now()
            # Clear scheduled_date for published posts
            validated_data['scheduled_date'] = None
        
        # Update blog post fields
        for attr, value in validated_data.items():
            setattr(instance, attr, value)
        
        # Save the instance to trigger status updates
        instance.save()
        
        # Update categories if provided
        if categories_data is not None:
            instance.categories.clear()
            for category_data in categories_data:
                category_name = category_data.get('name')
                if category_name:
                    category, created = Category.objects.get_or_create(
                        name__iexact=category_name,
                        defaults={
                            'name': category_name,
                            'created_by': user
                        }
                    )
                    instance.categories.add(category)
        
        # Update text blocks if provided
        if text_blocks_data is not None:
            # Delete existing text blocks and create new ones
            instance.text_blocks.all().delete()
            for order, content in enumerate(text_blocks_data, start=1):
                BlogText.objects.create(
                    blog_post=instance,
                    content=content,
                    order=order
                )
        
        return instance

class BlogListSerializer(serializers.ModelSerializer):
    categories = CategorySerializer(many=True, read_only=True)
    author = AuthorSerializer(read_only=True)
    image_url = serializers.SerializerMethodField()
    excerpt = serializers.SerializerMethodField()
    created_by = serializers.StringRelatedField()
    
    class Meta:
        model = BlogPost
        fields = [
            'id', 'title', 'slug', 'image', 'image_url', 'categories',
            'author', 'status', 'scheduled_date', 'published_date',
            'excerpt', 'created_by', 'created_at'
        ]
        read_only_fields = ['published_date']
    
    def get_image_url(self, obj):
        request = self.context.get('request')
        if obj.image and request:
            return request.build_absolute_uri(obj.image.url)
        return None
    
    def get_excerpt(self, obj):
        # Get the first text block or create an excerpt from the first one
        first_text = obj.text_blocks.first()
        if first_text:
            excerpt = first_text.content[:150]
            if len(first_text.content) > 150:
                excerpt += '...'
            return excerpt
        return ''