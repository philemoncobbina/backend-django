from django.urls import path, include
from rest_framework.routers import DefaultRouter

from .views import (
    AcademicTermViewSet,
    HolidayViewSet,
    ClassAttendanceRegisterViewSet,
    AttendanceRecordViewSet,
    AttendanceChangeLogViewSet,
    StudentAttendanceSummaryView,
    ClassChoicesView,
    StudentsByClassView,
)

router = DefaultRouter()
router.register(r"terms", AcademicTermViewSet, basename="academic-term")
router.register(r"holidays", HolidayViewSet, basename="holiday")
router.register(r"registers", ClassAttendanceRegisterViewSet, basename="attendance-register")
router.register(r"records", AttendanceRecordViewSet, basename="attendance-record")
router.register(r"logs", AttendanceChangeLogViewSet, basename="attendance-log")

urlpatterns = [
    path("classes/", ClassChoicesView.as_view(), name="attendance-classes"),
    path("students-by-class/", StudentsByClassView.as_view(), name="students-by-class"),
    path("summary/", StudentAttendanceSummaryView.as_view(), name="attendance-summary"),
    path("", include(router.urls)),
]