import logging
from datetime import timedelta

from django.db import transaction
from django.shortcuts import get_object_or_404
from django_filters.rest_framework import DjangoFilterBackend, FilterSet, CharFilter
from rest_framework import viewsets, permissions, status
from rest_framework.decorators import action
from rest_framework.response import Response
from rest_framework.views import APIView

from authapp.models import CustomUser
from .models import (
    AcademicTerm,
    Holiday,
    ClassAttendanceRegister,
    AttendanceRecord,
    AttendanceChangeLog,
    describe_changes,
)
from .permissions import (
    IsStaffOrPrincipal,
    IsPrincipal,
    FinalizedRegisterOnlyPrincipal,
)
from .serializers import (
    AcademicTermSerializer,
    HolidaySerializer,
    ClassAttendanceRegisterSerializer,
    ClassAttendanceRegisterDetailSerializer,
    AttendanceRecordSerializer,
    MarkAttendanceSerializer,
    StudentMiniSerializer,
    AttendanceChangeLogSerializer,
)
from .tasks import notify_absent_students_after_finalize
from .cache_utils import (
    CACHE_TTL_LONG,
    CACHE_TTL_MEDIUM,
    CACHE_TTL_SHORT,
    CLASS_CHOICES_CACHE_KEY,
    term_calendar_key,
    roster_key,
    summary_key,
    invalidate_roster,
    invalidate_summary,
    invalidate_summaries,
    safe_cache_get,
    safe_cache_set,
    get_or_compute_locked,
)

logger = logging.getLogger(__name__)


class AcademicTermViewSet(viewsets.ModelViewSet):
    """
    Staff and principal can view every term.

    Only a principal can create, edit, or delete a term.
    The calendar is structural data, so it is locked down.
    """

    queryset = AcademicTerm.objects.all().prefetch_related("holidays")
    serializer_class = AcademicTermSerializer
    permission_classes = [permissions.IsAuthenticated, IsStaffOrPrincipal]

    def get_queryset(self):
        qs = super().get_queryset()

        academic_year = self.request.query_params.get("academic_year")
        term = self.request.query_params.get("term")
        is_current = self.request.query_params.get("is_current")

        if academic_year:
            qs = qs.filter(academic_year=academic_year)

        if term:
            qs = qs.filter(term=term)

        if is_current is not None:
            if str(is_current).lower() in ["true", "1", "yes"]:
                qs = qs.filter(is_current=True)
            elif str(is_current).lower() in ["false", "0", "no"]:
                qs = qs.filter(is_current=False)

        return qs

    def get_permissions(self):
        if self.action in ["create", "update", "partial_update", "destroy"]:
            return [permissions.IsAuthenticated(), IsPrincipal()]

        return [permissions.IsAuthenticated(), IsStaffOrPrincipal()]

    @action(detail=True, methods=["get"])
    def calendar(self, request, pk=None):
        """
        GET /attendance/terms/<id>/calendar/

        Returns every date in the term, flagged as:
        - school day
        - weekend
        - holiday

        Cached per term: this is a day-by-day loop over the whole term,
        and the result only changes when the term's calendar-affecting
        fields or its holidays change (both invalidate this cache).

        FIX (stampede protection): rebuilt through get_or_compute_locked()
        instead of a bare get/compute/set. When this cache entry expires,
        only the first request that misses rebuilds it; concurrent misses
        wait briefly and reuse that result instead of every one of them
        looping over the whole term simultaneously.
        """
        term = self.get_object()
        cache_key = term_calendar_key(term.pk)

        def _build_calendar():
            days = []
            current = term.start_date
            while current <= term.end_date:
                days.append({
                    "date": current,
                    "is_school_day": term.is_school_day(current),
                    "is_weekend": current.weekday() >= 5,
                    "is_holiday": current in term.holiday_dates,
                })
                current += timedelta(days=1)

            return {
                "term": term.id,
                "academic_year": term.academic_year,
                "term_name": term.term,
                "term_display": term.get_term_display(),
                "total_school_days": term.total_school_days,
                "days": days,
            }

        response_data = get_or_compute_locked(cache_key, _build_calendar, CACHE_TTL_MEDIUM)
        return Response(response_data)


class HolidayViewSet(viewsets.ModelViewSet):
    queryset = Holiday.objects.select_related("term").all()
    serializer_class = HolidaySerializer
    permission_classes = [permissions.IsAuthenticated, IsStaffOrPrincipal]

    def get_queryset(self):
        qs = super().get_queryset()

        term_id = self.request.query_params.get("term")

        if term_id:
            qs = qs.filter(term_id=term_id)

        return qs


