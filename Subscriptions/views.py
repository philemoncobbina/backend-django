from rest_framework import viewsets, status
from rest_framework.response import Response
from .models import Subscription, EmailList  # Import the new model
from .serializers import SubscriptionSerializer, EmailListSerializer  
from django.conf import settings
from sib_api_v3_sdk import Configuration, ApiClient, SendSmtpEmail
from sib_api_v3_sdk.api.transactional_emails_api import TransactionalEmailsApi
from sib_api_v3_sdk.rest import ApiException

class SubscriptionViewSet(viewsets.ModelViewSet):
    queryset = Subscription.objects.all()
    serializer_class = SubscriptionSerializer

    def create(self, request):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        subscription = serializer.save()

        # Update EmailList
        self.update_email_list(subscription.email)

        # Send confirmation email
        self.send_confirmation_email(subscription)

        return Response({'detail': 'Subscription submitted successfully!', 'data': serializer.data}, status=status.HTTP_201_CREATED)

    def send_confirmation_email(self, subscription):
        configuration = Configuration()
        configuration.api_key['api-key'] = settings.BREVO_API_KEY
        
        api_instance = TransactionalEmailsApi(ApiClient(configuration))
        
        send_smtp_email = SendSmtpEmail(
            to=[{"email": subscription.email}],
            sender={"name": "Your Company", "email": settings.DEFAULT_FROM_EMAIL},
            subject="Your Subscription Confirmation",
            html_content=f"""
            <html>
            <body>
                <p>Dear {subscription.full_name},</p>
                <p>Thank you for subscribing! You will now receive updates and notifications.</p>
                <p>Best regards,<br>Your Company</p>
            </body>
            </html>
            """
        )

        try:
            api_response = api_instance.send_transac_email(send_smtp_email)
            print("Email sent successfully: %s\n" % api_response)
        except ApiException as e:
            print("Exception when sending email: %s\n" % e)

    def update_email_list(self, new_email, remove=False):
        email_list, created = EmailList.objects.get_or_create(id=1)
        current_emails = email_list.emails.split(';') if email_list.emails else []

        if remove:
            # Remove email if present
            if new_email in current_emails:
                current_emails.remove(new_email)
                email_list.emails = ';'.join(current_emails)
                email_list.save()
        else:
            # Add email if not present
            if new_email not in current_emails:
                current_emails.append(new_email)
                email_list.emails = ';'.join(current_emails)
                email_list.save()

    def destroy(self, request, *args, **kwargs):
        instance = self.get_object()  # Get the specific Subscription instance
        email_to_remove = instance.email  # Store the email before deletion
        self.perform_destroy(instance)  # Completely delete the Subscription instance
        self.update_email_list(email_to_remove, remove=True)  # Remove email from EmailList if it exists
        return Response({'detail': 'Subscription and email removed successfully!'}, status=status.HTTP_204_NO_CONTENT)

class EmailListViewSet(viewsets.ModelViewSet):
    queryset = EmailList.objects.all()
    serializer_class = EmailListSerializer

    def get_queryset(self):
        # Return only the first instance (assuming there’s only one)
        return EmailList.objects.filter(id=1)

    def create(self, request):
        emails = request.data.get('emails', '')
        email_list = self.get_queryset().first()

        if email_list is None:
            return Response({'detail': 'Email list does not exist.'}, status=status.HTTP_404_NOT_FOUND)

        # Split the incoming emails and update the email list
        new_emails = emails.split(';')
        updated = False  # Flag to check if any email was added
        duplicates = []  # Track any duplicate emails

        for email in new_emails:
            email = email.strip()
            if not email:
                continue
            if self.is_duplicate_email(email, email_list):
                duplicates.append(email)
            else:
                updated = self.update_email_list(email, email_list) or updated

        # If duplicates were found, return an error message
        if duplicates:
            return Response(
                {'detail': f'These emails already exist: {", ".join(duplicates)}'},
                status=status.HTTP_400_BAD_REQUEST
            )

        # If the list was updated with new emails
        if updated:
            email_list.save()
            return Response({'detail': 'Email list updated successfully!'}, status=status.HTTP_201_CREATED)
        
        # If no new emails were added and no duplicates were found
        return Response({'detail': 'No new emails added.'}, status=status.HTTP_200_OK)

    def update(self, request, *args, **kwargs):
        return self.create(request)  # Use the same logic as create for updating

    def update_email_list(self, new_email, email_list):
        # Append the new email if not already present
        current_emails = email_list.emails.split(';') if email_list.emails else []
        if new_email not in current_emails:
            current_emails.append(new_email)
            email_list.emails = ';'.join(current_emails)
            return True  # Indicates that a new email was added
        return False  # Indicates that no email was added

    def is_duplicate_email(self, email, email_list):
        current_emails = email_list.emails.split(';') if email_list.emails else []
        return email in current_emails
