#adnin_auth/models.py

from django.conf import settings
from django.db import models

from authapp.models import CustomUser


class AdminCustomUser(CustomUser):
    class Meta:
        proxy = True
        verbose_name = "Admin Custom User"
        verbose_name_plural = "Admin Custom Users"


class UserChangeLog(models.Model):
    """
    Stores an immutable audit trail of changes made to users and guardians.

    The target user and administrator foreign keys use SET_NULL so that audit
    records remain available even if either account is later deleted.

    Email snapshot fields preserve the identities involved at the exact time
    the action occurred.
    """

    class Action(models.TextChoices):
        CREATE = "create", "Created"
        UPDATE = "update", "Updated"
        DELETE = "delete", "Deleted"
        BLOCK = "block", "Blocked"
        UNBLOCK = "unblock", "Unblocked"
        ACTIVATE = "activate", "Activated"
        DEACTIVATE = "deactivate", "Deactivated"
        GUARDIAN_CREATE = "guardian_create", "Guardian Created"
        GUARDIAN_UPDATE = "guardian_update", "Guardian Updated"
        GUARDIAN_DELETE = "guardian_delete", "Guardian Deleted"

    target_user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="admin_change_logs",
        help_text="User whose account or related guardian information was changed.",
    )

    target_user_email = models.EmailField(
        max_length=255,
        db_index=True,
        help_text="Email snapshot of the affected user at the time of the change.",
    )

    changed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="performed_user_change_logs",
        help_text="Administrator who performed the change.",
    )

    changed_by_email = models.EmailField(
        max_length=255,
        db_index=True,
        help_text="Email snapshot of the administrator who performed the change.",
    )

    action = models.CharField(
        max_length=30,
        choices=Action.choices,
        db_index=True,
    )

    field_name = models.CharField(
        max_length=150,
        blank=True,
        default="",
        help_text=(
            "Changed user field or guardian field. Examples: first_name or "
            "guardian[12].primary_phone."
        ),
    )

    previous_value = models.TextField(
        null=True,
        blank=True,
    )

    new_value = models.TextField(
        null=True,
        blank=True,
    )

    changed_at = models.DateTimeField(
        auto_now_add=True,
        db_index=True,
    )

    request_method = models.CharField(
        max_length=10,
        blank=True,
        default="",
    )

    request_path = models.CharField(
        max_length=500,
        blank=True,
        default="",
    )

    ip_address = models.GenericIPAddressField(
        null=True,
        blank=True,
    )

    class Meta:
        ordering = ["-changed_at", "-id"]
        verbose_name = "User Change Log"
        verbose_name_plural = "User Change Logs"
        indexes = [
            models.Index(
                fields=["target_user_email", "changed_at"],
                name="userlog_target_date_idx",
            ),
            models.Index(
                fields=["changed_by_email", "changed_at"],
                name="userlog_actor_date_idx",
            ),
            models.Index(
                fields=["action", "changed_at"],
                name="userlog_action_date_idx",
            ),
        ]

    def __str__(self):
        field_description = (
            f" field '{self.field_name}'"
            if self.field_name
            else ""
        )

        return (
            f"{self.get_action_display()}{field_description} for "
            f"{self.target_user_email} by {self.changed_by_email}"
        )

    def save(self, *args, **kwargs):
        """
        Audit logs should not normally be modified once created.

        This prevents accidental updates through regular model save calls.
        Django admin is also configured as read-only.
        """
        if self.pk:
            raise ValueError("UserChangeLog records are immutable.")

        super().save(*args, **kwargs)