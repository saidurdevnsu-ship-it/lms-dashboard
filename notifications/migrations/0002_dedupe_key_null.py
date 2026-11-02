from django.db import migrations, models


def blank_to_null(apps, schema_editor):
    apps.get_model("notifications", "Notification").objects.filter(dedupe_key="").update(dedupe_key=None)


def null_to_blank(apps, schema_editor):
    apps.get_model("notifications", "Notification").objects.filter(dedupe_key=None).update(dedupe_key="")


class Migration(migrations.Migration):
    """Replace the conditional unique constraint (which MySQL ignores) with a plain one over a nullable key."""

    dependencies = [
        ("notifications", "0001_initial"),
    ]

    operations = [
        migrations.RemoveConstraint(model_name="notification", name="notification_once_per_key"),
        migrations.AlterField(
            model_name="notification",
            name="dedupe_key",
            field=models.CharField(blank=True, max_length=100, null=True),
        ),
        migrations.RunPython(blank_to_null, null_to_blank),
        migrations.AddConstraint(
            model_name="notification",
            constraint=models.UniqueConstraint(fields=("recipient", "dedupe_key"), name="notification_once_per_key"),
        ),
    ]
