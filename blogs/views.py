from django.shortcuts import render
from rest_framework import viewsets, status, mixins
from rest_framework.decorators import action
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticatedOrReadOnly, IsAuthenticated, AllowAny
from rest_framework.pagination import PageNumberPagination
from rest_framework.parsers import MultiPartParser, FormParser, JSONParser
from rest_framework.views import APIView
from django_filters.rest_framework import DjangoFilterBackend
from .models import BlogPost, Category, Author, BlogText
from .serializers import (
    BlogPostSerializer, 
    BlogListSerializer,
    CategorySerializer,
    AuthorSerializer,
    BlogTextSerializer
)
from django.shortcuts import get_object_or_404
from django.db.models import Count, Prefetch, Q
from django.db import models as django_models
from django.utils import timezone

class StandardPagination(PageNumberPagination):
    page_size = 9
    page_size_query_param = 'page_size'
    max_page_size = 100

class BlogPostViewSet(viewsets.ModelViewSet):
    queryset = BlogPost.objects.all().select_related('author', 'created_by').prefetch_related(
        'categories',
        'text_blocks'
    )
    permission_classes = [IsAuthenticatedOrReadOnly]
    pagination_class = StandardPagination
    filter_backends = [DjangoFilterBackend]
    filterset_fields = ['categories__name', 'author__name', 'status']
    lookup_field = 'slug'
    parser_classes = [MultiPartParser, FormParser, JSONParser]
    
    def get_serializer_class(self):
        if self.action == 'list':
            return BlogListSerializer
        return BlogPostSerializer
    
    def get_serializer_context(self):
        context = super().get_serializer_context()
        context.update({"request": self.request})
        return context
    
    def get_queryset(self):
        queryset = super().get_queryset()
        
        # Check and update status for scheduled posts
        now = timezone.now()
        scheduled_posts = queryset.filter(status='SCHEDULED', scheduled_date__lte=now)
        for post in scheduled_posts:
            post.check_and_update_status()
        
        # Filter by category if provided
        category = self.request.query_params.get('category', None)
        if category:
            queryset = queryset.filter(categories__name__iexact=category)
        
        # Filter by author if provided
        author = self.request.query_params.get('author', None)
        if author:
            queryset = queryset.filter(author__name__icontains=author)
        
        # Search functionality
        search = self.request.query_params.get('search', None)
        if search:
            queryset = queryset.filter(
                Q(title__icontains=search) |
                Q(text_blocks__content__icontains=search) |
                Q(author__name__icontains=search) |
                Q(categories__name__icontains=search)
            ).distinct()
        
        # Filter by status
        status_filter = self.request.query_params.get('status', None)
        if status_filter:
            queryset = queryset.filter(status=status_filter)
        
        # Filter by date range for published_date
        start_date = self.request.query_params.get('start_date', None)
        end_date = self.request.query_params.get('end_date', None)
        
        if start_date:
            queryset = queryset.filter(published_date__gte=start_date)
        if end_date:
            queryset = queryset.filter(published_date__lte=end_date)
        
        # Filter by created_by if provided
        created_by = self.request.query_params.get('created_by', None)
        if created_by:
            queryset = queryset.filter(created_by__id=created_by)
        
        # Filter by author ID if provided
        author_id = self.request.query_params.get('author_id', None)
        if author_id:
            queryset = queryset.filter(author__id=author_id)
        
        # Show only published posts to non-authenticated users for list view
        if self.action == 'list' and not self.request.user.is_authenticated:
            queryset = queryset.filter(status='PUBLISHED')
        
        return queryset
    
    def retrieve(self, request, *args, **kwargs):
        """
        Override retrieve to allow public access to published posts
        while keeping draft/scheduled posts private
        """
        instance = self.get_object()
        
        # Allow public access if post is published
        if instance.status == 'PUBLISHED':
            serializer = self.get_serializer(instance)
            return Response(serializer.data)
        
        # For non-published posts (DRAFT/SCHEDULED), check authentication
        if not request.user.is_authenticated:
            # Return 404 instead of 401 to not reveal existence of non-published posts
            return Response(
                {'detail': 'Not found'},
                status=status.HTTP_404_NOT_FOUND
            )
        
        # Authenticated users can access all posts
        serializer = self.get_serializer(instance)
        return Response(serializer.data)
    
    def perform_create(self, serializer):
        # The serializer will handle setting created_by from request.user
        if self.request.user.is_authenticated:
            serializer.save(created_by=self.request.user)
        else:
            serializer.save()
    
    @action(detail=False, methods=['get'])
    def published(self, request):
        """Get only published blog posts"""
        queryset = self.get_queryset().filter(status='PUBLISHED')
        
        # Apply pagination
        page = self.paginate_queryset(queryset)
        if page is not None:
            serializer = BlogListSerializer(page, many=True, context={'request': request})
            return self.get_paginated_response(serializer.data)
        
        serializer = BlogListSerializer(queryset, many=True, context={'request': request})
        return Response(serializer.data)
    
    @action(detail=False, methods=['get'])
    def drafts(self, request):
        """Get draft blog posts (authenticated users only)"""
        if not request.user.is_authenticated:
            return Response(
                {'detail': 'Authentication required'},
                status=status.HTTP_401_UNAUTHORIZED
            )
        
        queryset = self.get_queryset().filter(status='DRAFT', created_by=request.user)
        
        # Apply pagination
        page = self.paginate_queryset(queryset)
        if page is not None:
            serializer = BlogListSerializer(page, many=True, context={'request': request})
            return self.get_paginated_response(serializer.data)
        
        serializer = BlogListSerializer(queryset, many=True, context={'request': request})
        return Response(serializer.data)
    
    @action(detail=False, methods=['get'])
    def scheduled(self, request):
        """Get scheduled blog posts (authenticated users only)"""
        if not request.user.is_authenticated:
            return Response(
                {'detail': 'Authentication required'},
                status=status.HTTP_401_UNAUTHORIZED
            )
        
        queryset = self.get_queryset().filter(status='SCHEDULED', created_by=request.user)
        
        # Apply pagination
        page = self.paginate_queryset(queryset)
        if page is not None:
            serializer = BlogListSerializer(page, many=True, context={'request': request})
            return self.get_paginated_response(serializer.data)
        
        serializer = BlogListSerializer(queryset, many=True, context={'request': request})
        return Response(serializer.data)
    
    @action(detail=False, methods=['get'])
    def recent(self, request):
        """Get recent published blog posts"""
        limit = request.query_params.get('limit', 3)
        try:
            limit = int(limit)
        except ValueError:
            limit = 3
        
        posts = self.get_queryset().filter(status='PUBLISHED').order_by('-published_date')[:limit]
        serializer = BlogListSerializer(posts, many=True, context={'request': request})
        return Response(serializer.data)
    
    @action(detail=False, methods=['get'])
    def by_category(self, request):
        """Get published blog posts grouped by category"""
        categories = Category.objects.annotate(
            post_count=Count('blog_posts', filter=Q(blog_posts__status='PUBLISHED'))
        ).filter(post_count__gt=0)
        
        result = []
        
        for category in categories:
            posts = BlogPost.objects.filter(categories=category, status='PUBLISHED').order_by('-published_date')[:5]
            serializer = BlogListSerializer(posts, many=True, context={'request': request})
            result.append({
                'category': {
                    'id': category.id,
                    'name': category.name,
                    'created_by': category.created_by.username if category.created_by else None
                },
                'posts': serializer.data
            })
        
        return Response(result)
    
    @action(detail=False, methods=['get'])
    def my_posts(self, request):
        """Get blog posts created by the current user"""
        if not request.user.is_authenticated:
            return Response(
                {'detail': 'Authentication required'},
                status=status.HTTP_401_UNAUTHORIZED
            )
        
        posts = self.get_queryset().filter(created_by=request.user)
        page = self.paginate_queryset(posts)
        if page is not None:
            serializer = BlogListSerializer(page, many=True, context={'request': request})
            return self.get_paginated_response(serializer.data)
        
        serializer = BlogListSerializer(posts, many=True, context={'request': request})
        return Response(serializer.data)
    
    @action(detail=False, methods=['get'])
    def categories(self, request):
        """Get all categories (for reference)"""
        categories = Category.objects.all().order_by('name')
        serializer = CategorySerializer(categories, many=True, context={'request': request})
        return Response(serializer.data)
    
    @action(detail=False, methods=['get'])
    def authors(self, request):
        """Get all authors (for reference)"""
        authors = Author.objects.all().order_by('name')
        serializer = AuthorSerializer(authors, many=True, context={'request': request})
        return Response(serializer.data)
    
    @action(detail=True, methods=['post'])
    def publish(self, request, slug=None):
        """Publish a draft or scheduled post"""
        blog_post = self.get_object()
        
        if blog_post.status == 'DRAFT':
            blog_post.status = 'PUBLISHED'
            blog_post.published_date = timezone.now()
            blog_post.scheduled_date = None
            blog_post.save()
            return Response({'status': 'Post published successfully'})
        
        elif blog_post.status == 'SCHEDULED':
            blog_post.status = 'PUBLISHED'
            blog_post.published_date = timezone.now()
            blog_post.save()
            return Response({'status': 'Scheduled post published now'})
        
        elif blog_post.status == 'PUBLISHED':
            return Response({'status': 'Post is already published'}, status=status.HTTP_400_BAD_REQUEST)
    
    @action(detail=True, methods=['post'])
    def schedule(self, request, slug=None):
        """Schedule a draft post for publishing"""
        blog_post = self.get_object()
        
        if blog_post.status != 'DRAFT':
            return Response(
                {'error': 'Only draft posts can be scheduled'},
                status=status.HTTP_400_BAD_REQUEST
            )
        
        scheduled_date = request.data.get('scheduled_date')
        if not scheduled_date:
            return Response(
                {'error': 'scheduled_date is required'},
                status=status.HTTP_400_BAD_REQUEST
            )
        
        try:
            from django.utils.dateparse import parse_datetime
            scheduled_datetime = parse_datetime(scheduled_date)
            if not scheduled_datetime:
                raise ValueError
            
            if scheduled_datetime <= timezone.now():
                return Response(
                    {'error': 'Scheduled date must be in the future'},
                    status=status.HTTP_400_BAD_REQUEST
                )
            
            blog_post.status = 'SCHEDULED'
            blog_post.scheduled_date = scheduled_datetime
            blog_post.save()
            
            return Response({'status': 'Post scheduled successfully'})
            
        except (ValueError, TypeError):
            return Response(
                {'error': 'Invalid date format. Use ISO format: YYYY-MM-DDTHH:MM:SS'},
                status=status.HTTP_400_BAD_REQUEST
            )
    
    @action(detail=True, methods=['post'])
    def add_category(self, request, slug=None):
        """Add a category to an existing blog post"""
        blog_post = self.get_object()
        category_name = request.data.get('name')
        
        if not category_name:
            return Response(
                {'error': 'category name is required'},
                status=status.HTTP_400_BAD_REQUEST
            )
        
        category, created = Category.objects.get_or_create(
            name__iexact=category_name,
            defaults={
                'name': category_name,
                'created_by': request.user if request.user.is_authenticated else None
            }
        )
        
        if category not in blog_post.categories.all():
            blog_post.categories.add(category)
        
        serializer = BlogPostSerializer(blog_post, context={'request': request})
        return Response(serializer.data)
    
    @action(detail=True, methods=['post'])
    def add_text_block(self, request, slug=None):
        """Add a text block to an existing blog post"""
        blog_post = self.get_object()
        content = request.data.get('content')
        
        if not content or not content.strip():
            return Response(
                {'error': 'content is required and cannot be empty'},
                status=status.HTTP_400_BAD_REQUEST
            )
        
        # Get the next order number
        last_order = blog_post.text_blocks.aggregate(last=django_models.Max('order'))['last'] or 0
        new_order = last_order + 1
        
        text_block = BlogText.objects.create(
            blog_post=blog_post,
            content=content.strip(),
            order=new_order
        )
        
        serializer = BlogTextSerializer(text_block)
        return Response(serializer.data, status=status.HTTP_201_CREATED)
    
    @action(detail=False, methods=['get'])
    def check_scheduled(self, request):
        """Check and update scheduled posts (can be called via cron job)"""
        now = timezone.now()
        scheduled_posts = BlogPost.objects.filter(
            status='SCHEDULED', 
            scheduled_date__lte=now
        )
        
        updated_count = 0
        for post in scheduled_posts:
            if post.check_and_update_status():
                updated_count += 1
        
        return Response({
            'updated_posts': updated_count,
            'message': f'Updated {updated_count} scheduled posts to published'
        })


