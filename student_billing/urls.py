from django.urls import path
from . import views

app_name = 'billing'

urlpatterns = [
    # Billing Templates
    path('templates/', views.BillingTemplateListCreateView.as_view(), name='billing-template-list-create'),
    path('templates/<int:pk>/', views.BillingTemplateDetailView.as_view(), name='billing-template-detail'),
    
    # Billing Items
    path('items/', views.BillingItemListCreateView.as_view(), name='billing-item-list-create'),
    path('items/<int:pk>/', views.BillingItemDetailView.as_view(), name='billing-item-detail'),
    path('items/<int:billing_item_id>/logs/', views.BillingItemLogListView.as_view(), name='billing-item-logs'),
    
    # Payment Receipts
    path('payment-receipts/', views.PaymentReceiptListCreateView.as_view(), name='payment-receipt-list-create'),
    path('payment-receipts/<int:pk>/', views.PaymentReceiptDetailView.as_view(), name='payment-receipt-detail'),
    path('bills/<int:bill_id>/payment-receipts/', views.PaymentReceiptListCreateView.as_view(), name='bill-payment-receipts'),
    
    # Students
    path('students/', views.StudentListView.as_view(), name='student-list'),
    
    # Student Bills (Admin/Staff) - Custom charges are now managed within bills
    path('bills/', views.StudentBillListView.as_view(), name='student-bill-list'),
    path('bills/create/', views.StudentBillCreateView.as_view(), name='student-bill-create'),
    path('bills/<int:pk>/', views.StudentBillDetailView.as_view(), name='student-bill-detail'),
    path('bills/<int:bill_id>/logs/', views.StudentBillLogListView.as_view(), name='student-bill-logs'),
    
    # Student-Only APIs
    path('my-bills/', views.StudentMyBillsView.as_view(), name='student-my-bills'),
    path('my-bills/current-class/', views.StudentCurrentClassBillsView.as_view(), name='student-current-class-bills'),
    path('my-bills/previous-classes/', views.StudentPreviousClassBillsView.as_view(), name='student-previous-class-bills'),

    # Payment Receipt Requests
    path('receipt-requests/', views.PaymentReceiptRequestListCreateView.as_view(), name='payment-receipt-request-list-create'),
    path('receipt-requests/<int:pk>/', views.PaymentReceiptRequestDetailView.as_view(), name='payment-receipt-request-detail'),
    path('receipt-requests/<int:pk>/review/', views.PaymentReceiptRequestReviewView.as_view(), name='payment-receipt-request-review'),
    path('receipt-requests/<int:request_id>/logs/', views.PaymentReceiptRequestLogListView.as_view(), name='payment-receipt-request-logs'),
]