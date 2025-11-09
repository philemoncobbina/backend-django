from rest_framework import serializers
from django.db import transaction
from decimal import Decimal
from .models import (
    BillingTemplate, BillingItem, StudentBill, StudentBillLog, 
    CustomCharge, PaymentReceipt, BillingItemLog
)
from authapp.models import CustomUser


class BillingItemLogSerializer(serializers.ModelSerializer):
    class Meta:
        model = BillingItemLog
        fields = [
            'id', 'field_name', 'old_value', 'new_value',
            'user_first_name', 'user_last_name', 'user_email', 'timestamp'
        ]


class BillingItemSerializer(serializers.ModelSerializer):
    created_by = serializers.StringRelatedField(read_only=True)
    logs = BillingItemLogSerializer(many=True, read_only=True)

    class Meta:
        model = BillingItem
        fields = [
            'id', 'billing_template', 'item_name', 'category', 'amount',
            'created_date', 'created_by', 'logs'
        ]
        read_only_fields = ['id', 'created_date', 'created_by', 'logs']

    def update(self, instance, validated_data):
        """Override update to set user context for logging"""
        user = self.context['request'].user
        instance._current_user = user
        
        for attr, value in validated_data.items():
            setattr(instance, attr, value)
        
        instance.save()
        return instance


class BillingTemplateSerializer(serializers.ModelSerializer):
    created_by = serializers.StringRelatedField(read_only=True)
    billing_items = BillingItemSerializer(many=True, read_only=True)

    class Meta:
        model = BillingTemplate
        fields = [
            'id', 'academic_year', 'class_name', 'term', 'created_date',
            'created_by', 'due_date', 'billing_items'
        ]
        read_only_fields = ['id', 'created_date', 'created_by']


class CustomChargeSerializer(serializers.ModelSerializer):
    id = serializers.IntegerField(required=False)

    class Meta:
        model = CustomCharge
        fields = ['id', 'charge_name', 'description', 'amount', 'created_date']
        read_only_fields = ['created_date']


class PaymentReceiptSerializer(serializers.ModelSerializer):
    created_by = serializers.StringRelatedField(read_only=True)

    class Meta:
        model = PaymentReceipt
        fields = [
            'id', 'student_bill', 'receipt_number', 'amount_paid',
            'payment_date', 'payment_method', 'notes', 'created_by'
        ]
        read_only_fields = ['id', 'created_by']

    def create(self, validated_data):
        validated_data['created_by'] = self.context['request'].user
        receipt = PaymentReceipt(**validated_data)
        receipt._current_user = self.context['request'].user
        receipt.save()
        return receipt

    def update(self, instance, validated_data):
        """Override update to set user context for logging and cascading updates"""
        user = self.context['request'].user
        instance._current_user = user
        
        for attr, value in validated_data.items():
            setattr(instance, attr, value)
        
        instance.save()
        return instance


class StudentBillLogSerializer(serializers.ModelSerializer):
    class Meta:
        model = StudentBillLog
        fields = [
            'id', 'field_name', 'old_value', 'new_value',
            'user_first_name', 'user_last_name', 'user_email', 'timestamp'
        ]


