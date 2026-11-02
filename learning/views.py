import logging

from django.contrib import admin, messages
from django.contrib.admin.views.decorators import staff_member_required
from django.contrib.auth import logout
from django.contrib.auth.views import LoginView
from django.db import transaction
from django.http import Http404
from django.shortcuts import redirect, render
from django.template.loader import render_to_string
from django.template.response import TemplateResponse
from django.urls import reverse
from django.views.decorators.cache import never_cache

from audit.services import Action, record
from hr.forms import EmployeeContactForm
from hr.models import Employee
from notifications.mail import send_email, site_url

from . import workspaces
from .forms import ProfileForm, SignInForm

logger = logging.getLogger(__name__)


def home(request):
    """Site root: the dashboard for staff, otherwise the sign-in page.

    A signed-in account without dashboard access is signed out first; sending it to /admin/ would loop back here.
    """
    if request.user.is_authenticated and not request.user.is_staff:
        logout(request)
    return redirect("admin:index" if request.user.is_authenticated else "login")


class RememberMeLoginView(LoginView):
    """Login view that ends the session on browser close unless "remember me" is ticked."""

    form_class = SignInForm

    def form_valid(self, form):
        response = super().form_valid(form)
        # Every sign-in starts at the workspace picker.
        self.request.session.pop(workspaces.SESSION_KEY, None)
        if not self.request.POST.get("remember_me"):
            self.request.session.set_expiry(0)
        return response


def dashboard(request):
    """/admin/: the dashboard of the workspace picked after sign-in, or the picker if none is picked yet."""
    if request.user.is_active and request.user.is_staff:
        available = workspaces.available(request.user)
        ws = workspaces.current(request)
        if ws not in available and len(available) > 1:
            return redirect("choose_workspace")
        if ws == "hr" and "hr" in available or available == ["hr"]:
            return redirect(workspaces.home_url("hr", request.user))
    return admin.site.admin_view(admin.site.index)(request)


@staff_member_required
def choose_workspace(request):
    """After sign-in: pick a workspace. Someone with only one goes straight there."""
    available = workspaces.available(request.user)
    if len(available) <= 1:
        return switch_workspace(request, available[0] if available else "academic")
    choices = [{"name": name, "label": workspaces.LABELS[name]} for name in available]
    return render(request, "registration/workspace.html", {"choices": choices, "current": workspaces.current(request)})


@staff_member_required
def switch_workspace(request, name):
    if name not in workspaces.LABELS or (name not in workspaces.available(request.user) and name != "academic"):
        raise Http404
    request.session[workspaces.SESSION_KEY] = name
    return redirect(workspaces.home_url(name, request.user))


@never_cache
@staff_member_required
def profile(request):
    """My profile: everyone keeps their own name and email (and, for employees, phone and address) up to date.
    Job details stay with HR. Never cached: the page shows personal details."""
    user = request.user
    employee = Employee.objects.select_related("department", "designation", "reports_to__user").filter(
        user=user).first()
    data = request.POST if request.method == "POST" else None
    form = ProfileForm(data, instance=user)
    contact_form = EmployeeContactForm(data, instance=employee) if employee else None
    forms = [f for f in (form, contact_form) if f is not None]

    if data is not None and all(f.is_valid() for f in forms):
        changed = [name for f in forms for name in f.changed_data if name != "current_password"]
        if not changed:
            messages.info(request, "Nothing changed.")
            return redirect("profile")
        old_email, email_changed = form.original_email, form.email_changed()
        with transaction.atomic():
            for f in forms:
                if f.has_changed():
                    f.save()
        details = "Updated own profile: " + ", ".join(changed)
        if email_changed:
            details += f" (email {old_email or 'none'} → {user.email})"
            if old_email:
                _tell_old_address(user, old_email, request)
        record(user, Action.CHANGED, user, request=request, details=details)
        messages.success(request, "Your profile is saved.")
        return redirect("profile")

    context = {
        **admin.site.each_context(request),
        "title": "My profile",
        "form": form,
        "contact_form": contact_form,
        "employee": employee,
        "roles": user.groups.order_by("name"),
    }
    return TemplateResponse(request, "registration/profile.html", context)


def _tell_old_address(user, old_email, request):
    """Let the previous address know its account now uses another email, so a takeover doesn't go unnoticed."""
    context = {"user": user, "old_email": old_email, "new_email": user.email,
               "sign_in_url": site_url(reverse("login"), request)}
    text = render_to_string("emails/email_changed.txt", context)
    try:
        send_email("user.email_changed", old_email, "Your STS Pulse email was changed", text)
    except Exception:
        logger.exception("Couldn't tell %s that the account's email changed", old_email)
