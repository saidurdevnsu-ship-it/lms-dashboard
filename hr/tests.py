import threading
from datetime import date, datetime, time
from io import StringIO
from unittest import mock, skipUnless
from zoneinfo import ZoneInfo

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.core.management import call_command
from django.db import connection, transaction
from django.test import TestCase, TransactionTestCase, override_settings
from django.urls import reverse
from django.utils import timezone
from django.utils.html import escape

from audit.models import AuditEvent
from notifications.models import Notification

from . import reports, services
from .models import Attendance, Department, Designation, Employee, Holiday, LeaveRequest, LeaveType, ReviewStatus

DHAKA = ZoneInfo("Asia/Dhaka")


class HRTestCase(TestCase):
    def setUp(self):
        User = get_user_model()
        self.hr = User.objects.create_superuser("hr", "hr@example.com", "Pw-12345!")
        user = User.objects.create_user("rahim", "rahim@example.com", "Pw-12345!", is_staff=True)
        self.employee = Employee.objects.create(
            user=user, employee_id="T-001", department=Department.objects.create(name="Academics"),
            designation=Designation.objects.create(name="Teacher"), joining_date=date(2025, 1, 1),
        )
        self.casual = LeaveType.objects.create(name="Casual", days_per_year=5)

    def leave(self, start, end, status=ReviewStatus.PENDING):
        return LeaveRequest.objects.create(employee=self.employee, leave_type=self.casual, start_date=start,
                                           end_date=end, reason="Family", status=status,
                                           days=len(services.working_days(start, end)))


class LeaveBalanceTests(HRTestCase):
    # Weekends are Friday and Saturday; 2026-11-01 is a Sunday.

    def test_approval_rechecks_the_balance(self):
        first = self.leave(date(2026, 11, 1), date(2026, 11, 4))   # Sun–Wed: 4 days
        second = self.leave(date(2026, 11, 8), date(2026, 11, 11))  # 4 days; both fit on their own
        services.review_leave(first, approve=True, reviewer=self.hr)
        with self.assertRaisesMessage(services.LeaveError, "Only 1 day(s) of Casual leave are left in 2026"):
            services.review_leave(second, approve=True, reviewer=self.hr)
        second.refresh_from_db()
        self.assertEqual(second.status, ReviewStatus.PENDING)

    def test_leave_over_new_year_counts_in_each_year(self):
        self.leave(date(2026, 12, 29), date(2027, 1, 3), ReviewStatus.APPROVED)  # Tue 29–Thu 31, Sun 3
        self.assertEqual(services.leave_used(self.employee, self.casual, 2026), 3)
        self.assertEqual(services.leave_used(self.employee, self.casual, 2027), 1)

    def test_hr_manager_cannot_review_own_leave(self):
        self.employee.user = self.hr
        self.employee.save()
        leave = self.leave(date(2026, 11, 1), date(2026, 11, 1))
        for approve in (True, False):
            with self.assertRaisesMessage(services.LeaveError, "your own work"):
                services.review_leave(leave, approve=approve, reviewer=self.hr)
        leave.refresh_from_db()
        self.assertEqual(leave.status, ReviewStatus.PENDING)

    def test_cancelling_approved_leave_clears_attendance_and_restores_balance(self):
        leave = self.leave(date(2026, 11, 1), date(2026, 11, 2))
        services.review_leave(leave, approve=True, reviewer=self.hr)
        self.assertEqual(Attendance.objects.filter(status=Attendance.Status.ON_LEAVE).count(), 2)
        services.cancel_leave(leave, by=self.hr)
        self.assertEqual(Attendance.objects.count(), 0)
        self.assertEqual(services.leave_used(self.employee, self.casual, 2026), 0)


