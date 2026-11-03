from datetime import date
from io import StringIO
from pathlib import Path
from unittest import mock

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group, Permission
from django.core import mail
from django.core.management import call_command
from django.test import TestCase, override_settings
from django.urls import reverse

from audit.models import AuditEvent
from hr.models import Department, Designation, Employee
from notifications.models import Notification

from .roles import DEFAULT_ROLES, ensure_roles, keep_final_papers_confidential, role_conflict


class HomeTests(TestCase):
    def test_root_sends_visitors_to_sign_in(self):
        self.assertRedirects(self.client.get("/"), reverse("login"))

    def test_root_sends_signed_in_staff_to_dashboard(self):
        user = get_user_model().objects.create_user("staff", password="Pw-12345!", is_staff=True)
        self.client.force_login(user)
        self.assertRedirects(self.client.get("/"), reverse("admin:index"))

    def test_admin_login_uses_sts_sign_in_page(self):
        response = self.client.get("/admin/login/?next=/admin/hr/")
        self.assertRedirects(response, f"{reverse('login')}?next=/admin/hr/", fetch_redirect_response=False)


class WorkspaceTests(TestCase):
    def make_user(self, *codenames):
        user = get_user_model().objects.create_user("staff", password="Pw-12345!", is_staff=True)
        user.user_permissions.set(Permission.objects.filter(codename__in=codenames))
        return user

    def test_sign_in_with_both_workspaces_shows_the_picker(self):
        self.make_user("view_question", "view_employee")
        response = self.client.post(reverse("login"), {"username": "staff", "password": "Pw-12345!"}, follow=True)
        self.assertContains(response, "Choose a workspace")

    def test_picking_hr_makes_it_the_dashboard(self):
        self.client.force_login(self.make_user("view_question", "view_employee"))
        hr_url = reverse("admin:app_list", args=["hr"])
        self.assertRedirects(self.client.get(reverse("switch_workspace", args=["hr"])), hr_url)
        self.assertRedirects(self.client.get(reverse("admin:index")), hr_url)

    def test_picking_academic_shows_the_academic_dashboard(self):
        self.client.force_login(self.make_user("view_question", "view_employee"))
        self.client.get(reverse("switch_workspace", args=["academic"]))
        self.assertContains(self.client.get(reverse("admin:index")), "Academic workspace")

    def test_one_workspace_skips_the_picker(self):
        self.client.force_login(self.make_user("view_employee"))
        self.assertRedirects(self.client.get(reverse("choose_workspace")), reverse("admin:app_list", args=["hr"]))

    def test_cannot_switch_to_a_workspace_without_access(self):
        self.client.force_login(self.make_user("view_question"))
        self.assertEqual(self.client.get(reverse("switch_workspace", args=["hr"])).status_code, 404)

    def test_picker_shows_only_available_workspaces(self):
        self.client.force_login(self.make_user("view_question", "view_employee"))
        response = self.client.get(reverse("choose_workspace"))
        self.assertContains(response, reverse("switch_workspace", args=["hr"]))
        self.assertContains(response, reverse("switch_workspace", args=["academic"]))

    def test_academic_workspace_shows_nothing_of_hr(self):
        user = self.make_user("view_question", "view_employee", "view_subject")
        Notification.objects.create(recipient=user, title="Leave approved", url="/admin/hr/leaverequest/1/change/")
        Notification.objects.create(recipient=user, title="Blueprint due", url="/admin/questionbank/questionblueprint/1/change/")
        self.client.force_login(user)
        self.client.get(reverse("switch_workspace", args=["academic"]))
        response = self.client.get(reverse("admin:questionbank_subject_changelist"))
        self.assertNotContains(response, reverse("admin:hr_employee_changelist"))
        self.assertNotContains(response, "Leave approved")
        self.assertContains(response, "Blueprint due")
        self.assertEqual(self.client.get(reverse("notifications:feed")).json()["unread"], 1)

    def test_hr_workspace_shows_nothing_of_academic(self):
        self.client.force_login(self.make_user("view_question", "view_employee"))
        self.client.get(reverse("switch_workspace", args=["hr"]))
        response = self.client.get(reverse("admin:hr_employee_changelist"))
        self.assertNotContains(response, reverse("admin:questionbank_question_changelist"))
        self.assertContains(response, f'class="ws-bar__switch" href="{reverse("choose_workspace")}"')

    def test_opening_a_page_of_the_other_workspace_moves_you_there(self):
        self.client.force_login(self.make_user("view_question", "view_employee"))
        self.client.get(reverse("switch_workspace", args=["academic"]))
        self.client.get(reverse("admin:hr_employee_changelist"))
        self.assertRedirects(self.client.get(reverse("admin:index")), reverse("admin:app_list", args=["hr"]))

    def test_mark_all_read_leaves_the_other_workspace_unread(self):
        user = self.make_user("view_question", "view_employee")
        hr_note = Notification.objects.create(recipient=user, title="Leave approved", url="/admin/hr/leaverequest/1/change/")
        academic_note = Notification.objects.create(recipient=user, title="Blueprint due", url="/admin/questionbank/")
        self.client.force_login(user)
        self.client.get(reverse("switch_workspace", args=["academic"]))
        self.client.post(reverse("notifications:read_all"))
        hr_note.refresh_from_db()
        academic_note.refresh_from_db()
        self.assertTrue(academic_note.is_read)
        self.assertFalse(hr_note.is_read)

    def test_losing_access_to_the_saved_workspace_does_not_empty_the_dashboard(self):
        user = self.make_user("view_question", "view_employee")
        self.client.force_login(user)
        self.client.get(reverse("switch_workspace", args=["hr"]))
        user.user_permissions.remove(Permission.objects.get(codename="view_employee"))
        response = self.client.get(reverse("admin:index"))
        self.assertContains(response, reverse("admin:questionbank_question_changelist"))
        self.assertNotContains(response, "You don't have permission")

    def test_managing_users_without_hr_still_opens_users_and_roles(self):
        self.client.force_login(self.make_user("view_question", "view_user", "view_group"))
        self.client.get(reverse("switch_workspace", args=["hr"]))
        self.assertRedirects(self.client.get(reverse("admin:index")), reverse("admin:app_list", args=["auth"]))
        self.assertContains(self.client.get(reverse("admin:auth_user_changelist")), "ws-bar--hr")

    def test_new_sign_in_asks_again(self):
        self.make_user("view_question", "view_employee")
        self.client.login(username="staff", password="Pw-12345!")
        self.client.get(reverse("switch_workspace", args=["hr"]))
        self.client.post(reverse("login"), {"username": "staff", "password": "Pw-12345!"})
        self.assertRedirects(self.client.get(reverse("admin:index")), reverse("choose_workspace"))


