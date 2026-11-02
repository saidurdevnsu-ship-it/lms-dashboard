from django.contrib.auth import get_user_model
from django.db import IntegrityError, connection, transaction
from django.db.migrations.executor import MigrationExecutor
from django.test import TestCase, TransactionTestCase

from .models import Notification
from .services import notify


class DedupeTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user("teacher", password="Pw-12345!")

    def test_same_key_is_sent_once(self):
        self.assertEqual(len(notify(self.user, "Due soon", dedupe_key="paper-due:1:3d")), 1)
        self.assertEqual(notify(self.user, "Due soon", dedupe_key="paper-due:1:3d"), [])
        self.assertEqual(Notification.objects.count(), 1)

    def test_without_key_every_notification_is_kept(self):
        notify(self.user, "One")
        notify(self.user, "Two")
        self.assertEqual(Notification.objects.filter(dedupe_key=None).count(), 2)

    def test_database_rejects_a_repeated_key(self):
        # A plain unique constraint, so MySQL enforces it too (it ignores conditional ones).
        Notification.objects.create(recipient=self.user, title="A", dedupe_key="k")
        with self.assertRaises(IntegrityError), transaction.atomic():
            Notification.objects.create(recipient=self.user, title="B", dedupe_key="k")


class DedupeKeyMigrationTests(TransactionTestCase):
    """0002 turns the old "no key" value ("") into NULL, so existing notifications don't clash in the new constraint."""

    before = [("notifications", "0001_initial")]
    after = [("notifications", "0002_dedupe_key_null")]

    def migrate(self, targets):
        executor = MigrationExecutor(connection)
        executor.loader.build_graph()
        executor.migrate(targets)
        return executor.loader.project_state(targets).apps

    def tearDown(self):
        # Leave the schema fully migrated for the tests that run after this one.
        executor = MigrationExecutor(connection)
        executor.migrate(executor.loader.graph.leaf_nodes())

    def test_blank_keys_become_null_and_no_longer_clash(self):
        old_apps = self.migrate(self.before)
        user = old_apps.get_model("auth", "User").objects.create(username="teacher")
        OldNotification = old_apps.get_model("notifications", "Notification")
        OldNotification.objects.create(recipient_id=user.pk, title="One", dedupe_key="")
        OldNotification.objects.create(recipient_id=user.pk, title="Two", dedupe_key="")

        new_apps = self.migrate(self.after)
        NewNotification = new_apps.get_model("notifications", "Notification")
        self.assertEqual(NewNotification.objects.filter(dedupe_key__isnull=True).count(), 2)
        self.assertFalse(NewNotification.objects.filter(dedupe_key="").exists())
