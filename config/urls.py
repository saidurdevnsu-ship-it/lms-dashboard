from django.conf import settings
from django.contrib import admin
from django.contrib.admin.views.decorators import staff_member_required
from django.contrib.auth.views import PasswordResetView
from django.urls import include, path, re_path
from django.views.generic import RedirectView
from django.views.static import serve

from learning.forms import ResetPasswordForm
from learning.views import RememberMeLoginView, choose_workspace, dashboard, home, profile, switch_workspace

admin.site.site_header = "STS LMS & HR Administration"
admin.site.site_title = "STS LMS & HR Administration"
admin.site.index_title = "STS LMS & HR Administration"

urlpatterns = [
    path("", home, name="home"),
    # One sign-in page for everyone: send Django admin's own login to the STS Pulse page (keeps ?next=).
    path("admin/login/", RedirectView.as_view(pattern_name="login", query_string=True)),
    # The admin home opens the workspace picked after sign-in (Academic dashboard or HR).
    path("admin/", dashboard),
    path("admin/", admin.site.urls),
    path("profile/", profile, name="profile"),
    path("workspace/", choose_workspace, name="choose_workspace"),
    path("workspace/<str:name>/", switch_workspace, name="switch_workspace"),
    path("accounts/login/", RememberMeLoginView.as_view(), name="login"),
    path("accounts/password_reset/", PasswordResetView.as_view(form_class=ResetPasswordForm), name="password_reset"),
    path("accounts/", include("django.contrib.auth.urls")),
    path("notifications/", include("notifications.urls")),
    # Uploaded books and sample papers: signed-in staff only, in development and on the server alike.
    re_path(r"^media/(?P<path>.*)$", staff_member_required(serve), {"document_root": settings.MEDIA_ROOT}),
]
