from rest_framework import permissions

class IsPrincipalOrSuperuser(permissions.BasePermission):
    """
    Custom permission to only allow full access to principals or superusers.
    Staff users get read-only access.
    """
    def has_permission(self, request, view):
        # Check if the user is authenticated
        if not request.user or not request.user.is_authenticated:
            return False
        
        # Superusers have full access
        if request.user.is_superuser:
            return True
        
        # Staff users only have read access (GET requests)
        if request.user.role == 'staff':
            return request.method == 'GET'
        
        # Principals have full access (GET, PATCH, DELETE)
        if request.user.role == 'principal':
            return True
        
        # Deny access for any other roles
        return False

class IsReadOnlyOrPrincipal(permissions.BasePermission):
    """
    Alternative permission class that explicitly handles read-only vs write permissions.
    Staff: Read-only access
    Principal/Superuser: Full access
    """
    def has_permission(self, request, view):
        # Check if the user is authenticated
        if not request.user or not request.user.is_authenticated:
            return False
        
        # Define read-only methods
        SAFE_METHODS = ('GET', 'HEAD', 'OPTIONS')
        
        # Superusers and principals have full access
        if request.user.is_superuser or request.user.role == 'principal':
            return True
        
        # Staff users only have read-only access
        if request.user.role == 'staff':
            return request.method in SAFE_METHODS
        
        # Deny access for any other roles
        return False