class StudentBillSerializer(serializers.ModelSerializer):
    created_by = serializers.StringRelatedField(read_only=True)
    student = serializers.StringRelatedField(read_only=True)
    billing_template = BillingTemplateSerializer(read_only=True)
    custom_charges = CustomChargeSerializer(many=True, required=False)
    payment_receipts = PaymentReceiptSerializer(many=True, read_only=True)
    logs = StudentBillLogSerializer(many=True, read_only=True)
    
    current_bill_balance = serializers.SerializerMethodField()
    total_outstanding = serializers.SerializerMethodField()

    class Meta:
        model = StudentBill
        fields = [
            'id', 'student', 'billing_template', 'bill_number',
            'first_name', 'last_name', 'previous_arrears', 'discount_amount',
            'discount_reason', 'discount_approved_by', 'status', 'payment_status', 
            'generated_date', 'scheduled_date', 'due_date', 'created_date', 'created_by',
            'total_amount_due', 'total_paid', 'notes', 'custom_charges', 
            'payment_receipts', 'logs', 'current_bill_balance', 'total_outstanding'
        ]
        read_only_fields = [
            'id', 'bill_number', 'first_name', 'last_name', 'generated_date',
            'created_date', 'created_by', 'total_amount_due',
            'balance_due', 'is_overdue', 'payment_receipts', 
            'logs', 'previous_arrears', 'total_paid', 'current_bill_balance', 'total_outstanding'
        ]

    def validate(self, data):
        """
        Validate that discount_reason and discount_approved_by are provided when discount_amount is set
        """
        discount_amount = data.get('discount_amount', getattr(self.instance, 'discount_amount', Decimal('0.00')))
        discount_reason = data.get('discount_reason', getattr(self.instance, 'discount_reason', ''))
        discount_approved_by = data.get('discount_approved_by', getattr(self.instance, 'discount_approved_by', ''))
        
        # If discount is being applied (greater than 0)
        if discount_amount and discount_amount > Decimal('0.00'):
            if not discount_reason or not discount_reason.strip():
                raise serializers.ValidationError({
                    'discount_reason': 'Discount reason is required when applying a discount.'
                })
            if not discount_approved_by or not discount_approved_by.strip():
                raise serializers.ValidationError({
                    'discount_approved_by': 'Discount approved by is required when applying a discount.'
                })
        
        return data

    def get_current_bill_balance(self, obj):
        """
        Calculate current bill balance from fresh database values
        """
        return float(obj.total_amount_due - obj.total_paid)
    
    def get_total_outstanding(self, obj):
        """
        Calculate total outstanding balance from fresh database values
        """
        return float(obj.balance_due)

    @transaction.atomic
    def update(self, instance, validated_data):
        """
        Handle updates with proper custom charges management and logging
        """
        user = self.context['request'].user
        instance._current_user = user

        # Handle custom charges if provided
        custom_charges_data = validated_data.pop('custom_charges', None)
        
        # Update the instance (non-custom-charge fields)
        for attr, value in validated_data.items():
            setattr(instance, attr, value)

        # Handle custom charges updates WITH PROPER LOGGING
        if custom_charges_data is not None:
            self.update_custom_charges(instance, custom_charges_data, user)

        # Save will trigger recalculation and cascading updates
        instance.save()
        
        return instance

    def update_custom_charges(self, bill_instance, custom_charges_data, user):
        """
        Handle custom charges: create new, update existing, delete removed
        """
        # Get existing custom charges mapped by ID
        existing_charges = {charge.id: charge for charge in bill_instance.custom_charges.all()}
        existing_charge_ids = set(existing_charges.keys())
        
        # Track which charge IDs are being kept (provided in the request)
        provided_charge_ids = set()

        # Process each charge in request data
        for charge_data in custom_charges_data:
            # Get the ID from the charge data
            charge_id = charge_data.get('id')
            
            # Convert to integer if it's a string
            if charge_id is not None:
                try:
                    charge_id = int(charge_id)
                except (ValueError, TypeError):
                    charge_id = None
            
            # CASE 1: UPDATE EXISTING CHARGE
            if charge_id and charge_id in existing_charge_ids:
                provided_charge_ids.add(charge_id)
                
                charge = existing_charges[charge_id]
                
                # Store old values BEFORE any changes
                old_charge_name = charge.charge_name
                old_description = charge.description or ''
                old_amount = charge.amount
                
                # Get new values from request
                new_charge_name = charge_data.get('charge_name', charge.charge_name)
                new_description = charge_data.get('description', charge.description) or ''
                new_amount = charge_data.get('amount', charge.amount)
                
                # Convert new_amount to Decimal for comparison
                if not isinstance(new_amount, Decimal):
                    new_amount = Decimal(str(new_amount))
                
                # Check if ANY field actually changed
                has_changes = (
                    old_charge_name != new_charge_name or 
                    old_description != new_description or 
                    old_amount != new_amount
                )
                
                # Only update and log if there are real changes
                if has_changes:
                    # Update the charge fields
                    charge.charge_name = new_charge_name
                    charge.description = new_description
                    charge.amount = new_amount
                    charge._current_user = user
                    charge.save()
                    
                    # Create formatted old and new summaries for logging
                    old_summary = f"{old_charge_name}"
                    if old_description:
                        old_summary += f" ({old_description})"
                    old_summary += f": GHS {old_amount}"
                    
                    new_summary = f"{new_charge_name}"
                    if new_description:
                        new_summary += f" ({new_description})"
                    new_summary += f": GHS {new_amount}"
                    
                    # Log as UPDATE
                    StudentBillLog.objects.create(
                        bill=bill_instance,
                        field_name='custom_charge_updated',
                        old_value=old_summary,
                        new_value=new_summary,
                        user_first_name=user.first_name,
                        user_last_name=user.last_name,
                        user_email=user.email
                    )
            
            # CASE 2: CREATE NEW CHARGE
            else:
                # Remove 'id' key if present
                charge_data_clean = {k: v for k, v in charge_data.items() if k != 'id'}
                
                # Create new charge
                new_charge = CustomCharge(
                    student_bill=bill_instance,
                    created_by=user,
                    **charge_data_clean
                )
                new_charge._current_user = user
                new_charge.save()
                
                # Log the creation
                charge_summary = f"{new_charge.charge_name}"
                if new_charge.description:
                    charge_summary += f" ({new_charge.description})"
                charge_summary += f": GHS {new_charge.amount}"
                
                StudentBillLog.objects.create(
                    bill=bill_instance,
                    field_name='custom_charge_added',
                    old_value='',
                    new_value=charge_summary,
                    user_first_name=user.first_name,
                    user_last_name=user.last_name,
                    user_email=user.email
                )
        
        # CASE 3: DELETE CHARGES NOT IN REQUEST
        charges_to_delete_ids = existing_charge_ids - provided_charge_ids
        
        for charge_id_to_delete in charges_to_delete_ids:
            charge_to_delete = existing_charges[charge_id_to_delete]
            
            # Set user context for deletion logging
            charge_to_delete._current_user = user
            
            # Delete will trigger the post_delete signal which logs the removal
            charge_to_delete.delete()