@override_settings(MAKE_WEBHOOK_URL="", MAKE_MAILHOOK_ADDRESS="")
class ProfileTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(
            "nadia", "nadia@example.com", "Pw-12345!", first_name="Nadia", is_staff=True)
        self.client.force_login(self.user)

    def save(self, **changes):
        data = {"first_name": "Nadia", "last_name": "", "email": "nadia@example.com", **changes}
        return self.client.post(reverse("profile"), data)

    def test_header_links_to_my_profile(self):
        self.assertContains(self.client.get(reverse("admin:index")), f'href="{reverse("profile")}"')

    def test_updates_name_and_logs_it(self):
        self.assertRedirects(self.save(last_name="Rahman"), reverse("profile"))
        self.user.refresh_from_db()
        self.assertEqual(self.user.last_name, "Rahman")
        event = AuditEvent.objects.get(action=AuditEvent.Action.CHANGED, actor=self.user)
        self.assertIn("last_name", event.details)

    def test_email_change_needs_the_current_password(self):
        response = self.save(email="new@example.com")
        self.assertContains(response, "Enter your current password to change your email.")
        self.user.refresh_from_db()
        self.assertEqual(self.user.email, "nadia@example.com")
        self.save(email="new@example.com", current_password="Pw-12345!")
        self.user.refresh_from_db()
        self.assertEqual(self.user.email, "new@example.com")

    def test_email_must_be_free(self):
        get_user_model().objects.create_user("other", "taken@example.com")
        response = self.save(email="Taken@example.com", current_password="Pw-12345!")
        self.assertContains(response, "Another account already uses this email.")

    def test_employee_updates_contact_details_but_not_job_details(self):
        employee = Employee.objects.create(
            user=self.user, employee_id="T-009", department=Department.objects.create(name="Science"),
            designation=Designation.objects.create(name="Teacher"), joining_date=date(2025, 1, 1))
        self.assertContains(self.client.get(reverse("profile")), "T-009")
        self.save(phone="01700000000", address="Uttara", employee_id="HACK")
        employee.refresh_from_db()
        self.assertEqual((employee.phone, employee.address, employee.employee_id), ("01700000000", "Uttara", "T-009"))

    def test_people_without_an_employee_record_have_no_contact_fields(self):
        self.assertNotContains(self.client.get(reverse("profile")), 'name="phone"')

    def test_profile_needs_sign_in(self):
        self.client.logout()
        self.assertEqual(self.client.get(reverse("profile")).status_code, 302)

    def test_a_shared_email_does_not_block_other_changes(self):
        # Django allows two accounts with one email, and older accounts may share one already.
        get_user_model().objects.create_user("nadia2", "Nadia@example.com")
        self.assertRedirects(self.save(last_name="Rahman"), reverse("profile"))
        self.user.refresh_from_db()
        self.assertEqual(self.user.last_name, "Rahman")

    def test_profile_is_never_cached(self):
        self.assertIn("no-store", self.client.get(reverse("profile"))["Cache-Control"])

    def test_email_change_tells_the_old_address_and_logs_both(self):
        self.save(email="new@example.com", current_password="Pw-12345!")
        self.assertEqual([m.to for m in mail.outbox], [["nadia@example.com"]])
        self.assertIn("new@example.com", mail.outbox[0].body)
        event = AuditEvent.objects.get(action=AuditEvent.Action.CHANGED, actor=self.user)
        self.assertIn("nadia@example.com", event.details)
        self.assertIn("new@example.com", event.details)

    def test_wrong_password_on_email_change_is_logged(self):
        with self.assertLogs("learning.forms", "WARNING"):
            self.save(email="new@example.com", current_password="wrong")
        self.assertEqual(mail.outbox, [])

    def test_saving_without_changes_writes_nothing(self):
        with mock.patch.object(get_user_model(), "save") as save:
            response = self.client.post(reverse("profile"), {"first_name": "Nadia", "last_name": "",
                                                             "email": "nadia@example.com"}, follow=True)
        save.assert_not_called()
        self.assertContains(response, "Nothing changed.")
        self.assertFalse(AuditEvent.objects.filter(action=AuditEvent.Action.CHANGED).exists())

    def test_too_long_phone_is_a_form_error(self):
        Employee.objects.create(
            user=self.user, employee_id="T-010", department=Department.objects.create(name="Arts"),
            designation=Designation.objects.create(name="Teacher"), joining_date=date(2025, 1, 1))
        response = self.save(phone="0" * 31)
        self.assertContains(response, "at most 30 characters")


