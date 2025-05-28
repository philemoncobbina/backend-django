from rest_framework import permissions
import logging

logger = logging.getLogger(__name__)

class IsStaffOrPrincipal(permissions.BasePermission):
    """
    Custom permission to only allow staff or principal to access.
    """
    def has_permission(self, request, view):
        return request.user and request.user.role in ['staff', 'principal']

class IsOwnerOrReadOnly(permissions.BasePermission):
    """
    Custom permission to only allow owners of an object to edit it.
    """
    def has_object_permission(self, request, view, obj):
        # Read permissions are allowed for any request
        if request.method in permissions.SAFE_METHODS:
            return True
            
        # Write permissions are only allowed to the owner
        return obj.created_by == request.user

class IsOwnerOrStaffOrPrincipal(permissions.BasePermission):
    """
    Custom permission to allow owners, staff, or principal to access.
    """
    def has_object_permission(self, request, view, obj):
        # Allow if the user is the owner of the object
        if hasattr(obj, 'student') and obj.student == request.user:
            return True
            
        # Allow if the user is staff or principal
        return request.user and request.user.role in ['staff', 'principal']

class IsPrincipal(permissions.BasePermission):
    """
    Custom permission to only allow principal to access.
    """
    def has_permission(self, request, view):
        return request.user and request.user.role == 'principal'
        
    def has_object_permission(self, request, view, obj):
        return request.user and request.user.role == 'principal'

class PublishedResultsOnlyPrincipal(permissions.BasePermission):
    """
    Custom permission that only allows principals to modify or delete published results.
    Staff can modify or delete results that are not published.
    """
    def has_permission(self, request, view):
        # Allow principals always
        if request.user and request.user.role == 'principal':
            return True
        
        # For create actions, allow staff
        if view.action == 'create' and request.user and request.user.role == 'staff':
            return True
            
        # For other actions, permissions are checked at object level
        if request.user and request.user.role in ['staff', 'principal']:
            return True
            
        logger.error(f"Permission denied: User {request.user.username} with role {request.user.role} attempted to access {view.action} in {view.__class__.__name__}")
        return False
    
    def has_object_permission(self, request, view, obj):
        # Read permissions are allowed for any request with appropriate authentication
        if request.method in permissions.SAFE_METHODS:
            return True
            
        # If the result is published, only principal can edit or delete
        if hasattr(obj, 'status') and obj.status == 'PUBLISHED':
            is_allowed = request.user and request.user.role == 'principal'
            if not is_allowed:
                logger.error(f"Permission denied: User {request.user.username} with role {request.user.role} attempted to modify published result {obj.id}")
            return is_allowed
            
        # For non-published results, staff can also edit
        return request.user and request.user.role in ['staff', 'principal']