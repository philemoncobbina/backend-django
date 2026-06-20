from decimal import Decimal
import logging

from django.db import IntegrityError, transaction
from django.utils import timezone
from rest_framework import serializers

from .models import (
    BillingTemplate,
    BillingItem,
    StudentBill,
    StudentBillLog,
    CustomCharge,
    PaymentReceipt,
    BillingItemLog,
    PaymentReceiptRequest,
    PaymentReceiptRequestLog,
    _decimal_str,
)

logger = logging.getLogger(__name__)


# ===========================================================================
# BILLING ITEM SERIALIZERS
# ===========================================================================

class BillingItemLogSerializer(serializers.ModelSerializer):
    class Meta:
        model = BillingItemLog
        fields = [
            "id", "field_name", "old_value", "new_value",
            "user_first_name", "user_last_name", "user_email", "timestamp"
        ]


class BillingItemSerializer(serializers.ModelSerializer):
    created_by = serializers.StringRelatedField(read_only=True)
    logs = BillingItemLogSerializer(many=True, read_only=True)

    class Meta:
        model = BillingItem
        fields = [
            "id", "billing_template", "item_name", "category", "amount",
            "created_date", "created_by", "logs"
        ]
        read_only_fields = ["id", "created_date", "created_by", "logs"]

    def create(self, validated_data):
        user = self.context["request"].user
        item = BillingItem(created_by=user, **validated_data)
        item._current_user = user
        item.save()
        return item

    def update(self, instance, validated_data):
        user = self.context["request"].user
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
            "id", "academic_year", "class_name", "term", "created_date",
            "created_by", "due_date", "billing_items"
        ]
        read_only_fields = ["id", "created_date", "created_by"]

    def create(self, validated_data):
        user = self.context["request"].user
        instance = BillingTemplate(created_by=user, **validated_data)
        instance._current_user = user
        instance.save()
        return instance

    def update(self, instance, validated_data):
        user = self.context["request"].user
        instance._current_user = user

        for attr, value in validated_data.items():
            setattr(instance, attr, value)

        instance.save()
        return instance


# ===========================================================================
# CUSTOM CHARGE & PAYMENT RECEIPT SERIALIZERS
# ===========================================================================

class CustomChargeSerializer(serializers.ModelSerializer):
    id = serializers.IntegerField(required=False)

    class Meta:
        model = CustomCharge
        fields = ["id", "charge_name", "description", "amount", "created_date"]
        read_only_fields = ["created_date"]


class PaymentReceiptSerializer(serializers.ModelSerializer):
    created_by = serializers.StringRelatedField(read_only=True)

    class Meta:
        model = PaymentReceipt
        fields = [
            "id", "student_bill", "receipt_number", "amount_paid",
            "payment_date", "payment_method", "notes", "created_by"
        ]
        read_only_fields = ["id", "receipt_number", "created_by"]

    def create(self, validated_data):
        user = self.context["request"].user
        receipt = PaymentReceipt(created_by=user, **validated_data)
        receipt._current_user = user
        receipt.save()
        return receipt

    def update(self, instance, validated_data):
        user = self.context["request"].user
        instance._current_user = user

        for attr, value in validated_data.items():
            setattr(instance, attr, value)

        instance.save()
        return instance


# ===========================================================================
# STUDENT BILL LOG SERIALIZER
# ===========================================================================

class StudentBillLogSerializer(serializers.ModelSerializer):
    class Meta:
        model = StudentBillLog
        fields = [
            "id", "field_name", "old_value", "new_value",
            "user_first_name", "user_last_name", "user_email", "timestamp"
        ]


# ===========================================================================
# STUDENT BILL SERIALIZERS
# ===========================================================================

class StudentBillSummarySerializer(serializers.ModelSerializer):
    student_name = serializers.SerializerMethodField()
    class_term = serializers.SerializerMethodField()
    current_bill_balance = serializers.SerializerMethodField()
    balance_due = serializers.SerializerMethodField()
    credit_balance = serializers.SerializerMethodField()
    pdf_url = serializers.SerializerMethodField()

    class Meta:
        model = StudentBill
        fields = [
            "id", "bill_number", "student_name", "class_term",
            "total_amount_due", "total_paid",
            "current_bill_balance",
            "previous_arrears",
            "balance_due",
            "credit_balance",
            "payment_status", "status",
            "due_date", "is_overdue", "generated_date",
            "discount_amount", "discount_reason", "discount_approved_by",
            "pdf_url"
        ]

    def get_student_name(self, obj):
        return f"{obj.first_name} {obj.last_name}"

    def get_class_term(self, obj):
        return f"{obj.billing_template.class_name} - {obj.billing_template.get_term_display()}"

    def get_current_bill_balance(self, obj):
        return _decimal_str(obj.current_bill_balance)

    def get_balance_due(self, obj):
        return _decimal_str(obj.balance_due)

    def get_credit_balance(self, obj):
        return _decimal_str(obj.credit_balance)

    def get_pdf_url(self, obj):
        if obj.pdf_file:
            request = self.context.get("request")
            if request:
                return request.build_absolute_uri(obj.pdf_file.url)
            return obj.pdf_file.url
        return None


