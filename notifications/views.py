from django.contrib import admin
from django.contrib.admin.views.decorators import staff_member_required
from django.contrib.humanize.templatetags.humanize import naturaltime
from django.core.paginator import Paginator
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect
from django.template.response import TemplateResponse
from django.urls import reverse
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.http import require_POST

from learning import workspaces

from .models import Notification


@staff_member_required
def feed(request):
    """Unread count and the latest few notifications, polled by the header bell."""
    mine = workspaces.without_hidden_links(request, Notification.objects.filter(recipient=request.user))
    items = [
        {
            "id": n.id,
            "title": n.title,
            "body": n.body,
            "kind": n.kind,
            "is_read": n.is_read,
            "when": naturaltime(n.created_at),
            "open_url": reverse("notifications:open", args=[n.id]),
        }
        for n in mine[:8]
    ]
    return JsonResponse({"unread": mine.filter(is_read=False).count(), "items": items})


@staff_member_required
def open_notification(request, pk):
    n = get_object_or_404(Notification, pk=pk, recipient=request.user)
    if not n.is_read:
        n.is_read = True
        n.save(update_fields=["is_read"])
    if n.url and url_has_allowed_host_and_scheme(n.url, allowed_hosts={request.get_host()}):
        return redirect(n.url)
    return redirect("notifications:list")


@staff_member_required
@require_POST
def read_all(request):
    # Only what the bell shows: the other workspace's notifications stay unread until the user switches.
    unread = Notification.objects.filter(recipient=request.user, is_read=False)
    workspaces.without_hidden_links(request, unread).update(is_read=True)
    if request.headers.get("x-requested-with") == "fetch":
        return JsonResponse({"unread": 0})
    target = request.POST.get("next", "")
    if not url_has_allowed_host_and_scheme(target, allowed_hosts={request.get_host()}):
        target = reverse("notifications:list")
    return redirect(target)


@staff_member_required
def notification_list(request):
    page = Paginator(Notification.objects.filter(recipient=request.user), 25).get_page(request.GET.get("page"))
    context = {**admin.site.each_context(request), "title": "Notifications", "page": page}
    return TemplateResponse(request, "notifications/list.html", context)