@override_settings(MAKE_WEBHOOK_URL="", MAKE_MAILHOOK_ADDRESS="")
class WelcomeEmailTests(TestCase):
    def setUp(self):
        admin = get_user_model().objects.create_superuser("boss", "boss@example.com", "Pw-12345!")
        self.client.force_login(admin)

    def add_user(self, **extra):
        data = {"username": "newbie", "first_name": "Nadia", "email": "nadia@example.com",
                "usable_password": "false", "_save": "1", **extra}
        return self.client.post(reverse("admin:auth_user_add"), data)

    def test_new_user_gets_welcome_email_without_a_password(self):
        self.add_user()
        user = get_user_model().objects.get(username="newbie")
        self.assertTrue(user.is_staff)
        self.assertEqual(len(mail.outbox), 1)
        message = mail.outbox[0]
        self.assertEqual(message.to, ["nadia@example.com"])
        self.assertIn("/accounts/reset/", message.body)
        self.assertNotIn("Pw-", message.body)

    def test_email_is_required(self):
        response = self.add_user(email="")
        self.assertEqual(response.status_code, 200)
        self.assertFalse(get_user_model().objects.filter(username="newbie").exists())
        self.assertEqual(mail.outbox, [])

    @override_settings(MAKE_WEBHOOK_URL="https://hook.example.com/abc")
    def test_welcome_email_goes_through_make_when_webhook_is_set(self):
        with mock.patch("notifications.mail.deliver") as deliver:
            self.add_user()
        payload = deliver.call_args.args[0]
        self.assertEqual(payload["event"], "user.welcome")
        self.assertEqual(payload["data"]["to"], "nadia@example.com")
        self.assertIn("/accounts/reset/", payload["data"]["set_password_url"])
        self.assertIn("/accounts/reset/", payload["data"]["html"])
        self.assertEqual(mail.outbox, [])

    @override_settings(SITE_URL="https://lms.example.com")
    def test_welcome_links_use_site_url(self):
        self.add_user()
        self.assertIn("https://lms.example.com/accounts/reset/", mail.outbox[0].body)


