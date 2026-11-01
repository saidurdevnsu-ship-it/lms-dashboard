"""The two workspaces behind the one sign-in. Each user works in one at a time and sees nothing of the other.

Academic is the admin index plus the question bank; HR is the HR app page plus HR, users and roles."""

from django.urls import reverse

SESSION_KEY = "workspace"

# Admin app labels in each workspace; the first one decides who has access. Other apps (notifications,
# audit) belong to neither and show in both.
APPS = {
    "academic": ("questionbank",),
    "hr": ("hr", "auth"),
}
LABELS = {"academic": "Academic", "hr": "HR"}


def available(user):
    return [name for name, apps in APPS.items() if user.has_module_perms(apps[0])]


def current(request):
    return request.session.get(SESSION_KEY)


def home_url(name):
    return reverse("admin:app_list", kwargs={"app_label": "hr"}) if name == "hr" else reverse("admin:index")


def of_app(app_label):
    return next((name for name, apps in APPS.items() if app_label in apps), None)


def of_path(path):
    """The workspace an admin URL belongs to, e.g. /admin/hr/employee/ -> "hr"; None for shared pages."""
    prefix = reverse("admin:index")
    if not path.startswith(prefix):
        return None
    return of_app(path[len(prefix):].split("/", 1)[0])


def hidden_apps(request):
    """App labels of the workspaces the user isn't in right now."""
    ws = current(request)
    if ws is None:
        return set()
    return {label for name, apps in APPS.items() if name != ws for label in apps}


def without_hidden_links(request, queryset, field="url"):
    """Drop rows (e.g. notifications) that link into the workspace the user isn't in right now."""
    for app_label in hidden_apps(request):
        queryset = queryset.exclude(**{f"{field}__startswith": reverse("admin:app_list", args=[app_label])})
    return queryset


class WorkspaceMiddleware:
    """Opening a page of the other workspace (say, a link in an email) moves the user into that workspace,
    so the two are never shown together."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        user = getattr(request, "user", None)
        if user is not None and user.is_authenticated and user.is_staff and current(request):
            ws = of_path(request.path)
            if ws and ws != current(request) and ws in available(user):
                request.session[SESSION_KEY] = ws
        return self.get_response(request)


def context(request):
    user = getattr(request, "user", None)
    if user is None or not user.is_authenticated or not user.is_staff:
        return {}
    ws = current(request)
    return {
        "workspace": ws,
        "workspace_label": LABELS.get(ws, ""),
        "workspace_can_switch": len(available(user)) > 1,
    }
