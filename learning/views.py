from django.contrib import admin
from django.contrib.admin.views.decorators import staff_member_required
from django.contrib.auth import logout
from django.contrib.auth.views import LoginView
from django.http import Http404
from django.shortcuts import redirect, render

from . import workspaces
from .forms import SignInForm


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
            return redirect(workspaces.home_url("hr"))
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
    return redirect(workspaces.home_url(name))