@override_settings(MAKE_WEBHOOK_URL="", MAKE_MAILHOOK_ADDRESS="", SITE_URL="https://lms.example.com")
class PasswordResetTests(TestCase):
    def setUp(self):
        get_user_model().objects.create_user("nadia", "nadia@example.com", "Pw-12345!", is_staff=True)

    def test_reset_email_uses_site_url(self):
        self.client.post(reverse("password_reset"), {"email": "nadia@example.com"})
        self.assertEqual(len(mail.outbox), 1)
        self.assertIn("https://lms.example.com/accounts/reset/", mail.outbox[0].body)

    @override_settings(MAKE_WEBHOOK_URL="https://hook.example.com/abc")
    def test_reset_email_goes_through_make_when_webhook_is_set(self):
        with mock.patch("notifications.mail.deliver") as deliver:
            self.client.post(reverse("password_reset"), {"email": "nadia@example.com"})
        payload = deliver.call_args.args[0]
        self.assertEqual(payload["event"], "user.password_reset")
        self.assertEqual(payload["data"]["to"], "nadia@example.com")
        self.assertIn("https://lms.example.com/accounts/reset/", payload["data"]["text"])
        self.assertEqual(mail.outbox, [])


class SignInTests(TestCase):
    def test_account_without_dashboard_access_gets_a_message_not_a_loop(self):
        get_user_model().objects.create_user("student", password="Pw-12345!")  # not staff
        response = self.client.post(reverse("login"), {"username": "student", "password": "Pw-12345!"})
        self.assertContains(response, "doesn&#x27;t have access to STS LMS")
        self.assertNotIn("_auth_user_id", self.client.session)

    def test_old_non_staff_session_is_signed_out_at_root(self):
        self.client.force_login(get_user_model().objects.create_user("student", password="Pw-12345!"))
        self.assertRedirects(self.client.get("/"), reverse("login"))
        self.assertNotIn("_auth_user_id", self.client.session)


