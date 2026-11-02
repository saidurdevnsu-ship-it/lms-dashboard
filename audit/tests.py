from django.contrib.admin.models import LogEntry
from django.contrib.auth import get_user_model
from django.core import serializers
from django.test import TestCase
from django.urls import reverse

from .models import AuditEvent

Action = AuditEvent.Action


class AuditLogTests(TestCase):
    def setUp(self):
        self.boss = get_user_model().objects.create_superuser("boss", "boss@example.com", "Pw-12345!")

    def test_sign_in_and_failed_sign_in_are_logged_without_password(self):
        self.client.post(reverse("login"), {"username": "boss", "password": "wrong-pw"})
        self.client.post(reverse("login"), {"username": "boss", "password": "Pw-12345!"})
        failed = AuditEvent.objects.get(action=Action.LOGIN_FAILED)
        self.assertIn("boss", failed.details)
        self.assertNotIn("wrong-pw", failed.details)
        self.assertTrue(AuditEvent.objects.filter(action=Action.LOGIN, actor=self.boss).exists())

    def test_admin_edits_are_logged(self):
        self.client.force_login(self.boss)
        self.client.post(reverse("admin:questionbank_subject_add"), {"name": "Physics", "code": "PHY"})
        entry = AuditEvent.objects.get(action=Action.ADDED)
        self.assertEqual((entry.actor_name, entry.target_type, entry.target), ("boss", "subject", "Physics"))

    def test_loading_old_admin_history_is_not_logged_again(self):
        # Copying data between databases (loaddata) re-saves old admin log entries; they aren't new actions.
        self.client.force_login(self.boss)
        self.client.post(reverse("admin:questionbank_subject_add"), {"name": "Physics", "code": "PHY"})
        fixture = serializers.serialize("json", LogEntry.objects.all())
        LogEntry.objects.all().delete()
        before = AuditEvent.objects.count()
        for obj in serializers.deserialize("json", fixture):
            obj.save()  # what loaddata does: a raw save
        self.assertEqual(LogEntry.objects.count(), 1)
        self.assertEqual(AuditEvent.objects.count(), before)

    def test_log_is_read_only(self):
        self.client.force_login(self.boss)
        self.assertEqual(self.client.get(reverse("admin:audit_auditevent_changelist")).status_code, 200)
        self.assertEqual(self.client.get(reverse("admin:audit_auditevent_add")).status_code, 403)
