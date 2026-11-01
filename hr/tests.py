from datetime import date, datetime, time
from io import StringIO
from unittest import mock
from zoneinfo import ZoneInfo

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.core.management import call_command
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from audit.models import AuditEvent

from . import reports, services
from .models import Attendance, Department, Designation, Employee, LeaveRequest, LeaveType, ReviewStatus

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