class DepartmentHeadTests(HRTestCase):
    def setUp(self):
        super().setUp()
        call_command("setup_hr", stdout=StringIO())
        User = get_user_model()
        self.head_user = User.objects.create_user("head", "head@example.com", "Pw-12345!", is_staff=True)
        self.head_user.groups.add(Group.objects.get(name="Employee"))
        self.head = Employee.objects.create(user=self.head_user, employee_id="T-000",
                                            department=self.employee.department,
                                            designation=self.employee.designation, joining_date=date(2025, 1, 1))
        self.employee.department.head = self.head_user
        self.employee.department.save()
        other_user = User.objects.create_user("karim", password="Pw-12345!", is_staff=True)
        self.outsider = Employee.objects.create(user=other_user, employee_id="A-001",
                                                department=Department.objects.create(name="Accounts"),
                                                designation=self.employee.designation, joining_date=date(2025, 1, 1))
        self.client.force_login(self.head_user)

    def test_head_sees_own_department_only(self):
        response = self.client.get(reverse("admin:hr_employee_changelist"))
        self.assertContains(response, "T-001")
        self.assertNotContains(response, "A-001")

    def test_head_approves_team_leave_but_not_own(self):
        team = self.leave(date(2026, 11, 1), date(2026, 11, 1))
        own = LeaveRequest.objects.create(employee=self.head, leave_type=self.casual, start_date=date(2026, 11, 2),
                                          end_date=date(2026, 11, 2), reason="Doctor", days=1)
        self.client.post(reverse("admin:hr_leaverequest_changelist"),
                         {"action": "approve_selected", "_selected_action": [team.pk, own.pk]})
        team.refresh_from_db()
        own.refresh_from_db()
        self.assertEqual(team.status, ReviewStatus.APPROVED)
        self.assertEqual(own.status, ReviewStatus.PENDING)
        entry = AuditEvent.objects.get(action=AuditEvent.Action.LEAVE_APPROVED)
        self.assertEqual((entry.actor, entry.target_id), (self.head_user, str(team.pk)))

    def test_head_cannot_see_team_attendance_or_report(self):
        Attendance.objects.create(employee=self.employee, date=date(2026, 10, 4), status=Attendance.Status.PRESENT)
        Attendance.objects.create(employee=self.head, date=date(2026, 10, 4), status=Attendance.Status.PRESENT)
        response = self.client.get(reverse("admin:hr_attendance_changelist"))
        self.assertContains(response, "T-000")      # their own
        self.assertNotContains(response, "T-001")   # their team member's
        self.assertEqual(self.client.get(reverse("admin:hr_attendance_report")).status_code, 403)

    def test_head_does_not_see_team_bank_details(self):
        response = self.client.get(reverse("admin:hr_employee_change", args=[self.employee.pk]))
        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, "bank_account")


class ReportTests(HRTestCase):
    def setUp(self):
        super().setUp()
        day = date(2026, 10, 4)  # a Sunday, a working day
        Attendance.objects.create(employee=self.employee, date=day, status=Attendance.Status.LATE,
                                  check_in=time(9, 30), check_out=time(17, 15))
        Attendance.objects.create(employee=self.employee, date=date(2026, 10, 5), status=Attendance.Status.ABSENT)
        self.leave(date(2026, 3, 1), date(2026, 3, 2), ReviewStatus.APPROVED)  # 2 casual days this year

    def test_monthly_report_counts_days_hours_and_leave(self):
        with mock.patch("hr.reports.timezone.localdate", return_value=date(2026, 10, 31)):
            report = reports.monthly_report([self.employee], 2026, 10)
        row = report["rows"][0]
        self.assertEqual((row["late"], row["absent"], row["hours"]), (1, 1, 7.75))
        self.assertEqual(row["unmarked"], report["working_days"] - 2)
        self.assertEqual(row["leave_taken"], [2])

    def test_hr_downloads_csv_and_it_is_logged(self):
        self.client.force_login(self.hr)
        response = self.client.get(reverse("admin:hr_attendance_report") + "?month=2026-10&format=csv")
        self.assertEqual(response["Content-Type"], "text/csv; charset=utf-8")
        lines = response.content.decode("utf-8-sig").splitlines()
        self.assertTrue(lines[0].startswith("Employee ID,Name,Department"))
        self.assertIn("T-001,rahim,Academics,0,1,0,1,0", lines[1])
        self.assertTrue(AuditEvent.objects.filter(action=AuditEvent.Action.REPORT_EXPORTED).exists())

    def test_employees_cannot_open_the_report(self):
        self.client.force_login(self.employee.user)
        self.assertEqual(self.client.get(reverse("admin:hr_attendance_report")).status_code, 403)


class PunchButtonTests(HRTestCase):
    def dashboard(self):
        self.client.force_login(self.employee.user)
        return self.client.get(reverse("admin:index"))

    def test_states_follow_the_day(self):
        self.assertContains(self.dashboard(), "Punch in")
        self.client.post(reverse("admin:hr_check_in"), {"next": reverse("admin:index")})
        response = self.dashboard()
        self.assertContains(response, "Punch out")
        self.assertContains(response, "Working for")
        self.client.post(reverse("admin:hr_check_out"), {"next": reverse("admin:index")})
        response = self.dashboard()
        self.assertContains(response, "Update punch-out")
        self.assertNotContains(response, ">Punch in<")

    def test_on_leave_shows_no_punch(self):
        Attendance.objects.create(employee=self.employee, date=timezone.localdate(), status=Attendance.Status.ON_LEAVE)
        response = self.dashboard()
        self.assertContains(response, "approved leave today")
        self.assertNotContains(response, reverse("admin:hr_check_in"))

    def test_punch_returns_to_the_page_it_was_pressed_on(self):
        self.client.force_login(self.employee.user)
        response = self.client.post(reverse("admin:hr_check_in"), {"next": reverse("admin:index")})
        self.assertRedirects(response, reverse("admin:index"), fetch_redirect_response=False)

    def test_no_button_without_an_employee_profile(self):
        self.client.force_login(self.hr)
        self.client.get(reverse("switch_workspace", args=["academic"]))
        self.assertNotContains(self.client.get(reverse("admin:index")), "punch__btn")

    def test_hr_page_uses_the_same_button(self):
        self.employee.user.groups.add(Group.objects.get(name="Employee"))
        self.client.force_login(self.employee.user)
        self.assertContains(self.client.get(reverse("admin:app_list", args=["hr"])), "punch--embedded")


