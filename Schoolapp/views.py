from rest_framework import viewsets, status
from rest_framework.response import Response
from .models import Contact, ContactLog
from .serializers import ContactSerializer, ContactLogSerializer
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.decorators import action
from django.forms.models import model_to_dict  # Import to convert model instances to dictionaries

class ContactViewSet(viewsets.ModelViewSet):
    queryset = Contact.objects.all()
    serializer_class = ContactSerializer

    def create(self, request):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        contact = serializer.save()

        return Response({'detail': 'Your message has been submitted successfully!', 'data': serializer.data}, status=status.HTTP_201_CREATED)

    def update(self, request, pk=None):
        contact = self.get_object()
        original_data = model_to_dict(contact)  # Convert model instance to dict to capture original data

        serializer = self.get_serializer(contact, data=request.data, partial=False)
        serializer.is_valid(raise_exception=True)
        updated_contact = serializer.save()

        # Log changes
        self.log_changes(original_data, updated_contact, request.user)

        return Response(serializer.data)

    def partial_update(self, request, pk=None):
        contact = self.get_object()
        original_data = model_to_dict(contact)  # Convert model instance to dict to capture original data

        serializer = self.get_serializer(contact, data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        updated_contact = serializer.save()

        # Log changes
        self.log_changes(original_data, updated_contact, request.user)

        return Response(serializer.data)

    def log_changes(self, original_data, updated_data, user):
        """
        Log the changes made to the contact inquiry.
        """
        changed_fields = self.get_changed_fields(original_data, updated_data)
        if changed_fields:
            ContactLog.objects.create(
                contact=updated_data,
                user=user,
                user_email=user.email if user else "Anonymous",
                changed_fields=changed_fields
            )

    def get_changed_fields(self, original_data, updated_data):
        """
        Compare the original and updated data and return the changed fields.
        """
        changed_fields = {}

        # Loop through the original data (dictionary)
        for key, original_value in original_data.items():
            # Get the new value from the updated instance using attribute access
            updated_value = getattr(updated_data, key, None)

            if original_value != updated_value:
                changed_fields[key] = {
                    'old_value': original_value,
                    'new_value': updated_value
                }

        return changed_fields

    def get_permissions(self):
        if self.action in ['update', 'partial_update']:
            self.permission_classes = [IsAuthenticated]
        else:
            self.permission_classes = [AllowAny]
        return super().get_permissions()


class ContactLogViewSet(viewsets.ReadOnlyModelViewSet):
    serializer_class = ContactLogSerializer

    def get_queryset(self):
        # Get the contact ID from the URL
        contact_id = self.kwargs['contact_id']
        # Return only logs for the specific contact
        return ContactLog.objects.filter(contact__id=contact_id)

    # Custom action to get logs for a contact
    @action(detail=True, methods=['get'])
    def logs(self, request, pk=None):
        logs = self.get_queryset()
        serializer = self.get_serializer(logs, many=True)
        return Response(serializer.data)