class StudentBillSerializer(serializers.ModelSerializer):
    created_by = serializers.StringRelatedField(read_only=True)
    student = serializers.StringRelatedField(read_only=True)
    billing_template = BillingTemplateSerializer(read_only=True)
    custom_charges = CustomChargeSerializer(many=True, required=False)
    payment_receipts = PaymentReceiptSerializer(many=True, read_only=True)
    logs = StudentBillLogSerializer(many=True, read_only=True)

    current_bill_balance = serializers.SerializerMethodField()
    balance_due = serializers.SerializerMethodField()
    credit_balance = serializers.SerializerMethodField()
    pdf_url = serializers.SerializerMethodField()

    class Meta:
        model = StudentBill
        fields = [
            "id", "student", "billing_template", "bill_number",
            "first_name", "last_name",
            "previous_arrears", "discount_amount", "discount_reason", "discount_approved_by",
            "status", "payment_status",
            "generated_date", "scheduled_date", "due_date", "created_date", "created_by",
            "total_amount_due", "total_paid", "notes",
            "custom_charges", "payment_receipts", "logs",
            "current_bill_balance", "balance_due", "credit_balance",
            "pdf_url"
        ]
        read_only_fields = [
            "id", "bill_number", "first_name", "last_name",
            "generated_date", "created_date", "created_by",
            "previous_arrears", "total_amount_due", "total_paid",
            "payment_status",
            "payment_receipts", "logs",
            "current_bill_balance", "balance_due", "credit_balance",
            "pdf_url"
        ]

    def validate(self, data):
        discount_amount = data.get("discount_amount", getattr(self.instance, "discount_amount", Decimal("0.00")))
        discount_reason = data.get("discount_reason", getattr(self.instance, "discount_reason", ""))
        discount_approved_by = data.get("discount_approved_by", getattr(self.instance, "discount_approved_by", ""))

        if discount_amount and discount_amount > Decimal("0.00"):
            if not discount_reason or not discount_reason.strip():
                raise serializers.ValidationError({
                    "discount_reason": "Discount reason is required when applying a discount."
                })
            if not discount_approved_by or not discount_approved_by.strip():
                raise serializers.ValidationError({
                    "discount_approved_by": "Discount approved by is required when applying a discount."
                })

        return data

    def get_current_bill_balance(self, obj):
        return _decimal_str(obj.current_bill_balance)

    def get_balance_due(self, obj):
        return _decimal_str(obj.balance_due)

    def get_credit_balance(self, obj):
        return _decimal_str(obj.credit_balance)

    def get_pdf_url(self, obj):
        if obj.pdf_file:
            request = self.context.get("request")
            if request:
                return request.build_absolute_uri(obj.pdf_file.url)
            return obj.pdf_file.url
        return None

    @transaction.atomic
    def update(self, instance, validated_data):
        user = self.context["request"].user
        instance._current_user = user

        custom_charges_data = validated_data.pop("custom_charges", None)

        for attr, value in validated_data.items():
            setattr(instance, attr, value)

        # Save bill editable fields
        instance.save()

        # Then sync custom charges if provided
        if custom_charges_data is not None:
            self.update_custom_charges(instance, custom_charges_data, user)
            instance.refresh_from_db()

        return instance

    def update_custom_charges(self, bill_instance, custom_charges_data, user):
        """
        Create, update, delete charges.
        Logging is handled ONLY by the CustomCharge model.
        """
        existing_charges = {charge.id: charge for charge in bill_instance.custom_charges.all()}
        existing_ids = set(existing_charges.keys())
        provided_ids = set()

        for charge_data in custom_charges_data:
            charge_id = charge_data.get("id")

            if charge_id is not None:
                try:
                    charge_id = int(charge_id)
                except (ValueError, TypeError):
                    charge_id = None

            if charge_id and charge_id in existing_ids:
                provided_ids.add(charge_id)
                charge = existing_charges[charge_id]
                charge._current_user = user
                charge.charge_name = charge_data.get("charge_name", charge.charge_name)
                charge.description = charge_data.get("description", charge.description)
                charge.amount = charge_data.get("amount", charge.amount)
                charge.save()
            else:
                data_clean = {k: v for k, v in charge_data.items() if k != "id"}
                new_charge = CustomCharge(
                    student_bill=bill_instance,
                    created_by=user,
                    **data_clean
                )
                new_charge._current_user = user
                new_charge.save()

        to_delete_ids = existing_ids - provided_ids

        for charge_id in to_delete_ids:
            charge = existing_charges[charge_id]
            charge._current_user = user
            charge.delete()