class CheckInTests(HRTestCase):
    def check_in_at(self, hour, minute):
        now = datetime(2026, 11, 2, hour, minute, tzinfo=DHAKA)
        with mock.patch("hr.services.timezone.localtime", return_value=now):
            return services.check_in(self.employee)

    def test_check_in_after_being_marked_absent(self):
        Attendance.objects.create(employee=self.employee, date=date(2026, 11, 2), status=Attendance.Status.ABSENT)
        record, outcome = self.check_in_at(10, 0)
        self.assertEqual(outcome, "checked_in")
        self.assertEqual(record.status, Attendance.Status.LATE)
        self.assertIsNotNone(record.check_in)

    def test_no_check_in_on_leave(self):
        Attendance.objects.create(employee=self.employee, date=date(2026, 11, 2), status=Attendance.Status.ON_LEAVE)
        record, outcome = self.check_in_at(9, 0)
        self.assertEqual(outcome, "on_leave")
        self.assertIsNone(record.check_in)

    def test_second_check_in(self):
        self.check_in_at(9, 0)
        record, outcome = self.check_in_at(9, 30)
        self.assertEqual(outcome, "already")
        self.assertEqual(record.status, Attendance.Status.PRESENT)


@override_settings(MAKE_WEBHOOK_URL="", MAKE_MAILHOOK_ADDRESS="")
class LeaveReviewLockTests(HRTestCase):
    """Reviews re-read the request under a lock, so a stale copy can't review it twice, and nothing leaves the
    system until the change is saved."""

    def setUp(self):
        super().setUp()
        User = get_user_model()
        self.head_user = User.objects.create_user("head", "head@example.com", "Pw-12345!", is_staff=True)
        self.employee.department.head = self.head_user
        self.employee.department.save()
        self.stranger = User.objects.create_user("karim", "karim@example.com", "Pw-12345!", is_staff=True)

    def notifications(self):
        return Notification.objects.filter(recipient=self.employee.user).count()

    def test_stale_copy_of_a_reviewed_request_is_refused_and_changes_nothing(self):
        leave = self.leave(date(2026, 11, 1), date(2026, 11, 2))
        stale = LeaveRequest.objects.get(pk=leave.pk)
        with mock.patch("hr.services.send_event") as send_event, self.captureOnCommitCallbacks(execute=True):
            services.review_leave(leave, approve=True, reviewer=self.hr)
        for approve in (True, False):
            with mock.patch("hr.services.send_event") as again, self.captureOnCommitCallbacks(execute=True) as cbs:
                with self.assertRaisesMessage(services.LeaveError, "This request was already approved."):
                    services.review_leave(stale, approve=approve, reviewer=self.head_user)
            again.assert_not_called()
            self.assertEqual(cbs, [])
        send_event.assert_called_once()
        leave.refresh_from_db()
        self.assertEqual((leave.status, leave.reviewed_by), (ReviewStatus.APPROVED, self.hr))
        self.assertEqual(Attendance.objects.filter(status=Attendance.Status.ON_LEAVE).count(), 2)
        self.assertEqual(self.notifications(), 1)
        self.assertEqual(AuditEvent.objects.filter(action=AuditEvent.Action.LEAVE_APPROVED).count(), 1)
        self.assertEqual(AuditEvent.objects.filter(action=AuditEvent.Action.LEAVE_REJECTED).count(), 0)

    def test_review_refreshes_the_callers_copy(self):
        leave = self.leave(date(2026, 11, 1), date(2026, 11, 1))
        stale = LeaveRequest.objects.get(pk=leave.pk)
        services.cancel_leave(leave, by=self.employee.user)
        with self.assertRaisesMessage(services.LeaveError, "This request was already cancelled."):
            services.review_leave(stale, approve=False, reviewer=self.hr)
        self.assertEqual(stale.status, ReviewStatus.CANCELLED)

    def test_review_by_someone_not_allowed_is_refused_in_the_service(self):
        leave = self.leave(date(2026, 11, 1), date(2026, 11, 1))
        other_head = get_user_model().objects.create_user("otherhead", password="Pw-12345!", is_staff=True)
        Department.objects.create(name="Accounts", head=other_head)
        for user in (self.stranger, other_head):
            self.assertFalse(services.can_review_leave(user, leave))
            with self.assertRaisesMessage(services.LeaveError, "Only HR or the employee's department head"):
                services.review_leave(leave, approve=True, reviewer=user)
        leave.refresh_from_db()
        self.assertEqual(leave.status, ReviewStatus.PENDING)
        self.assertFalse(AuditEvent.objects.filter(action=AuditEvent.Action.LEAVE_APPROVED).exists())
        self.assertTrue(services.can_review_leave(self.hr, leave))
        self.assertTrue(services.can_review_leave(self.head_user, leave))
        self.assertFalse(services.can_review_leave(self.employee.user, leave))

    def test_department_head_reviews_team_leave(self):
        leave = self.leave(date(2026, 11, 1), date(2026, 11, 1))
        services.review_leave(leave, approve=False, reviewer=self.head_user, note="Exams week")
        leave.refresh_from_db()
        self.assertEqual((leave.status, leave.review_note), (ReviewStatus.REJECTED, "Exams week"))

    def test_outside_calls_wait_for_the_commit(self):
        leave = self.leave(date(2026, 11, 1), date(2026, 11, 1))
        with mock.patch("hr.services.send_event") as send_event:
            with self.captureOnCommitCallbacks(execute=False) as callbacks:
                services.review_leave(leave, approve=True, reviewer=self.hr)
            send_event.assert_not_called()
            self.assertEqual(self.notifications(), 0)
            # The audit record is part of the change itself.
            self.assertTrue(AuditEvent.objects.filter(action=AuditEvent.Action.LEAVE_APPROVED).exists())
            for callback in callbacks:
                callback()
        send_event.assert_called_once()
        self.assertEqual(send_event.call_args.args[0], "leave.approved")
        self.assertEqual(self.notifications(), 1)

    def test_rolled_back_review_sends_nothing(self):
        leave = self.leave(date(2026, 11, 1), date(2026, 11, 1))
        with mock.patch("hr.services.send_event") as send_event, self.captureOnCommitCallbacks(execute=True) as cbs:
            with self.assertRaises(RuntimeError):
                with transaction.atomic():
                    services.review_leave(leave, approve=True, reviewer=self.hr)
                    raise RuntimeError("something later failed")
        self.assertEqual(cbs, [])
        send_event.assert_not_called()
        self.assertEqual(self.notifications(), 0)
        leave.refresh_from_db()
        self.assertEqual(leave.status, ReviewStatus.PENDING)
        self.assertEqual(Attendance.objects.count(), 0)

    def test_refused_approval_sends_nothing(self):
        self.leave(date(2026, 11, 1), date(2026, 11, 4), ReviewStatus.APPROVED)   # 4 of 5 days used
        leave = self.leave(date(2026, 11, 8), date(2026, 11, 9))                  # needs 2
        with mock.patch("hr.services.send_event") as send_event, self.captureOnCommitCallbacks(execute=True) as cbs:
            with self.assertRaisesMessage(services.LeaveError, "Only 1 day(s) of Casual leave are left in 2026"):
                services.review_leave(leave, approve=True, reviewer=self.hr)
        self.assertEqual(cbs, [])
        send_event.assert_not_called()
        self.assertEqual(self.notifications(), 0)

    def test_two_requests_over_the_balance_cannot_both_be_approved_from_stale_copies(self):
        first = self.leave(date(2026, 11, 1), date(2026, 11, 4))   # 4 days
        second = self.leave(date(2026, 11, 8), date(2026, 11, 11))  # 4 days; 5 allowed
        # Both pages were opened while both requests were pending.
        first_copy, second_copy = LeaveRequest.objects.get(pk=first.pk), LeaveRequest.objects.get(pk=second.pk)
        services.review_leave(first_copy, approve=True, reviewer=self.hr)
        with self.assertRaisesMessage(services.LeaveError, "Only 1 day(s) of Casual leave are left in 2026"):
            services.review_leave(second_copy, approve=True, reviewer=self.head_user)
        self.assertEqual(LeaveRequest.objects.filter(status=ReviewStatus.APPROVED).count(), 1)
        self.assertEqual(services.leave_used(self.employee, self.casual, 2026), 4)


