from django.conf import settings
from django.db import models


class Notification(models.Model):
    class Kind(models.TextChoices):
        APPROVAL = "approval", "Needs your approval"
        REMINDER = "reminder", "Reminder"
        SUCCESS = "success", "Approved / done"
        WARNING = "warning", "Needs attention"
        INFO = "info", "Information"

    recipient = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="notifications")
    kind = models.CharField(max_length=10, choices=Kind.choices, default=Kind.INFO)
    title = models.CharField(max_length=200)
    body = models.CharField(max_length=300, blank=True)
    url = models.CharField(max_length=300, blank=True)
    is_read = models.BooleanField(default=False)
    # Lets reminders be sent once per (recipient, key), e.g. "paper-due:12:3d". NULL (not "") when there's no key:
    # NULLs never clash in a unique constraint, so this works on MySQL, which has no conditional unique constraints.
    dedupe_key = models.CharField(max_length=100, null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["recipient", "is_read"])]
        constraints = [
            models.UniqueConstraint(fields=["recipient", "dedupe_key"], name="notification_once_per_key"),
        ]

    def __str__(self):
        return f"{self.recipient} · {self.title}"
