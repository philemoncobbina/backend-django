from rest_framework import generics, status
from rest_framework.response import Response
from rest_framework.views import APIView
from rest_framework.permissions import IsAuthenticated, AllowAny
from rest_framework_simplejwt.tokens import RefreshToken
from django.contrib.auth import login
from django.contrib.auth.hashers import check_password
from django.urls import reverse
from django.shortcuts import get_object_or_404, redirect
from django.core.exceptions import ObjectDoesNotExist
import jwt
import logging

from authapp.models import CustomUser
from .serializers import StudentUserSerializer
from .permissions import IsTeacherOrPrincipalOrSuperuser
from sib_api_v3_sdk import Configuration, ApiClient, SendSmtpEmail
from sib_api_v3_sdk.api.transactional_emails_api import TransactionalEmailsApi
from sib_api_v3_sdk.rest import ApiException
from django.conf import settings

# Ensure that the is_active field is set to False by default when creating a new user
from django.db.models.signals import pre_save
from django.dispatch import receiver
from authapp.models import CustomUser

@receiver(pre_save, sender=CustomUser)
def set_is_active_to_false(sender, instance, **kwargs):
    if instance._state.adding:  # Check if the instance is being created
        instance.is_active = False


logger = logging.getLogger(__name__)

class StudentSignUpView(generics.CreateAPIView):
    permission_classes = [IsTeacherOrPrincipalOrSuperuser]  # Ensure the user has proper permissions
    serializer_class = StudentUserSerializer

    def create(self, request, *args, **kwargs):
        # Check if the logged-in user has appropriate permissions
        if not (request.user.is_superuser or request.user.role in ['principal', 'staff']):
            return Response({'error': 'Only principals, staff, or superusers can create student accounts.'}, 
                           status=status.HTTP_403_FORBIDDEN)

        # Create a mutable copy of the request data
        mutable_data = request.data.copy()

        email = mutable_data.get('email')
        index_number = mutable_data.get('index_number')
        class_name = mutable_data.get('class_name')

        # Validate required fields
        if not email or not index_number or not class_name:
            return Response({'error': 'Email, index number, and class name are required.'}, 
                           status=status.HTTP_400_BAD_REQUEST)

        # Check if the email has already been used
        if CustomUser.objects.filter(email=email).exists():
            return Response({'error': 'Email has already been used.'}, status=status.HTTP_400_BAD_REQUEST)

        # Set the role to student
        mutable_data['role'] = 'student'
        
        # If username not provided, use index_number as username
        if not mutable_data.get('username'):
            mutable_data['username'] = index_number.lower()

        # Pass the modified data to the serializer
        serializer = self.get_serializer(data=mutable_data)
        serializer.is_valid(raise_exception=True)
        self.perform_create(serializer)

        # Get the created user and send a verification email
        try:
            user = CustomUser.objects.get(email=email)
        except ObjectDoesNotExist:
            return Response({'error': 'Student creation failed.'}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

        # Call the method to send the verification email
        self.send_verification_email(user)

        # Customize the response
        headers = self.get_success_headers(serializer.data)
        return Response({'message': 'Student registration successful. Please check email for the verification link.'},
                        status=status.HTTP_201_CREATED, headers=headers)

    def send_verification_email(self, user):
        verification_token = RefreshToken.for_user(user).access_token
        verification_url = reverse('student-verify-email', kwargs={'user_id': user.id, 'token': str(verification_token)})
        verification_url = self.request.build_absolute_uri(verification_url)  # Make the URL absolute

        # Get the raw password from the request data before it's hashed
        raw_password = self.request.data.get('password')

        # Hardcoded professional email content with password included
        email_body = f"""
        <html>
        <body style="font-family: Arial, sans-serif; color: #333;">
            Dear {user.first_name},
            <h2 style="color: #4CAF50;">Welcome to Our School System!</h2>
            <p>Your student account has been created. Please click the button below to verify your email address:</p>
            
            <a href="{verification_url}" style="display: inline-block; padding: 10px 20px; background-color: #4CAF50; color: #fff; text-decoration: none; border-radius: 5px; font-weight: bold;">
                Verify Your Email
            </a>

            <p>Your account details:</p>
            <ul>
                <li>Username: {user.username}</li>
                <li>Email: {user.email}</li>
                <li>Index Number: {user.index_number}</li>
                <li>Class: {user.get_class_name_display()}</li>
                <li>Password: {raw_password}</li>
            </ul>

            <p>Please keep these credentials safe and secure. After logging in for the first time, we recommend changing your password.</p>
            <p>If you did not expect this email, please contact the school administration.</p>
            
            <br>
            <p>Best regards,<br>School Administration</p>
        </body>
        </html>
        """

        # Brevo (Sendinblue) email sending logic
        configuration = Configuration()
        configuration.api_key['api-key'] = settings.BREVO_API_KEY
        
        api_instance = TransactionalEmailsApi(ApiClient(configuration))
        
        send_smtp_email = SendSmtpEmail(
            to=[{"email": user.email}],
            sender={"name": "School Admin", "email": settings.DEFAULT_FROM_EMAIL},
            subject="Verify Your Student Account",
            html_content=email_body
        )

        try:
            api_response = api_instance.send_transac_email(send_smtp_email)
            logger.info(f"Verification email sent to student {user.email}: {api_response}")
        except ApiException as e:
            logger.error(f"Exception when sending email to student: {e}")


class StudentVerifyEmailView(APIView):
    def get(self, request, user_id, token):
        user = get_object_or_404(CustomUser, id=user_id)

        if user.is_active:
            return redirect('http://localhost:5173/student-dashboard')  # Redirect to student dashboard if already verified

        try:
            payload = jwt.decode(token, settings.SECRET_KEY, algorithms=['HS256'])
            user_id_from_token = payload['user_id']

            logger.debug(f"user_id: {user_id}, user_id_from_token: {user_id_from_token}")

            if str(user_id) != str(user_id_from_token):  # Ensure the types match and compare values
                return Response({'error': 'Invalid token for this user.'}, status=status.HTTP_400_BAD_REQUEST)

            # Activate the student account
            user.is_active = True
            user.save()

            return redirect('http://localhost:5173/student-dashboard')  # Redirect to student dashboard after verification

        except jwt.ExpiredSignatureError:
            logger.error("Activation link has expired.")
            return Response({'error': 'Activation link has expired.'}, status=status.HTTP_400_BAD_REQUEST)

        except jwt.InvalidTokenError:
            logger.error("Invalid activation link.")
            return Response({'error': 'Invalid activation link.'}, status=status.HTTP_400_BAD_REQUEST)


class StudentLoginView(APIView):
    permission_classes = [AllowAny]
    
    def post(self, request):
        email = request.data.get('email')
        password = request.data.get('password')
        
        # Alternatively, allow login with index number instead of email
        index_number = request.data.get('index_number')
        
        try:
            if email:
                user = CustomUser.objects.get(email=email)
            elif index_number:
                user = CustomUser.objects.get(index_number=index_number)
            else:
                return Response({'error': 'Please provide either an email or index number.'}, 
                              status=status.HTTP_400_BAD_REQUEST)
        except CustomUser.DoesNotExist:
            return Response({'error': 'Incorrect login credentials.'}, status=status.HTTP_401_UNAUTHORIZED)

        # Check if the user is a student
        if user.role != 'student':
            return Response({'error': 'This login is for students only.'}, status=status.HTTP_403_FORBIDDEN)

        # Check if the user is blocked
        if user.is_blocked:
            return Response({'error': 'Your account has been blocked. Please contact school administration for assistance.'}, 
                          status=status.HTTP_403_FORBIDDEN)

        # Check if the user is active
        if not user.is_active:
            return Response({'error': 'Account not verified. Please check your email for the verification link.'}, 
                          status=status.HTTP_401_UNAUTHORIZED)

        # Check if the password matches
        if check_password(password, user.password):
            login(request, user)
            refresh = RefreshToken.for_user(user)
            return Response({
                'access_token': str(refresh.access_token),
                'refresh_token': str(refresh),
                'user': {
                    'id': user.id,
                    'email': user.email,
                    'index_number': user.index_number,
                    'class_name': user.class_name,
                    'first_name': user.first_name,
                    'last_name': user.last_name,
                    'role': user.role
                }
            })
        else:
            return Response({'error': 'Incorrect login credentials.'}, status=status.HTTP_401_UNAUTHORIZED)


class BatchStudentCreationView(generics.CreateAPIView):
    """API view for creating multiple student accounts at once from a CSV or data list"""
    permission_classes = [IsTeacherOrPrincipalOrSuperuser]
    serializer_class = StudentUserSerializer
    
    def create(self, request, *args, **kwargs):
        # Check for appropriate permissions
        if not (request.user.is_superuser or request.user.role in ['principal', 'staff']):
            return Response({'error': 'Only principals, staff, or superusers can create student accounts.'}, 
                          status=status.HTTP_403_FORBIDDEN)
        
        students_data = request.data.get('students', [])
        if not students_data or not isinstance(students_data, list):
            return Response({'error': 'Please provide a list of student data.'}, 
                          status=status.HTTP_400_BAD_REQUEST)
        
        created_students = []
        errors = []
        
        for i, student_data in enumerate(students_data):
            # Set default username if not provided (using index number)
            if not student_data.get('username') and student_data.get('index_number'):
                student_data['username'] = student_data['index_number'].lower()
            
            # Set role to student
            student_data['role'] = 'student'
            
            serializer = self.get_serializer(data=student_data)
            if serializer.is_valid():
                try:
                    user = serializer.save()
                    # Send verification email for each student
                    self.send_verification_email(user)
                    created_students.append({
                        'index_number': user.index_number,
                        'email': user.email,
                        'class_name': user.class_name
                    })
                except Exception as e:
                    errors.append({'index': i, 'error': str(e), 'data': student_data})
            else:
                errors.append({'index': i, 'error': serializer.errors, 'data': student_data})
        
        return Response({
            'message': f'Successfully created {len(created_students)} student accounts.',
            'created_students': created_students,
            'errors': errors
        }, status=status.HTTP_201_CREATED if created_students else status.HTTP_400_BAD_REQUEST)
    
    def send_verification_email(self, user):
        verification_token = RefreshToken.for_user(user).access_token
        verification_url = reverse('student-verify-email', kwargs={'user_id': user.id, 'token': str(verification_token)})
        verification_url = self.request.build_absolute_uri(verification_url)  # Make the URL absolute

        # Hardcoded professional email content
        email_body = f"""
        <html>
        <body style="font-family: Arial, sans-serif; color: #333;">
            Dear {user.first_name},
            <h2 style="color: #4CAF50;">Welcome to Our School System!</h2>
            <p>Your student account has been created. Please click the button below to verify your email address:</p>
            
            <a href="{verification_url}" style="display: inline-block; padding: 10px 20px; background-color: #4CAF50; color: #fff; text-decoration: none; border-radius: 5px; font-weight: bold;">
                Verify Your Email
            </a>

            <p>Your account details:</p>
            <ul>
                <li>Index Number: {user.index_number}</li>
                <li>Class: {user.get_class_name_display()}</li>
            </ul>

            <p>If you did not expect this email, please contact the school administration.</p>
            
            <br>
            <p>Best regards,<br>School Administration</p>
        </body>
        </html>
        """

        # Brevo (Sendinblue) email sending logic
        configuration = Configuration()
        configuration.api_key['api-key'] = settings.BREVO_API_KEY
        
        api_instance = TransactionalEmailsApi(ApiClient(configuration))
        
        send_smtp_email = SendSmtpEmail(
            to=[{"email": user.email}],
            sender={"name": "School Admin", "email": settings.DEFAULT_FROM_EMAIL},
            subject="Verify Your Student Account",
            html_content=email_body
        )

        try:
            api_response = api_instance.send_transac_email(send_smtp_email)
            logger.info(f"Verification email sent to student {user.email}: {api_response}")
        except ApiException as e:
            logger.error(f"Exception when sending email to student: {e}")