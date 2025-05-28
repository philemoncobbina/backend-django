from django.urls import path
from .views import ApplyToJobView, JobApplicationListView, JobApplicationDetailView , JobApplicationLogListView

urlpatterns = [
    path('job-applications/', JobApplicationListView.as_view(), name='job-application-list'),
    path('job-applications/apply/', ApplyToJobView.as_view(), name='job-application-create'),
    path('job-applications/<int:id>/', JobApplicationDetailView.as_view(), name='job-application-detail'),
    path('job-applications/<int:application_id>/logs/', JobApplicationLogListView.as_view(), name='job-application-logs'),
]