class EscalationTests(TestCase):
    """Someone who can manage users, but isn't a super admin, must not be able to gain more power."""

    def setUp(self):
        User = get_user_model()
        self.boss = User.objects.create_superuser("boss", "boss@example.com", "Pw-12345!")
        account_perms = Permission.objects.filter(content_type__app_label="auth")
        self.user_admin_role = Group.objects.create(name="User admin")
        self.user_admin_role.permissions.set(account_perms)
        self.strong_role = Group.objects.create(name="Strong")
        self.strong_role.permissions.set(Permission.objects.filter(codename="delete_question"))
        self.manager = User.objects.create_user("manager", "m@example.com", "Pw-12345!", is_staff=True)
        self.manager.groups.add(self.user_admin_role)
        self.client.force_login(self.manager)

    def change_self(self, **extra):
        data = {"username": "manager", "first_name": "", "last_name": "", "email": "m@example.com",
                "is_active": "on", "groups": [self.user_admin_role.pk], **extra}
        return self.client.post(reverse("admin:auth_user_change", args=[self.manager.pk]), data)

    def test_cannot_make_self_super_admin(self):
        response = self.change_self(first_name="Mia", is_superuser="on", is_staff="on")
        self.assertEqual(response.status_code, 302)  # saved, but the super admin box was ignored
        self.manager.refresh_from_db()
        self.assertEqual(self.manager.first_name, "Mia")
        self.assertFalse(self.manager.is_superuser)

    def test_cannot_give_self_a_stronger_role(self):
        response = self.change_self(groups=[self.user_admin_role.pk, self.strong_role.pk])
        self.assertContains(response, "Select a valid choice")  # the stronger role isn't offered
        self.assertFalse(self.manager.groups.filter(pk=self.strong_role.pk).exists())

    def test_cannot_edit_or_reset_a_super_admin(self):
        # Changing the super admin's email would let the manager reset the password and take over the account.
        self.client.post(reverse("admin:auth_user_change", args=[self.boss.pk]),
                         {"username": "boss", "email": "manager-owns-this@example.com", "is_active": "on"})
        self.boss.refresh_from_db()
        self.assertEqual(self.boss.email, "boss@example.com")
        password_url = reverse("admin:auth_user_password_change", args=[self.boss.pk])
        self.assertEqual(self.client.get(password_url).status_code, 403)

    def test_cannot_change_what_a_role_allows(self):
        extra = Permission.objects.get(codename="delete_question")
        self.client.post(reverse("admin:auth_group_change", args=[self.user_admin_role.pk]),
                         {"name": "User admin", "permissions": [extra.pk]})
        self.assertFalse(self.user_admin_role.permissions.filter(pk=extra.pk).exists())


class PredefinedRoleTests(TestCase):
    def setUp(self):
        self.boss = get_user_model().objects.create_superuser("boss", "boss@example.com", "Pw-12345!")
        self.hod = Group.objects.get(name="Head of Department")  # created by migrate, no setup command needed
        self.delete_q = Permission.objects.get(codename="delete_question")

    def test_all_predefined_roles_exist_after_migrate(self):
        self.assertEqual(set(Group.objects.values_list("name", flat=True)) & set(DEFAULT_ROLES), set(DEFAULT_ROLES))
        self.assertTrue(self.hod.permissions.filter(codename="approve_question").exists())

    def test_edits_survive_setup_commands_until_reset(self):
        self.hod.permissions.remove(self.delete_q)
        call_command("setup_academics", stdout=StringIO())
        self.assertFalse(self.hod.permissions.filter(pk=self.delete_q.pk).exists())
        call_command("setup_academics", "--reset", stdout=StringIO())
        self.assertTrue(self.hod.permissions.filter(pk=self.delete_q.pk).exists())

    def test_reset_action_in_admin(self):
        self.hod.permissions.clear()
        self.client.force_login(self.boss)
        self.client.post(reverse("admin:auth_group_changelist"),
                         {"action": "reset_to_defaults", "_selected_action": [self.hod.pk]})
        self.assertTrue(self.hod.permissions.filter(codename="approve_question").exists())

    def test_final_paper_permission_stays_with_exam_controller(self):
        final = Permission.objects.get(codename="manage_final_papers")
        self.client.force_login(self.boss)
        perms = list(self.hod.permissions.values_list("pk", flat=True)) + [final.pk]
        self.client.post(reverse("admin:auth_group_change", args=[self.hod.pk]),
                         {"name": self.hod.name, "permissions": perms})
        self.assertFalse(self.hod.permissions.filter(pk=final.pk).exists())
        self.assertTrue(self.hod.permissions.filter(codename="approve_question").exists())  # rest was saved

    def test_admin_role_has_every_permission_including_final_papers(self):
        admin_role = Group.objects.get(name="Admin")
        self.assertEqual(admin_role.permissions.count(), Permission.objects.count())
        keep_final_papers_confidential()
        self.assertTrue(admin_role.permissions.filter(codename="manage_final_papers").exists())

    def test_subject_matter_expert_only_manages_materials(self):
        sme = Group.objects.get(name="Subject Matter Expert")
        self.assertEqual(
            set(sme.permissions.values_list("codename", flat=True)),
            {"view_subject", "view_grade", "add_knowledgesource", "change_knowledgesource",
             "delete_knowledgesource", "view_knowledgesource"})


