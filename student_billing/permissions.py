from rest_framework import permissions


class StaffOrAdminPermission(permissions.BasePermission):
    """
    Allows access only to authenticated non-student users (staff or admin).
    Used on all write operations that students must not perform.
    """

    def has_permission(self, request, view):
        return (
            request.user
            and request.user.is_authenticated
            and request.user.role != 'student'
        )


class StudentOnlyPermission(permissions.BasePermission):
    """
    Allows access only to authenticated students.
    Used on student-facing bill and payment-request endpoints.
    """

    def has_permission(self, request, view):
        return (
            request.user
            and request.user.is_authenticated
            and request.user.role == 'student'
        )


class IsAuthenticatedReadStaffWrite(permissions.BasePermission):
    """
    Safe methods (GET, HEAD, OPTIONS) → any authenticated user.
    Unsafe methods (POST, PUT, PATCH, DELETE) → staff/admin only.

    Used on BillingTemplateListCreateView and BillingItemListCreateView so that
    students cannot create or modify billing templates or items.
    """

    def has_permission(self, request, view):
        if not request.user or not request.user.is_authenticated:
            return False
        if request.method in permissions.SAFE_METHODS:
            return True
        return request.user.role != 'student'