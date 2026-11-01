from django.apps import AppConfig
from django.db.models.signals import post_migrate


class LearningConfig(AppConfig):
    name = 'learning'

    def ready(self):
        from django.contrib import admin
        from django.contrib.auth.models import Group

        from . import workspaces
        from .roles import create_missing_roles

        # Staff think in terms of roles (Head of Department, Teacher...), not Django "groups".
        # Display-only; the user form and filter labels are set in learning/admin.py.
        Group._meta.verbose_name = "role"
        Group._meta.verbose_name_plural = "roles"

        # Predefined roles appear on a fresh install; existing (possibly edited) roles are left alone.
        post_migrate.connect(create_missing_roles, sender=self, dispatch_uid="learning.create_missing_roles")

        # The admin's side menu and app lists show only the workspace the user is in.
        get_app_list = admin.site.get_app_list

        def workspace_app_list(request, app_label=None):
            hidden = workspaces.hidden_apps(request)
            return [app for app in get_app_list(request, app_label) if app["app_label"] not in hidden]

        admin.site.get_app_list = workspace_app_list
