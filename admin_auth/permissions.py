from rest_framework import permissions

class IsPrincipalOrSuperuser(permissions.BasePermission):
    """
    Custom permission to only allow access to principals or superusers.
    """
    def has_permission(self, request, view):
        # Check if the user is authenticated
        if not request.user or not request.user.is_authenticated:
            return False

        # Allow access only if the user is a superuser or has the 'principal' role
        return request.user.is_superuser or request.user.role == 'principal'