class RoleConflictTests(TestCase):
    """Whoever sees final papers must not also write blueprints or questions, or they could read their own exam."""

    def setUp(self):
        self.boss = get_user_model().objects.create_superuser("boss", "boss@example.com", "Pw-12345!")

    def roles(self, *names):
        return Group.objects.filter(name__in=names)

    def test_final_paper_roles_cannot_be_combined_with_authoring_roles(self):
        message = role_conflict(self.roles("Exam Controller", "Teacher / Subject Expert"))
        self.assertIn("Exam Controller", message)
        self.assertIn("Teacher / Subject Expert", message)
        self.assertTrue(role_conflict(self.roles("Admin", "Head of Department")))
        self.assertIsNone(role_conflict(self.roles("Exam Controller", "Employee")))
        self.assertIsNone(role_conflict(self.roles("Teacher / Subject Expert", "Head of Department", "Employee")))
        self.assertIsNone(role_conflict(self.roles("Admin")))  # all access on its own is allowed

    def test_conflicts_are_found_by_permission_not_only_by_name(self):
        custom = Group.objects.create(name="papers team")
        custom.permissions.add(Permission.objects.get(codename="manage_final_papers"))
        writers = Group.objects.create(name="writers")
        writers.permissions.add(Permission.objects.get(codename="add_questionblueprint"))
        self.assertIn("papers team", role_conflict([custom, writers]))

    def test_roles_are_matched_ignoring_case_and_renamed_to_the_exact_name(self):
        Group.objects.filter(name="Admin").update(name="admin")
        ensure_roles(["Admin"])
        self.assertEqual(list(Group.objects.filter(name__iexact="admin").values_list("name", flat=True)), ["Admin"])

    def test_user_form_refuses_a_conflicting_combination(self):
        self.client.force_login(self.boss)
        response = self.client.post(reverse("admin:auth_user_add"), {
            "username": "mixed", "first_name": "", "last_name": "", "email": "mixed@example.com",
            "groups": [g.pk for g in self.roles("Exam Controller", "Teacher / Subject Expert")],
            "usable_password": "false"})
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "can&#x27;t also have")
        self.assertFalse(get_user_model().objects.filter(username="mixed").exists())


class SubjectMatterExpertAccessTests(TestCase):
    def setUp(self):
        from questionbank.models import Grade, Subject, SubjectAssignment

        self.sme = get_user_model().objects.create_user("sme", "sme@example.com", "Pw-12345!", is_staff=True)
        self.sme.groups.add(Group.objects.get(name="Subject Matter Expert"))
        SubjectAssignment.objects.create(user=self.sme, subject=Subject.objects.create(name="Science"),
                                         grade=Grade.objects.create(name="Class 8"))
        self.client.force_login(self.sme)

    def test_can_upload_materials_but_nothing_else_academic(self):
        self.assertEqual(self.client.get(reverse("admin:questionbank_knowledgesource_changelist")).status_code, 200)
        self.assertEqual(self.client.get(reverse("admin:questionbank_knowledgesource_add")).status_code, 200)
        for name in ("admin:questionbank_question_generate", "admin:questionbank_question_changelist",
                     "admin:questionbank_questionblueprint_changelist", "admin:questionbank_finalpaper_changelist"):
            self.assertEqual(self.client.get(reverse(name)).status_code, 403, name)


@override_settings(MAKE_WEBHOOK_URL="https://hook.example.com/abc")
class MakeEventTests(TestCase):
    def test_events_are_sent_in_the_background_without_phone_numbers(self):
        from notifications.make import send_event

        with mock.patch("notifications.make.threading.Thread") as thread, \
                self.captureOnCommitCallbacks(execute=True):
            send_event("leave.approved", {"email": "x@example.com"})
        thread.return_value.start.assert_called_once()

        source = (Path(__file__).resolve().parent.parent / "notifications" / "make.py").read_text(encoding="utf-8")
        self.assertNotIn('"phone":', source)


class MediaTests(TestCase):
    def test_uploaded_files_need_staff_sign_in(self):
        response = self.client.get("/media/knowledge/book.pdf")
        self.assertEqual(response.status_code, 302)
        self.assertIn("login", response["Location"])
