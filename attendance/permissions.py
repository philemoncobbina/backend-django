import logging

from rest_framework import permissions

logger = logging.getLogger(__name__)


class IsStaffOrPrincipal(permissions.BasePermission):
    """Only staff or principal can access."""
    def has_permission(self, request, view):
        return bool(request.user and request.user.role in ['staff', 'principal'])


class IsPrincipal(permissions.BasePermission):
    """Only principal can access."""
    def has_permission(self, request, view):
        return bool(request.user and request.user.role == 'principal')

    def has_object_permission(self, request, view, obj):
        return bool(request.user and request.user.role == 'principal')


class FinalizedRegisterOnlyPrincipal(permissions.BasePermission):
    """
    Staff can create, mark, and edit attendance registers that are not yet
    finalized. Once a register is finalized, only a principal can modify,
    reopen, or delete it — mirrors PublishedResultsOnlyPrincipal in
    ResultsEntry.
    """
    def has_permission(self, request, view):
        return bool(request.user and request.user.role in ['staff', 'principal'])

    def has_object_permission(self, request, view, obj):
        if request.method in permissions.SAFE_METHODS:
            return True

        register = obj if hasattr(obj, 'is_finalized') else getattr(obj, 'register', None)
        is_finalized = getattr(register, 'is_finalized', False)

        if is_finalized:
            is_allowed = bool(request.user and request.user.role == 'principal')
            if not is_allowed:
                logger.warning(
                    f"Permission denied: {request.user.username} ({request.user.role}) "
                    f"attempted to modify finalized register {getattr(register, 'id', None)}"
                )
            return is_allowed

        return bool(request.user and request.user.role in ['staff', 'principal'])


class IsStudentSelfOrStaffOrPrincipal(permissions.BasePermission):
    """A student may only view their own attendance; staff/principal see any student's."""
    def has_permission(self, request, view):
        return bool(request.user and request.user.is_authenticated)

    def has_object_permission(self, request, view, obj):
        if request.user.role in ['staff', 'principal']:
            return True
        student = getattr(obj, 'student', obj)
        return student == request.user