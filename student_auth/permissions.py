from rest_framework import permissions

class IsTeacherOrPrincipalOrSuperuser(permissions.BasePermission):
    """
    Custom permission to only allow principals, staff, or superusers to access the view.
    """
    def has_permission(self, request, view):
        # Check if the user is authenticated
        if not request.user.is_authenticated:
            return False
        
        # Allow access to superusers, principals, and staff
        return (request.user.is_superuser or 
                request.user.role in ['principal', 'staff'])