@override_settings(MAKE_WEBHOOK_URL="", MAKE_MAILHOOK_ADDRESS="")
class LeaveCancelRuleTests(HRTestCase):
    """Only the employee (their own pending request) or HR (pending or approved, anyone's) may cancel leave."""

    def setUp(self):
        super().setUp()
        call_command("setup_hr", stdout=StringIO())
        User = get_user_model()
        self.head_user = User.objects.create_user("head", "head@example.com", "Pw-12345!", is_staff=True)
        self.head_user.groups.add(Group.objects.get(name="Employee"))
        self.employee.department.head = self.head_user
        self.employee.department.save()
        self.employee.user.groups.add(Group.objects.get(name="Employee"))

    def cancel_in_admin(self, user, leave):
        self.client.force_login(user)
        return self.client.post(reverse("admin:hr_leaverequest_changelist"),
                                {"action": "cancel_selected", "_selected_action": [leave.pk]}, follow=True)

    def test_department_head_cannot_cancel_team_members_pending_request(self):
        leave = self.leave(date(2026, 11, 1), date(2026, 11, 1))
        self.assertFalse(services.can_cancel_leave(self.head_user, leave))
        with self.assertRaisesMessage(services.LeaveError, "Only HR or the employee who asked for this leave"):
            services.cancel_leave(leave, by=self.head_user)
        response = self.cancel_in_admin(self.head_user, leave)
        self.assertContains(response, "Not cancelled:")
        self.assertContains(response, "Cancelled 0 leave request(s).")
        leave.refresh_from_db()
        self.assertEqual(leave.status, ReviewStatus.PENDING)
        self.assertFalse(AuditEvent.objects.filter(action=AuditEvent.Action.LEAVE_CANCELLED).exists())

    def test_department_head_cannot_cancel_approved_team_leave(self):
        leave = self.leave(date(2026, 11, 1), date(2026, 11, 1))
        services.review_leave(leave, approve=True, reviewer=self.head_user)
        with self.assertRaises(services.LeaveError):
            services.cancel_leave(leave, by=self.head_user)
        leave.refresh_from_db()
        self.assertEqual(leave.status, ReviewStatus.APPROVED)
        self.assertEqual(Attendance.objects.count(), 1)

    def test_employee_withdraws_own_pending_request(self):
        leave = self.leave(date(2026, 11, 1), date(2026, 11, 1))
        self.assertTrue(services.can_cancel_leave(self.employee.user, leave))
        response = self.cancel_in_admin(self.employee.user, leave)
        self.assertContains(response, "Cancelled 1 leave request(s).")
        leave.refresh_from_db()
        self.assertEqual(leave.status, ReviewStatus.CANCELLED)
        entry = AuditEvent.objects.get(action=AuditEvent.Action.LEAVE_CANCELLED)
        self.assertEqual(entry.actor, self.employee.user)

    def test_employee_cannot_cancel_own_approved_leave(self):
        leave = self.leave(date(2026, 11, 1), date(2026, 11, 2))
        services.review_leave(leave, approve=True, reviewer=self.hr)
        self.assertFalse(services.can_cancel_leave(self.employee.user, leave))
        with self.assertRaisesMessage(services.LeaveError, "Approved leave can only be cancelled by HR"):
            services.cancel_leave(leave, by=self.employee.user)
        response = self.cancel_in_admin(self.employee.user, leave)
        self.assertContains(response, "Not cancelled:")
        leave.refresh_from_db()
        self.assertEqual(leave.status, ReviewStatus.APPROVED)
        self.assertEqual(Attendance.objects.filter(status=Attendance.Status.ON_LEAVE).count(), 2)

    def test_hr_cancels_approved_leave_and_attendance_is_cleared(self):
        leave = self.leave(date(2026, 11, 1), date(2026, 11, 2))
        services.review_leave(leave, approve=True, reviewer=self.head_user)
        self.assertTrue(services.can_cancel_leave(self.hr, leave))
        with self.captureOnCommitCallbacks(execute=True):
            response = self.cancel_in_admin(self.hr, leave)
        self.assertContains(response, "Cancelled 1 leave request(s).")
        leave.refresh_from_db()
        self.assertEqual(leave.status, ReviewStatus.CANCELLED)
        self.assertEqual(Attendance.objects.count(), 0)
        self.assertTrue(Notification.objects.filter(recipient=self.employee.user, title__startswith="Leave cancelled")
                        .exists())

    def test_someone_else_cannot_cancel(self):
        leave = self.leave(date(2026, 11, 1), date(2026, 11, 1))
        stranger = get_user_model().objects.create_user("karim", password="Pw-12345!", is_staff=True)
        with self.assertRaisesMessage(services.LeaveError, "Only HR or the employee who asked for this leave"):
            services.cancel_leave(leave, by=stranger)

    def test_stale_cancel_is_refused(self):
        leave = self.leave(date(2026, 11, 1), date(2026, 11, 1))
        stale = LeaveRequest.objects.get(pk=leave.pk)
        services.review_leave(leave, approve=False, reviewer=self.hr)
        with self.assertRaisesMessage(services.LeaveError, "This request was already rejected."):
            services.cancel_leave(stale, by=self.employee.user)
        with self.assertRaisesMessage(services.LeaveError, "This request was already rejected."):
            services.cancel_leave(stale, by=self.hr)
        self.assertFalse(AuditEvent.objects.filter(action=AuditEvent.Action.LEAVE_CANCELLED).exists())

    def test_cancelled_pending_request_sends_no_notification(self):
        leave = self.leave(date(2026, 11, 1), date(2026, 11, 1))
        with self.captureOnCommitCallbacks(execute=True):
            services.cancel_leave(leave, by=self.hr)
        self.assertFalse(Notification.objects.filter(recipient=self.employee.user).exists())