class StudentBillCreateSerializer(serializers.ModelSerializer):
    custom_charges = CustomChargeSerializer(many=True, required=False)

    class Meta:
        model = StudentBill
        fields = [
            'student', 'billing_template', 'discount_amount',
            'discount_reason', 'discount_approved_by', 'status', 
            'scheduled_date', 'notes', 'custom_charges'
        ]

    def validate(self, data):
        """
        Validate that the combination of student and billing_template doesn't already exist
        and that discount fields are provided when discount is applied
        """
        student = data.get('student')
        billing_template = data.get('billing_template')
        
        if student and billing_template:
            if StudentBill.objects.filter(
                student=student, 
                billing_template=billing_template
            ).exists():
                raise serializers.ValidationError(
                    f"A bill already exists for {student.first_name} {student.last_name} "
                    f"for {billing_template.class_name} - {billing_template.get_term_display()} "
                    f"({billing_template.academic_year})"
                )
        
        # Validate discount fields
        discount_amount = data.get('discount_amount', Decimal('0.00'))
        discount_reason = data.get('discount_reason', '')
        discount_approved_by = data.get('discount_approved_by', '')
        
        if discount_amount and discount_amount > Decimal('0.00'):
            if not discount_reason or not discount_reason.strip():
                raise serializers.ValidationError({
                    'discount_reason': 'Discount reason is required when applying a discount.'
                })
            if not discount_approved_by or not discount_approved_by.strip():
                raise serializers.ValidationError({
                    'discount_approved_by': 'Discount approved by is required when applying a discount.'
                })
        
        return data

    @transaction.atomic
    def create(self, validated_data):
        """Override create to set user context and handle custom charges WITH LOGGING"""
        user = self.context['request'].user
        custom_charges_data = validated_data.pop('custom_charges', [])
        
        validated_data['created_by'] = user
        
        try:
            # Create the bill with atomic transaction
            bill = StudentBill.objects.create(**validated_data)
            bill._current_user = user
            
            # Create custom charges if provided WITH LOGGING
            for charge_data in custom_charges_data:
                new_charge = CustomCharge(
                    student_bill=bill,
                    created_by=user,
                    **charge_data
                )
                new_charge._current_user = user
                new_charge.save()
                
                # Log the creation
                charge_summary = f"{new_charge.charge_name}"
                if new_charge.description:
                    charge_summary += f" ({new_charge.description})"
                charge_summary += f": GHS {new_charge.amount}"
                
                StudentBillLog.objects.create(
                    bill=bill,
                    field_name='custom_charge_added',
                    old_value='',
                    new_value=charge_summary,
                    user_first_name=user.first_name,
                    user_last_name=user.last_name,
                    user_email=user.email
                )
            
            return bill
            
        except Exception as e:
            if 'UNIQUE constraint failed' in str(e):
                if 'bill_number' in str(e):
                    raise serializers.ValidationError(
                        "Failed to generate unique bill number. Please try again."
                    )
                elif 'student_billing_studentbill' in str(e):
                    raise serializers.ValidationError(
                        "A bill already exists for this student and billing template combination."
                    )
            raise e


class StudentBillSummarySerializer(serializers.ModelSerializer):
    """Lightweight serializer for bill summaries without nested objects"""
    student_name = serializers.SerializerMethodField()
    class_term = serializers.SerializerMethodField()
    current_bill_balance = serializers.SerializerMethodField()
    balance_due = serializers.SerializerMethodField()  # FIXED: Added this field
    
    class Meta:
        model = StudentBill
        fields = [
            'id', 'bill_number', 'student_name', 'class_term',
            'total_amount_due', 'total_paid', 'current_bill_balance',
            'previous_arrears', 'balance_due', 'payment_status',
            'due_date', 'is_overdue', 'generated_date', 'discount_amount',
            'discount_reason', 'discount_approved_by'
        ]
    
    def get_student_name(self, obj):
        return f"{obj.first_name} {obj.last_name}"
    
    def get_class_term(self, obj):
        return f"{obj.billing_template.class_name} - {obj.billing_template.get_term_display()}"
    
    def get_current_bill_balance(self, obj):
        """
        Calculate current bill balance
        """
        return float(obj.total_amount_due - obj.total_paid)
    
    def get_balance_due(self, obj):
        """
        Calculate total outstanding balance (previous arrears + current bill balance)
        """
        return float(obj.balance_due)