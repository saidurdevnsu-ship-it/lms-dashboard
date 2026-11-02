"""Sign-ins, sign-outs, and every add / edit / delete made through the admin, which Django already notes in LogEntry."""

from django.contrib.admin.models import ADDITION, CHANGE, DELETION, LogEntry
from django.contrib.auth.signals import user_logged_in, user_logged_out, user_login_failed
from django.db.models.signals import post_save
from django.dispatch import receiver

from .services import Action, record

ADMIN_ACTIONS = {ADDITION: Action.ADDED, CHANGE: Action.CHANGED, DELETION: Action.DELETED}


@receiver(user_logged_in)
def logged_in(sender, request, user, **kwargs):
    record(user, Action.LOGIN, request=request)


@receiver(user_logged_out)
def logged_out(sender, request, user, **kwargs):
    if user is not None:
        record(user, Action.LOGOUT, request=request)


@receiver(user_login_failed)
def login_failed(sender, credentials, request=None, **kwargs):
    # Django masks the password in credentials; only the username is kept.
    record(None, Action.LOGIN_FAILED, details=f"Username: {credentials.get('username', '')}", request=request)


@receiver(post_save, sender=LogEntry)
def admin_change(sender, instance, created, raw=False, **kwargs):
    # raw: loaddata re-saving old history (e.g. copying the data to another database), not a new admin action.
    if not created or raw:
        return
    model = instance.content_type.model_class() if instance.content_type_id else None
    record(
        instance.user, ADMIN_ACTIONS.get(instance.action_flag, Action.CHANGED),
        target_type=model._meta.verbose_name if model else "", target_id=instance.object_id or "",
        target_repr=instance.object_repr, details=instance.get_change_message(),
    )