@skipUnless(connection.vendor == "mysql", "SQLite serialises writes, so only MySQL can show two reviewers racing.")
@override_settings(MAKE_WEBHOOK_URL="", MAKE_MAILHOOK_ADDRESS="")
class ConcurrentLeaveReviewTests(TransactionTestCase):
    """Two reviewers on two connections: the second waits for the first one's lock, then is refused."""

    def setUp(self):
        User = get_user_model()
        self.hr = User.objects.create_superuser("hr", "hr@example.com", "Pw-12345!")
        self.hr2 = User.objects.create_superuser("hr2", "hr2@example.com", "Pw-12345!")
        user = User.objects.create_user("rahim", "rahim@example.com", "Pw-12345!", is_staff=True)
        self.employee = Employee.objects.create(
            user=user, employee_id="T-001", department=Department.objects.create(name="Academics"),
            designation=Designation.objects.create(name="Teacher"), joining_date=date(2025, 1, 1),
        )
        self.casual = LeaveType.objects.create(name="Casual", days_per_year=5)

    def leave(self, start, end):
        return LeaveRequest.objects.create(employee=self.employee, leave_type=self.casual, start_date=start,
                                           end_date=end, reason="Family", days=len(services.working_days(start, end)))

    def race(self, first_leave_pk, second_leave_pk):
        """The first reviewer approves and holds its transaction open; the second one starts meanwhile. Returns
        (second_was_waiting, second_error)."""
        locked, release = threading.Event(), threading.Event()
        outcome = {}

        def first():
            try:
                with transaction.atomic():
                    services.review_leave(LeaveRequest.objects.get(pk=first_leave_pk), approve=True, reviewer=self.hr)
                    locked.set()
                    release.wait(10)
            finally:
                connection.close()

        def second():
            try:
                services.review_leave(LeaveRequest.objects.get(pk=second_leave_pk), approve=True, reviewer=self.hr2)
            except services.LeaveError as exc:
                outcome["error"] = str(exc)
            finally:
                connection.close()

        a = threading.Thread(target=first)
        a.start()
        self.assertTrue(locked.wait(10), "the first reviewer never got the lock")
        b = threading.Thread(target=second)
        b.start()
        b.join(1.5)
        waiting = b.is_alive()
        release.set()
        a.join(10)
        b.join(30)
        self.assertFalse(b.is_alive())
        return waiting, outcome.get("error")

    def test_second_reviewer_of_the_same_request_waits_and_is_refused(self):
        leave = self.leave(date(2026, 11, 1), date(2026, 11, 2))
        waiting, error = self.race(leave.pk, leave.pk)
        self.assertTrue(waiting, "the second reviewer didn't wait for the lock")
        self.assertEqual(error, "This request was already approved.")
        leave.refresh_from_db()
        self.assertEqual(leave.reviewed_by, self.hr)
        self.assertEqual(Attendance.objects.count(), 2)
        self.assertEqual(AuditEvent.objects.filter(action=AuditEvent.Action.LEAVE_APPROVED).count(), 1)
        self.assertEqual(Notification.objects.filter(recipient=self.employee.user).count(), 1)

    def test_two_requests_over_the_balance_cannot_both_be_approved_at_once(self):
        first = self.leave(date(2026, 11, 1), date(2026, 11, 4))   # 4 days
        second = self.leave(date(2026, 11, 8), date(2026, 11, 11))  # 4 days; 5 allowed
        waiting, error = self.race(first.pk, second.pk)
        self.assertTrue(waiting, "the second reviewer didn't wait for the employee lock")
        self.assertIn("Only 1 day(s) of Casual leave are left in 2026", error or "")
        self.assertEqual(LeaveRequest.objects.filter(status=ReviewStatus.APPROVED).count(), 1)


