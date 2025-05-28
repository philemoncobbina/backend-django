from django.contrib import admin
from .models import BookList, BookListItem, StudentClassHistory  # Removed AcademicYear import

class BookListItemInline(admin.TabularInline):
    model = BookListItem
    extra = 1
    fields = ['name', 'description', 'price', 'quantity', 'is_required', 'order']

@admin.register(BookList)
class BookListAdmin(admin.ModelAdmin):
    list_display = ['title', 'class_name', 'academic_year', 'status', 'created_by', 'created_at', 'total_price']
    list_filter = ['status', 'class_name', 'academic_year']  # academic_year is now a CharField
    search_fields = ['title', 'description']
    readonly_fields = ['created_by', 'created_at', 'updated_at']
    inlines = [BookListItemInline]
    
    def save_model(self, request, obj, form, change):
        if not change:  # If creating a new object
            obj.created_by = request.user
        super().save_model(request, obj, form, change)
    
    def total_price(self, obj):
        return obj.total_price()
    total_price.short_description = 'Total Price'

@admin.register(BookListItem)
class BookListItemAdmin(admin.ModelAdmin):
    list_display = ['name', 'book_list', 'price', 'quantity', 'total_item_price', 'is_required']
    list_filter = ['is_required', 'book_list__class_name', 'book_list__academic_year']
    search_fields = ['name', 'description', 'book_list__title']
    
    def total_item_price(self, obj):
        return obj.total_item_price()
    total_item_price.short_description = 'Total Price'

@admin.register(StudentClassHistory)
class StudentClassHistoryAdmin(admin.ModelAdmin):
    list_display = ['student', 'academic_year', 'class_name']
    list_filter = ['academic_year', 'class_name']
    search_fields = ['student__username', 'academic_year']