# ===========================================================================
# STUDENT BILL CREATE SERIALIZER
# ===========================================================================

class StudentBillCreateSerializer(serializers.ModelSerializer):
    custom_charges = CustomChargeSerializer(many=True, required=False)

    class Meta:
        model = StudentBill
        fields = [
            "student", "billing_template", "discount_amount",
            "discount_reason", "discount_approved_by", "status",
            "scheduled_date", "notes", "custom_charges"
        ]

    def validate(self, data):
        student = data.get("student")
        billing_template = data.get("billing_template")

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

        discount_amount = data.get("discount_amount", Decimal("0.00"))
        discount_reason = data.get("discount_reason", "")
        discount_approved_by = data.get("discount_approved_by", "")

        if discount_amount and discount_amount > Decimal("0.00"):
            if not discount_reason or not discount_reason.strip():
                raise serializers.ValidationError({
                    "discount_reason": "Discount reason is required when applying a discount."
                })
            if not discount_approved_by or not discount_approved_by.strip():
                raise serializers.ValidationError({
                    "discount_approved_by": "Discount approved by is required when applying a discount."
                })

        return data

    @transaction.atomic
    def create(self, validated_data):
        user = self.context["request"].user
        custom_charges_data = validated_data.pop("custom_charges", [])

        try:
            # ✅ STEP 1: create bill WITHOUT generating PDF prematurely
            bill = StudentBill(created_by=user, **validated_data)
            bill._current_user = user
            bill.save(skip_pdf_generation=True)

            # ✅ STEP 2: add custom charges (these trigger recalculation internally)
            for charge_data in custom_charges_data:
                charge = CustomCharge(
                    student_bill=bill,
                    created_by=user,
                    **charge_data
                )
                charge._current_user = user
                charge.save()

            # ✅ STEP 3 (CRITICAL FIX): force correct financial state NOW
            bill.refresh_from_db()
            bill.apply_financial_recalculation(
                actor=user,
                cascade_subsequent=False,   # important: avoid double chaining
                queue_pdf=True              # now safe to generate PDF
            )
            bill.refresh_from_db()

            return bill

        except IntegrityError as e:
            err_str = str(e).lower()
            if "bill_number" in err_str:
                raise serializers.ValidationError(
                    "Failed to generate unique bill number. Please try again."
                )
            raise serializers.ValidationError(
                "A bill already exists for this student and billing template combination."
            )


# ===========================================================================
# PAYMENT RECEIPT REQUEST SERIALIZERS
# ===========================================================================

class PaymentReceiptRequestLogSerializer(serializers.ModelSerializer):
    class Meta:
        model = PaymentReceiptRequestLog
        fields = [
            "id", "action", "old_status", "new_status", "comment",
            "actor_first_name", "actor_last_name", "actor_email", "timestamp"
        ]


class PaymentReceiptRequestSerializer(serializers.ModelSerializer):
    submitted_by = serializers.StringRelatedField(read_only=True)
    reviewed_by = serializers.StringRelatedField(read_only=True)
    generated_receipt = PaymentReceiptSerializer(read_only=True)
    logs = PaymentReceiptRequestLogSerializer(many=True, read_only=True)
    bill_number = serializers.CharField(source="student_bill.bill_number", read_only=True)
    student_name = serializers.SerializerMethodField()
    proof_of_payment_url = serializers.SerializerMethodField()

    class Meta:
        model = PaymentReceiptRequest
        fields = [
            "id", "student_bill", "bill_number", "student_name",
            "submitted_by", "amount", "payment_method", "payment_reference",
            "phone_number",
            "proof_of_payment", "proof_of_payment_url",
            "status", "reviewed_by", "review_comment", "reviewed_at",
            "generated_receipt", "submitted_at", "updated_at", "logs",
        ]
        read_only_fields = [
            "id", "submitted_by", "reviewed_by", "reviewed_at",
            "generated_receipt", "submitted_at", "updated_at", "logs",
            "bill_number", "student_name", "proof_of_payment_url",
        ]

    def get_student_name(self, obj):
        return f"{obj.submitted_by.first_name} {obj.submitted_by.last_name}"

    def get_proof_of_payment_url(self, obj):
        if obj.proof_of_payment:
            request = self.context.get("request")
            if request:
                return request.build_absolute_uri(obj.proof_of_payment.url)
            return obj.proof_of_payment.url
        return None