class PublishedBlogPostViewSet(viewsets.ReadOnlyModelViewSet):
    """
    Read-only viewset for published posts only
    Allows public access to both list and detail views
    """
    permission_classes = [AllowAny]
    pagination_class = StandardPagination
    lookup_field = 'slug'
    
    def get_serializer_class(self):
        if self.action == 'list':
            return BlogListSerializer
        return BlogPostSerializer
    
    def get_serializer_context(self):
        context = super().get_serializer_context()
        context.update({"request": self.request})
        return context
    
    def get_queryset(self):
        queryset = BlogPost.objects.filter(
            status='PUBLISHED'
        ).select_related('author', 'created_by').prefetch_related(
            'categories',
            'text_blocks'
        )
        
        # Filter by category if provided
        category = self.request.query_params.get('category', None)
        if category:
            queryset = queryset.filter(categories__name__iexact=category)
        
        # Filter by author if provided
        author = self.request.query_params.get('author', None)
        if author:
            queryset = queryset.filter(author__name__icontains=author)
        
        # Search functionality
        search = self.request.query_params.get('search', None)
        if search:
            queryset = queryset.filter(
                Q(title__icontains=search) |
                Q(text_blocks__content__icontains=search) |
                Q(author__name__icontains=search) |
                Q(categories__name__icontains=search)
            ).distinct()
        
        # Filter by date range for published_date
        start_date = self.request.query_params.get('start_date', None)
        end_date = self.request.query_params.get('end_date', None)
        
        if start_date:
            queryset = queryset.filter(published_date__gte=start_date)
        if end_date:
            queryset = queryset.filter(published_date__lte=end_date)
        
        # Filter by author ID if provided
        author_id = self.request.query_params.get('author_id', None)
        if author_id:
            queryset = queryset.filter(author__id=author_id)
        
        # Filter by created_by if provided
        created_by = self.request.query_params.get('created_by', None)
        if created_by:
            queryset = queryset.filter(created_by__id=created_by)
        
        return queryset
    
    @action(detail=False, methods=['get'])
    def recent(self, request):
        """Get recent published blog posts"""
        limit = request.query_params.get('limit', 3)
        try:
            limit = int(limit)
        except ValueError:
            limit = 3
        
        posts = self.get_queryset().order_by('-published_date')[:limit]
        serializer = BlogListSerializer(posts, many=True, context={'request': request})
        return Response(serializer.data)
    
    @action(detail=False, methods=['get'])
    def by_category(self, request):
        """Get published blog posts grouped by category"""
        categories = Category.objects.annotate(
            post_count=Count('blog_posts', filter=Q(blog_posts__status='PUBLISHED'))
        ).filter(post_count__gt=0)
        
        result = []
        
        for category in categories:
            posts = BlogPost.objects.filter(categories=category, status='PUBLISHED').order_by('-published_date')[:5]
            serializer = BlogListSerializer(posts, many=True, context={'request': request})
            result.append({
                'category': {
                    'id': category.id,
                    'name': category.name,
                    'created_by': category.created_by.username if category.created_by else None
                },
                'posts': serializer.data
            })
        
        return Response(result)