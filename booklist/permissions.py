from rest_framework import permissions

class IsStaffOrPrincipal(permissions.BasePermission):
    """
    Custom permission to only allow staff or principal to perform certain actions.
    """
    def has_permission(self, request, view):
        return request.user and request.user.role in ['staff', 'principal']


class IsOwnerOrReadOnly(permissions.BasePermission):
    """
    Custom permission to only allow owners of an object to edit it.
    """
    def has_object_permission(self, request, view, obj):
        # Read permissions are allowed to any authenticated user
        if request.method in permissions.SAFE_METHODS:
            return True
        
        # Check if user is staff or principal
        if request.user.role in ['staff', 'principal']:
            return True
            
        # Otherwise, only allow if the object belongs to this user
        return obj.created_by == request.user