class PaymentReceiptRequestCreateSerializer(serializers.ModelSerializer):
    class Meta:
        model = PaymentReceiptRequest
        fields = [
            "student_bill", "amount", "payment_method",
            "payment_reference", "phone_number", "proof_of_payment",
        ]
        extra_kwargs = {
            "phone_number": {"required": True}
        }

    def validate_student_bill(self, bill):
        request = self.context.get("request")
        if request and bill.student != request.user:
            raise serializers.ValidationError(
                "You can only submit payment requests for your own bills."
            )
        if bill.status != "PUBLISHED":
            raise serializers.ValidationError(
                "Payment requests can only be submitted for published bills."
            )
        return bill

    def validate_proof_of_payment(self, file):
        allowed_types = ["image/jpeg", "image/png", "image/gif", "application/pdf"]

        if hasattr(file, "content_type") and file.content_type not in allowed_types:
            raise serializers.ValidationError(
                "Only JPEG, PNG, GIF images and PDF files are accepted as proof of payment."
            )

        file.seek(0)
        header = file.read(8)
        file.seek(0)

        magic_signatures = {
            b"\xff\xd8\xff": "image/jpeg",
            b"\x89PNG\r\n\x1a\n": "image/png",
            b"GIF87a": "image/gif",
            b"GIF89a": "image/gif",
            b"%PDF": "application/pdf",
        }

        detected_type = None
        for signature, mime in magic_signatures.items():
            if header[:len(signature)] == signature:
                detected_type = mime
                break

        if detected_type is None:
            raise serializers.ValidationError(
                "File content does not match an accepted format (JPEG, PNG, GIF, or PDF)."
            )

        if hasattr(file, "content_type") and file.content_type != detected_type:
            raise serializers.ValidationError(
                "File content does not match the declared content type."
            )

        if file.size > 10 * 1024 * 1024:
            raise serializers.ValidationError(
                "Proof of payment file must be smaller than 10 MB."
            )

        return file

    def create(self, validated_data):
        user = self.context["request"].user
        receipt_request = PaymentReceiptRequest(
            submitted_by=user,
            status=PaymentReceiptRequest.STATUS_PENDING,
            **validated_data
        )
        receipt_request._current_user = user
        receipt_request.save()
        return receipt_request


class PaymentReceiptRequestReviewSerializer(serializers.ModelSerializer):
    class Meta:
        model = PaymentReceiptRequest
        fields = [
            "status",
            "review_comment",
            "amount",
            "payment_method",
            "payment_reference",
            "phone_number",
        ]
        extra_kwargs = {
            "status": {"required": False},
            "amount": {"required": False},
            "payment_method": {"required": False},
            "payment_reference": {"required": False},
            "phone_number": {"required": False},
        }

    def validate_amount(self, value):
        if value is not None and value <= Decimal("0.00"):
            raise serializers.ValidationError("Amount must be greater than zero.")
        return value

    def validate(self, data):
        new_status = data.get("status")
        comment = data.get("review_comment", "")

        if new_status in [
            PaymentReceiptRequest.STATUS_UNDER_REVIEW,
            PaymentReceiptRequest.STATUS_REJECTED,
        ]:
            if not comment or not comment.strip():
                raise serializers.ValidationError({
                    "review_comment": (
                        f"A comment is required when setting status to "
                        f"'{new_status.replace('_', ' ')}'."
                    )
                })

        return data

    @transaction.atomic
    def update(self, instance, validated_data):
        reviewer = self.context["request"].user

        for field in ("amount", "payment_method", "payment_reference", "phone_number"):
            if field in validated_data:
                setattr(instance, field, validated_data.pop(field))

        new_status = validated_data.get("status")
        comment = validated_data.get("review_comment", instance.review_comment or "")
        currently_accepted = instance.status == PaymentReceiptRequest.STATUS_ACCEPTED

        if new_status == PaymentReceiptRequest.STATUS_ACCEPTED:
            instance._current_user = reviewer
            instance.review_comment = comment
            instance.reviewed_by = reviewer
            instance.reviewed_at = timezone.now()
            instance.save()

            instance.accept_and_generate_receipt(reviewed_by_user=reviewer)

        elif currently_accepted and new_status is not None:
            instance.revoke_and_delete_receipt(
                revoked_by_user=reviewer,
                new_status=new_status,
                comment=comment,
            )

        else:
            instance._current_user = reviewer
            if new_status is not None:
                instance.status = new_status
            instance.review_comment = comment
            instance.reviewed_by = reviewer
            instance.reviewed_at = timezone.now()
            instance.save()

        return instance