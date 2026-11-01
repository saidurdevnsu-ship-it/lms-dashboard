from django.utils import timezone

from learning import workspaces

from .models import Notification


def notifications(request):
    user = getattr(request, "user", None)
    if user is None or not user.is_authenticated or not user.is_staff:
        return {}

    # Deadline reminders are worked out at most once a day per session, not on every page.
    today = timezone.localdate().isoformat()
    if request.session.get("reminders_checked") != today:
        from questionbank.reminders import ensure_reminders

        ensure_reminders(user)
        request.session["reminders_checked"] = today

    # Only this workspace's notifications (plus shared ones); the other workspace's wait until the user switches.
    mine = workspaces.without_hidden_links(request, Notification.objects.filter(recipient=user))
    return {
        "notif_unread": mine.filter(is_read=False).count(),
        "notif_recent": list(mine[:8]),
    }