@override_settings(MAKE_WEBHOOK_URL="", MAKE_MAILHOOK_ADDRESS="")
class LeaveRoleEdgeCaseTests(HRTestCase):
    """Each role through the admin and the service, mixed selections, and balance edge cases at review time."""

    def setUp(self):
        super().setUp()
        call_command("setup_hr", stdout=StringIO())
        User = get_user_model()
        staff = Group.objects.get(name="Employee")
        team, designation = self.employee.department, self.employee.designation

        def employee(username, employee_id, department):
            user = User.objects.create_user(username, f"{username}@example.com", "Pw-12345!", is_staff=True)
            user.groups.add(staff)
            return Employee.objects.create(user=user, employee_id=employee_id, department=department,
                                           designation=designation, joining_date=date(2025, 1, 1))

        self.employee.user.groups.add(staff)
        self.head = employee("head", "T-000", team)
        self.head_user = self.head.user
        team.head = self.head_user
        team.save()
        self.colleague = employee("salma", "T-002", team)
        accounts = Department.objects.create(name="Accounts")
        self.outsider = employee("karim", "A-001", accounts)
        self.hr_employee = employee("hrself", "H-001", accounts)
        self.hr_employee.user = self.hr
        self.hr_employee.save()

    def request(self, employee, start, end, status=ReviewStatus.PENDING):
        return LeaveRequest.objects.create(employee=employee, leave_type=self.casual, start_date=start, end_date=end,
                                           reason="Family", status=status,
                                           days=len(services.working_days(start, end)))

    def post_action(self, user, action, *leaves):
        self.client.force_login(user)
        return self.client.post(reverse("admin:hr_leaverequest_changelist"),
                                {"action": action, "_selected_action": [leave.pk for leave in leaves]}, follow=True)

    def status(self, leave):
        leave.refresh_from_db()
        return leave.status

    # ----- Roles in the admin -----

    def test_hr_mixed_approval_approves_what_fits_and_names_each_refusal(self):
        team = self.request(self.colleague, date(2026, 11, 1), date(2026, 11, 1))
        own = self.request(self.hr_employee, date(2026, 11, 2), date(2026, 11, 2))
        self.request(self.employee, date(2026, 11, 1), date(2026, 11, 4), ReviewStatus.APPROVED)  # 4 of 5 used
        over = self.request(self.employee, date(2026, 11, 8), date(2026, 11, 9))                  # needs 2
        with mock.patch("hr.services.send_event") as send_event, self.captureOnCommitCallbacks(execute=True):
            response = self.post_action(self.hr, "approve_selected", team, own, over)
        self.assertEqual(self.status(team), ReviewStatus.APPROVED)
        self.assertEqual((self.status(own), self.status(over)), (ReviewStatus.PENDING, ReviewStatus.PENDING))
        self.assertContains(response, escape(f"Not approved: {own}. {services.SELF_APPROVAL_MESSAGE}"))
        self.assertContains(response, escape(f"Not approved: {over}. Only 1 day(s) of Casual leave are left in 2026"))
        self.assertContains(response, "Approved 1 pending leave request(s).")
        send_event.assert_called_once()
        self.assertEqual(AuditEvent.objects.filter(action=AuditEvent.Action.LEAVE_APPROVED).count(), 1)

    def test_head_approves_team_only_and_other_departments_are_untouched(self):
        team = self.request(self.employee, date(2026, 11, 1), date(2026, 11, 1))
        own = self.request(self.head, date(2026, 11, 2), date(2026, 11, 2))
        elsewhere = self.request(self.outsider, date(2026, 11, 3), date(2026, 11, 3))
        response = self.post_action(self.head_user, "approve_selected", team, own, elsewhere)
        self.assertEqual(self.status(team), ReviewStatus.APPROVED)
        self.assertEqual((self.status(own), self.status(elsewhere)), (ReviewStatus.PENDING, ReviewStatus.PENDING))
        self.assertContains(response, escape(f"Not approved: {own}. {services.SELF_APPROVAL_MESSAGE}"))
        self.assertContains(response, "Approved 1 pending leave request(s).")
        self.assertFalse(services.can_review_leave(self.head_user, elsewhere))

    def test_head_rejection_notifies_the_employee_but_sends_no_make_event(self):
        leave = self.request(self.employee, date(2026, 11, 1), date(2026, 11, 2))
        with mock.patch("hr.services.send_event") as send_event, self.captureOnCommitCallbacks(execute=True):
            response = self.post_action(self.head_user, "reject_selected", leave)
        self.assertContains(response, "Rejected 1 pending leave request(s).")
        self.assertEqual(self.status(leave), ReviewStatus.REJECTED)
        send_event.assert_not_called()
        self.assertEqual(Attendance.objects.count(), 0)
        note = Notification.objects.get(recipient=self.employee.user)
        self.assertEqual(note.kind, Notification.Kind.WARNING)
        self.assertTrue(note.title.startswith("Leave rejected"))

    def test_plain_employee_cannot_review_in_the_admin_or_the_service(self):
        leave = self.request(self.colleague, date(2026, 11, 1), date(2026, 11, 1))
        own = self.request(self.employee, date(2026, 11, 2), date(2026, 11, 2))
        self.client.force_login(self.employee.user)
        self.assertNotContains(self.client.get(reverse("admin:hr_leaverequest_changelist")), "approve_selected")
        for action in ("approve_selected", "reject_selected"):
            self.post_action(self.employee.user, action, leave, own)
        self.assertEqual((self.status(leave), self.status(own)), (ReviewStatus.PENDING, ReviewStatus.PENDING))
        with self.assertRaisesMessage(services.LeaveError, "Only HR or the employee's department head"):
            services.review_leave(leave, approve=True, reviewer=self.employee.user)
        with self.assertRaisesMessage(services.LeaveError, "your own work"):
            services.review_leave(own, approve=True, reviewer=self.employee.user)
        self.assertFalse(AuditEvent.objects.filter(action__in=[AuditEvent.Action.LEAVE_APPROVED,
                                                               AuditEvent.Action.LEAVE_REJECTED]).exists())

    def test_employee_cannot_cancel_a_colleagues_leave(self):
        leave = self.request(self.colleague, date(2026, 11, 1), date(2026, 11, 1))
        self.post_action(self.employee.user, "cancel_selected", leave)
        self.assertEqual(self.status(leave), ReviewStatus.PENDING)
        with self.assertRaisesMessage(services.LeaveError, "Only HR or the employee who asked for this leave"):
            services.cancel_leave(leave, by=self.employee.user)
        self.assertFalse(AuditEvent.objects.filter(action=AuditEvent.Action.LEAVE_CANCELLED).exists())

    def test_employee_mixed_cancel_withdraws_pending_and_names_the_approved_refusal(self):
        pending = self.request(self.employee, date(2026, 11, 1), date(2026, 11, 1))
        approved = self.request(self.employee, date(2026, 11, 2), date(2026, 11, 2))
        services.review_leave(approved, approve=True, reviewer=self.hr)
        response = self.post_action(self.employee.user, "cancel_selected", pending, approved)
        self.assertEqual((self.status(pending), self.status(approved)), (ReviewStatus.CANCELLED, ReviewStatus.APPROVED))
        self.assertContains(response, "Cancelled 1 leave request(s).")
        self.assertContains(response, escape(f"Not cancelled: {approved}. Approved leave can only be cancelled by HR"))
        self.assertEqual(Attendance.objects.filter(status=Attendance.Status.ON_LEAVE).count(), 1)

    def test_employees_stale_pending_copy_cannot_cancel_after_hr_approved(self):
        leave = self.request(self.employee, date(2026, 11, 1), date(2026, 11, 2))
        stale = LeaveRequest.objects.get(pk=leave.pk)
        services.review_leave(leave, approve=True, reviewer=self.hr)
        self.assertTrue(services.can_cancel_leave(self.employee.user, stale))   # the old page still offers it
        with self.assertRaisesMessage(services.LeaveError, "Approved leave can only be cancelled by HR"):
            services.cancel_leave(stale, by=self.employee.user)
        self.assertEqual(self.status(leave), ReviewStatus.APPROVED)
        self.assertEqual(Attendance.objects.filter(status=Attendance.Status.ON_LEAVE).count(), 2)
        self.assertFalse(AuditEvent.objects.filter(action=AuditEvent.Action.LEAVE_CANCELLED).exists())

    # ----- Balance at review time (Fri/Sat weekend, 5 casual days a year) -----

    def test_request_exactly_at_the_balance_is_approved_and_one_more_day_is_refused(self):
        full = self.request(self.employee, date(2026, 11, 1), date(2026, 11, 5))   # Sun–Thu: 5 days
        services.review_leave(full, approve=True, reviewer=self.hr)
        self.assertEqual(services.leave_used(self.employee, self.casual, 2026), 5)
        extra = self.request(self.employee, date(2026, 11, 8), date(2026, 11, 8))
        with self.assertRaisesMessage(services.LeaveError, "Only 0 day(s) of Casual leave are left in 2026, "
                                                           "but this request needs 1."):
            services.review_leave(extra, approve=True, reviewer=self.hr)
        self.assertEqual(self.status(extra), ReviewStatus.PENDING)
        self.assertEqual(Attendance.objects.count(), 5)

    def test_single_request_one_day_over_the_balance_is_refused(self):
        leave = self.request(self.employee, date(2026, 11, 1), date(2026, 11, 8))  # 6 working days
        with self.assertRaisesMessage(services.LeaveError, "Only 5 day(s) of Casual leave are left in 2026, "
                                                           "but this request needs 6."):
            services.review_leave(leave, approve=True, reviewer=self.hr)
        self.assertEqual(Attendance.objects.count(), 0)
        services.review_leave(leave, approve=False, reviewer=self.hr)   # rejecting never needs the balance
        self.assertEqual(self.status(leave), ReviewStatus.REJECTED)

    def test_weekend_and_holiday_inside_the_range_are_neither_counted_nor_marked(self):
        Holiday.objects.create(date=date(2026, 11, 8), name="Test holiday")       # a Sunday
        leave = self.request(self.employee, date(2026, 11, 1), date(2026, 11, 8))  # 8 calendar days, 5 working
        services.review_leave(leave, approve=True, reviewer=self.hr)
        self.assertEqual(self.status(leave), ReviewStatus.APPROVED)
        marked = sorted(Attendance.objects.values_list("date", flat=True))
        self.assertEqual(marked, [date(2026, 11, d) for d in (1, 2, 3, 4, 5)])
        self.assertEqual(services.leave_used(self.employee, self.casual, 2026), 5)

    def test_leave_over_new_year_is_checked_against_each_years_balance(self):
        november = self.request(self.employee, date(2026, 11, 1), date(2026, 11, 4))   # 4 days of 2026
        services.review_leave(november, approve=True, reviewer=self.hr)
        new_year = self.request(self.employee, date(2026, 12, 29), date(2027, 1, 3))   # 3 in 2026, 1 in 2027
        with self.assertRaises(services.LeaveError) as refused:
            services.review_leave(new_year, approve=True, reviewer=self.hr)
        self.assertEqual(str(refused.exception),
                         "Only 1 day(s) of Casual leave are left in 2026, but this request needs 3.")
        services.cancel_leave(november, by=self.hr)
        services.review_leave(new_year, approve=True, reviewer=self.hr)
        self.assertEqual(services.leave_used(self.employee, self.casual, 2026), 3)
        self.assertEqual(services.leave_used(self.employee, self.casual, 2027), 1)
        self.assertEqual(sorted(Attendance.objects.values_list("date", flat=True)),
                         [date(2026, 12, 29), date(2026, 12, 30), date(2026, 12, 31), date(2027, 1, 3)])
