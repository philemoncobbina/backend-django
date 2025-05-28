from django.contrib import admin
from .models import Course, ClassCourse, Result, CourseResult, ResultChangeLog

class ClassCourseInline(admin.TabularInline):
    model = ClassCourse
    extra = 1

@admin.register(Course)
class CourseAdmin(admin.ModelAdmin):
    list_display = ('name', 'code', 'created_by', 'created_at')
    search_fields = ('name', 'code')
    list_filter = ('created_at',)
    inlines = [ClassCourseInline]

@admin.register(ClassCourse)
class ClassCourseAdmin(admin.ModelAdmin):
    list_display = ('course', 'class_name', 'term')
    list_filter = ('class_name', 'term')
    search_fields = ('course__name', 'course__code')

class CourseResultInline(admin.TabularInline):
    model = CourseResult
    extra = 1
    readonly_fields = ('total_score', 'grade')

class ResultChangeLogInline(admin.TabularInline):
    model = ResultChangeLog
    extra = 0
    readonly_fields = ('changed_by', 'changed_at', 'field_name', 'previous_value', 'new_value')
    can_delete = False
    
    def has_add_permission(self, request, obj=None):
        return False

@admin.register(Result)
class ResultAdmin(admin.ModelAdmin):
    list_display = ('student', 'class_name', 'term', 'status', 'scheduled_date', 'published_date')
    list_filter = ('class_name', 'term', 'status')
    search_fields = ('student__first_name', 'student__last_name', 'student__email')
    inlines = [CourseResultInline, ResultChangeLogInline]
    readonly_fields = ('published_date',)

@admin.register(CourseResult)
class CourseResultAdmin(admin.ModelAdmin):
    list_display = ('result', 'class_course', 'class_score', 'exam_score', 'total_score', 'grade')
    list_filter = ('result__class_name', 'result__term', 'class_course__course')
    search_fields = ('result__student__first_name', 'result__student__last_name', 'class_course__course__name')
    readonly_fields = ('total_score', 'grade')

@admin.register(ResultChangeLog)
class ResultChangeLogAdmin(admin.ModelAdmin):
    list_display = ('result', 'field_name', 'previous_value', 'new_value', 'changed_by', 'changed_at')
    list_filter = ('changed_at', 'field_name')
    search_fields = ('result__student__first_name', 'result__student__last_name', 'field_name')
    readonly_fields = ('result', 'changed_by', 'changed_at', 'field_name', 'previous_value', 'new_value')
    
    def has_add_permission(self, request):
        return False
    
    def has_change_permission(self, request, obj=None):
        return False