class RegisterFilter(FilterSet):
    class_name = CharFilter(field_name="class_name")
    term = CharFilter(field_name="term")

    class Meta:
        model = ClassAttendanceRegister
        fields = ["class_name", "term", "date", "is_finalized"]


class ClassAttendanceRegisterViewSet(viewsets.ModelViewSet):
    queryset = ClassAttendanceRegister.objects.select_related(
        "term",
        "marked_by",
    ).prefetch_related(
        "records__student",
        "logs",
    )

    permission_classes = [permissions.IsAuthenticated, FinalizedRegisterOnlyPrincipal]
    filter_backends = [DjangoFilterBackend]
    filterset_class = RegisterFilter

    def get_serializer_class(self):
        if self.action == "retrieve":
            return ClassAttendanceRegisterDetailSerializer

        return ClassAttendanceRegisterSerializer

    def perform_create(self, serializer):
        register = serializer.save(marked_by=self.request.user)

        AttendanceChangeLog.objects.create(
            register=register,
            record=None,
            student=None,
            user=self.request.user,
            user_email=self.request.user.email,
            action_type="CREATE",
            changed_fields=f"register opened for {register.class_name} on {register.date}",
        )

    @action(detail=False, methods=["get"])
    def roster(self, request):
        """
        GET /attendance/registers/roster/?class_name=Class+3&term=1&date=2026-08-03

        Returns the class roster together with each student's existing mark
        for that day, if a register already exists.

        Cached per (class_name, term, date) with a short TTL, since this
        endpoint is hit on every attendance-taking page load. Invalidated
        by mark/finalize/reopen and by direct AttendanceRecord edits.

        FIX (stampede protection): this is the endpoint the original
        review called out by name — "hit on every attendance-taking page
        load" — as the one most exposed to a thundering herd when its
        5-minute TTL expires. It now goes through get_or_compute_locked(),
        so only one request rebuilds a given (class, term, date) roster at
        a time; a whole staffroom refreshing at once no longer means N
        simultaneous identical database hits.
        """
        class_name = request.query_params.get("class_name")
        term_id = request.query_params.get("term")
        date = request.query_params.get("date")

        if not (class_name and term_id and date):
            return Response(
                {"detail": "class_name, term and date query params are all required."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        term = get_object_or_404(AcademicTerm, pk=term_id)

        if not term.is_school_day(date):
            return Response(
                {"detail": "This date is not a valid school day for the selected term."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        cache_key = roster_key(class_name, term_id, date)

        def _build_roster():
            students = CustomUser.objects.filter(
                role="student",
                class_name=class_name,
                is_active=True,
            ).order_by("last_name", "first_name", "id")

            register = (
                ClassAttendanceRegister.objects.filter(
                    class_name=class_name,
                    term=term,
                    date=date,
                )
                .prefetch_related("records")
                .first()
            )

            existing = {
                record.student_id: record
                for record in register.records.all()
            } if register else {}

            roster_rows = []
            for student in students:
                record = existing.get(student.id)

                roster_rows.append({
                    "student": StudentMiniSerializer(student).data,
                    "status": record.status if record else None,
                    "absence_reason": record.absence_reason if record else None,
                })

            return {
                "class_name": class_name,
                "term": term.id,
                "date": date,
                "register": register.id if register else None,
                "is_finalized": register.is_finalized if register else False,
                "roster": roster_rows,
            }

        response_data = get_or_compute_locked(cache_key, _build_roster, CACHE_TTL_SHORT)
        return Response(response_data)

    @action(detail=False, methods=["post"])
    def mark(self, request):
        """
        POST /attendance/registers/mark/

        Creates the day's register if it does not exist yet, then upserts
        every student's mark in one transaction.

        Cache invalidation for roster/summary happens inside
        MarkAttendanceSerializer.save(), deferred via transaction.on_commit
        so it only fires once the write is durably committed.
        """
        serializer = MarkAttendanceSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        register = serializer.save(marked_by=request.user)

        return Response(
            ClassAttendanceRegisterDetailSerializer(register).data,
            status=status.HTTP_200_OK,
        )

    @action(detail=True, methods=["post"])
    def finalize(self, request, pk=None):
        """
        POST /attendance/registers/<id>/finalize/

        Finalizes the register and triggers email/SMS notifications to the
        primary guardians of all absent students.

        The notification task is queued only after the database transaction
        commits successfully. Cache invalidation is deferred the same way,
        so it's safe regardless of whether ATOMIC_REQUESTS wraps the whole
        view in an outer transaction.
        """
        register = self.get_object()

        if register.is_finalized:
            return Response(
                {
                    "detail": "This register is already finalized.",
                    "notification_queued": False,
                    "register": ClassAttendanceRegisterDetailSerializer(register).data,
                },
                status=status.HTTP_200_OK,
            )

        student_ids = list(
            register.records.values_list("student_id", flat=True)
        )

        with transaction.atomic():
            register.is_finalized = True
            register.save()

            AttendanceChangeLog.objects.create(
                register=register,
                record=None,
                student=None,
                user=request.user,
                user_email=request.user.email,
                action_type="FINALIZE",
                changed_fields="is_finalized: 'False' -> 'True'",
            )

            transaction.on_commit(
                lambda: notify_absent_students_after_finalize.delay(register.id)
            )
            transaction.on_commit(
                lambda: invalidate_roster(register.class_name, register.term_id, register.date)
            )
            transaction.on_commit(
                lambda: invalidate_summaries(student_ids, register.term_id)
            )

        logger.info(
            "Attendance register finalized and notification task queued. "
            "register_id=%s finalized_by=%s",
            register.id,
            request.user.email,
        )

        return Response({
            "detail": (
                "Register finalized successfully. "
                "Absent-student guardian notifications have been queued."
            ),
            "notification_queued": True,
            "register": ClassAttendanceRegisterDetailSerializer(register).data,
        })

    @action(detail=True, methods=["post"])
    def reopen(self, request, pk=None):
        """
        POST /attendance/registers/<id>/reopen/

        Only a principal can reopen a finalized register.
        Reopening does not send any notification.
        """
        if request.user.role != "principal":
            return Response(
                {"detail": "Only a principal can reopen a finalized register."},
                status=status.HTTP_403_FORBIDDEN,
            )

        register = self.get_object()

        if not register.is_finalized:
            return Response(
                {"detail": "This register is already open."},
                status=status.HTTP_200_OK,
            )

        student_ids = list(
            register.records.values_list("student_id", flat=True)
        )

        with transaction.atomic():
            register.is_finalized = False
            register.save()

            AttendanceChangeLog.objects.create(
                register=register,
                record=None,
                student=None,
                user=request.user,
                user_email=request.user.email,
                action_type="REOPEN",
                changed_fields="is_finalized: 'True' -> 'False'",
            )

            transaction.on_commit(
                lambda: invalidate_roster(register.class_name, register.term_id, register.date)
            )
            transaction.on_commit(
                lambda: invalidate_summaries(student_ids, register.term_id)
            )

        return Response(ClassAttendanceRegisterDetailSerializer(register).data)


class AttendanceRecordFilter(FilterSet):
    student = CharFilter(field_name="student")
    class_name = CharFilter(field_name="register__class_name")
    term = CharFilter(field_name="register__term")

    class Meta:
        model = AttendanceRecord
        fields = ["student", "class_name", "term", "status"]


class AttendanceRecordViewSet(viewsets.ModelViewSet):
    """
    Mostly used for corrections to an individual mark.

    Bulk marking should go through:
    POST /attendance/registers/mark/

    Every create, update, and delete writes an AttendanceChangeLog entry,
    and invalidates that student's roster/summary cache entries.
    """

    queryset = AttendanceRecord.objects.select_related(
        "register",
        "register__term",
        "student",
    )

    serializer_class = AttendanceRecordSerializer
    permission_classes = [permissions.IsAuthenticated, FinalizedRegisterOnlyPrincipal]
    filter_backends = [DjangoFilterBackend]
    filterset_class = AttendanceRecordFilter

    def _invalidate_for_record(self, register, student_id):
        invalidate_roster(register.class_name, register.term_id, register.date)
        invalidate_summary(student_id, register.term_id)

    def _invalidate_for_record_on_commit(self, register, student_id):
        transaction.on_commit(
            lambda: self._invalidate_for_record(register, student_id)
        )

    def perform_create(self, serializer):
        record = serializer.save()

        AttendanceChangeLog.objects.create(
            register=record.register,
            record=record,
            student=record.student,
            user=self.request.user,
            user_email=self.request.user.email,
            action_type="CREATE",
            changed_fields=describe_changes(
                {},
                {
                    "status": record.status,
                    "absence_reason": record.absence_reason,
                },
            ),
        )

        self._invalidate_for_record_on_commit(record.register, record.student_id)

    def perform_update(self, serializer):
        instance = serializer.instance

        old_values = {
            "status": instance.status,
            "absence_reason": instance.absence_reason,
        }

        record = serializer.save()

        new_values = {
            "status": record.status,
            "absence_reason": record.absence_reason,
        }

        action_type = (
            "STATUS_CHANGE"
            if old_values["status"] != new_values["status"]
            else "UPDATE"
        )

        if old_values != new_values:
            AttendanceChangeLog.objects.create(
                register=record.register,
                record=record,
                student=record.student,
                user=self.request.user,
                user_email=self.request.user.email,
                action_type=action_type,
                changed_fields=describe_changes(old_values, new_values),
            )

        self._invalidate_for_record_on_commit(record.register, record.student_id)

    def perform_destroy(self, instance):
        register = instance.register
        student_id = instance.student_id

        AttendanceChangeLog.objects.create(
            register=instance.register,
            record=None,
            student=instance.student,
            user=self.request.user,
            user_email=self.request.user.email,
            action_type="DELETE",
            changed_fields=describe_changes(
                {
                    "status": instance.status,
                    "absence_reason": instance.absence_reason,
                },
                {},
            ),
        )

        instance.delete()
        self._invalidate_for_record_on_commit(register, student_id)


class AttendanceChangeLogFilter(FilterSet):
    register = CharFilter(field_name="register")
    record = CharFilter(field_name="record")
    student = CharFilter(field_name="student")
    action_type = CharFilter(field_name="action_type")

    class Meta:
        model = AttendanceChangeLog
        fields = ["register", "record", "student", "action_type"]


class AttendanceChangeLogViewSet(viewsets.ReadOnlyModelViewSet):
    """
    Read-only audit trail.

    Logs are written by the views above, never edited directly.
    """

    queryset = AttendanceChangeLog.objects.select_related(
        "register",
        "record",
        "student",
        "user",
    )

    serializer_class = AttendanceChangeLogSerializer
    permission_classes = [permissions.IsAuthenticated, IsStaffOrPrincipal]
    filter_backends = [DjangoFilterBackend]
    filterset_class = AttendanceChangeLogFilter


class ClassChoicesView(APIView):
    """
    GET /attendance/classes/

    Returns the class choices from CustomUser.CLASS_CHOICES.
    Useful for the frontend class dropdown.

    Cached with a long TTL (24h) since this data almost never changes at
    runtime. Redis persists across app restarts, so whenever CLASS_CHOICES
    changes in code, run `python manage.py clear_class_choices_cache` as
    part of the deploy/release process (see
    attendance/cache_utils.invalidate_class_choices).

    Low request volume, so no stampede lock here — a lock is only worth
    the added complexity for hot, expensive-to-rebuild keys like the
    roster/summary/calendar endpoints above.
    """

    permission_classes = [permissions.IsAuthenticated, IsStaffOrPrincipal]

    def get(self, request):
        cached_response = safe_cache_get(CLASS_CHOICES_CACHE_KEY)

        if cached_response is not None:
            return Response(cached_response)

        classes = [
            {
                "value": value,
                "label": label,
            }
            for value, label in CustomUser.CLASS_CHOICES
        ]

        safe_cache_set(CLASS_CHOICES_CACHE_KEY, classes, CACHE_TTL_LONG)
        return Response(classes)


class StudentsByClassView(APIView):
    """
    GET /attendance/students-by-class/?class_name=Class+3

    Returns active students in the selected class.
    This prevents staff from manually typing student IDs.
    """

    permission_classes = [permissions.IsAuthenticated, IsStaffOrPrincipal]

    def get(self, request):
        class_name = request.query_params.get("class_name")

        if not class_name:
            return Response(
                {"detail": "class_name query param is required."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        valid_classes = [value for value, label in CustomUser.CLASS_CHOICES]

        if class_name not in valid_classes:
            return Response(
                {"detail": "Invalid class_name supplied."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        students = CustomUser.objects.filter(
            role="student",
            class_name=class_name,
            is_active=True,
        ).order_by("last_name", "first_name", "id")

        return Response(StudentMiniSerializer(students, many=True).data)


class StudentAttendanceSummaryView(APIView):
    """
    Staff or principal flow:

    GET /attendance/summary/?term=<id>&class_name=<class_name>&student=<student_id>

    Student flow:

    GET /attendance/summary/?term=<id>

    For staff/principal:
    - term is required
    - class_name is required
    - student is required

    For student:
    - term is required
    - the logged-in student is used automatically

    Returns:
    - student details
    - term details
    - summary totals
    - all attendance records taken for that student in that term

    Cached per (student, term) with a short TTL — this rebuilds the full
    attendance_history list on every call, which gets expensive as a term
    goes on. Invalidated by any write that touches this student's records
    (mark, individual record CRUD, finalize, reopen) and by any term/
    holiday edit that changes total_school_days.

    FIX (stampede protection): also goes through get_or_compute_locked(),
    since this rebuild (a full history walk) is the most expensive of the
    three cached endpoints and the one where a stampede would hurt most.
    """

    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        term_id = request.query_params.get("term")
        class_name = request.query_params.get("class_name")
        student_id = request.query_params.get("student")

        if not term_id:
            return Response(
                {"detail": "term query param is required."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        term = get_object_or_404(AcademicTerm, pk=term_id)

        if request.user.role == "student":
            student = request.user
            class_name = student.class_name
        else:
            if request.user.role not in ["staff", "principal"]:
                return Response(
                    {"detail": "You do not have permission to view attendance summaries."},
                    status=status.HTTP_403_FORBIDDEN,
                )

            if not class_name:
                return Response(
                    {"detail": "class_name query param is required."},
                    status=status.HTTP_400_BAD_REQUEST,
                )

            if not student_id:
                return Response(
                    {"detail": "student query param is required."},
                    status=status.HTTP_400_BAD_REQUEST,
                )

            valid_classes = [value for value, label in CustomUser.CLASS_CHOICES]

            if class_name not in valid_classes:
                return Response(
                    {"detail": "Invalid class_name supplied."},
                    status=status.HTTP_400_BAD_REQUEST,
                )

            student = get_object_or_404(
                CustomUser,
                pk=student_id,
                role="student",
                is_active=True,
            )

            if student.class_name != class_name:
                return Response(
                    {
                        "detail": (
                            "Selected student does not belong to the selected class."
                        )
                    },
                    status=status.HTTP_400_BAD_REQUEST,
                )

        cache_key = summary_key(student.id, term.id)

        def _build_summary():
            records = AttendanceRecord.objects.filter(
                student=student,
                register__term=term,
                register__class_name=student.class_name,
            ).select_related(
                "register",
                "register__term",
                "register__marked_by",
                "student",
            ).order_by("register__date", "id")

            days_present = records.filter(status="present").count()
            days_absent = records.filter(status="absent").count()
            total_marked = days_present + days_absent

            total_school_days = term.total_school_days
            days_unmarked = max(total_school_days - total_marked, 0)

            attendance_percentage = (
                round((days_present / total_school_days) * 100, 2)
                if total_school_days
                else 0.0
            )

            marked_attendance_percentage = (
                round((days_present / total_marked) * 100, 2)
                if total_marked
                else 0.0
            )

            attendance_history = []

            for record in records:
                register = record.register
                marked_by = register.marked_by

                attendance_history.append({
                    "record_id": record.id,
                    "register_id": register.id,
                    "date": register.date,
                    "class_name": register.class_name,
                    "status": record.status,
                    "status_display": record.get_status_display(),
                    "absence_reason": record.absence_reason,
                    "is_finalized": register.is_finalized,
                    "finalized_at": register.finalized_at,
                    "marked_by": marked_by.id if marked_by else None,
                    "marked_by_name": (
                        f"{marked_by.first_name} {marked_by.last_name}".strip()
                        if marked_by
                        else None
                    ),
                    "created_at": record.created_at,
                    "updated_at": record.updated_at,
                })

            return {
                "student": {
                    "id": student.id,
                    "index_number": getattr(student, "index_number", ""),
                    "first_name": student.first_name,
                    "last_name": student.last_name,
                    "full_name": (
                        f"{student.first_name} {student.last_name}".strip()
                        or student.username
                    ),
                    "class_name": student.class_name,
                },
                "term": {
                    "id": term.id,
                    "academic_year": term.academic_year,
                    "term": term.term,
                    "term_display": term.get_term_display(),
                    "start_date": term.start_date,
                    "end_date": term.end_date,
                    "total_school_days": total_school_days,
                },
                "summary": {
                    "total_school_days": total_school_days,
                    "total_marked": total_marked,
                    "days_present": days_present,
                    "days_absent": days_absent,
                    "days_unmarked": days_unmarked,
                    "attendance_percentage": attendance_percentage,
                    "marked_attendance_percentage": marked_attendance_percentage,
                },
                "attendance_history": attendance_history,
            }

        response_data = get_or_compute_locked(cache_key, _build_summary, CACHE_TTL_SHORT)
        return Response(response_data)