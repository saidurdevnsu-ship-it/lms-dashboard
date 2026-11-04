"""HR business rules: working days, leave balances, approvals and check-in."""

from collections import Counter
from datetime import date, datetime, timedelta
from decimal import Decimal

from django.conf import settings
from django.db import transaction
from django.urls import reverse
from django.utils import timezone

from audit.services import Action, record
from learning.approvals import SELF_APPROVAL_MESSAGE, is_own
from notifications.make import leave_approved_data, send_event
from notifications.models import Notification
from notifications.services import notify, users_with_perm

from .models import Attendance, Employee, Holiday, LeaveRequest, LeaveType, ReviewStatus
from .roles import is_hr_manager


# ---------- Calendar ----------

def working_days(start, end):
    """Dates from start to end (inclusive) that are neither weekend days nor holidays."""
    holidays = set(Holiday.objects.filter(date__range=(start, end)).values_list("date", flat=True))
    day, days = start, []
    while day <= end:
        if day.weekday() not in settings.HR_WEEKEND_DAYS and day not in holidays:
            days.append(day)
        day += timedelta(days=1)
    return days


def days_by_year(start, end):
    """Working days from start to end, counted per calendar year: {2026: 2, 2027: 1}."""
    return Counter(day.year for day in working_days(start, end))


# ---------- Leave ----------

class LeaveError(Exception):
    pass


def leave_used(employee, leave_type, year):
    """Approved working days of this leave type that fall in the year, so leave over New Year counts in both years."""
    first, last = date(year, 1, 1), date(year, 12, 31)
    approved = LeaveRequest.objects.filter(
        employee=employee, leave_type=leave_type, status=ReviewStatus.APPROVED, start_date__lte=last, end_date__gte=first
    )
    return Decimal(sum(len(working_days(max(l.start_date, first), min(l.end_date, last))) for l in approved))


def leave_balances(employee, year=None):
    year = year or timezone.localdate().year
    rows = []
    for lt in LeaveType.objects.all():
        used = leave_used(employee, lt, year)
        rows.append({
            "type": lt,
            "allowed": lt.days_per_year,
            "used": used,
            "remaining": (lt.days_per_year - used) if lt.days_per_year else None,
        })
    return rows


def balance_problems(leave):
    """Years in which this request needs more days than are left of the yearly allowance."""
    lt = leave.leave_type
    if not lt.days_per_year:
        return []
    problems = []
    for year, needed in sorted(days_by_year(leave.start_date, leave.end_date).items()):
        remaining = lt.days_per_year - leave_used(leave.employee, lt, year)
        if needed > remaining:
            problems.append(f"Only {remaining:g} day(s) of {lt.name} leave are left in {year}, "
                            f"but this request needs {needed}.")
    return problems


def validate_leave(leave):
    """Return a list of problems with a leave request (empty if it can be submitted)."""
    problems = []
    if not working_days(leave.start_date, leave.end_date):
        problems.append("The selected dates are all weekends or holidays, so no leave is needed.")
    overlapping = LeaveRequest.objects.filter(
        employee=leave.employee,
        status__in=[ReviewStatus.PENDING, ReviewStatus.APPROVED],
        start_date__lte=leave.end_date,
        end_date__gte=leave.start_date,
    ).exclude(pk=leave.pk)
    if overlapping.exists():
        problems.append("These dates overlap another pending or approved leave request.")
    return problems + balance_problems(leave)


def review_refusal(user, leave):
    """Why this user may not approve or reject the request, or None if they may: HR managers and the head of the
    employee's department review leave, nobody their own."""
    if is_own(user, leave.employee.user_id):
        return SELF_APPROVAL_MESSAGE
    if is_hr_manager(user) or leave.employee.department.head_id == user.pk:
        return None
    return "Only HR or the employee's department head can review this request."


def can_review_leave(user, leave):
    return review_refusal(user, leave) is None


def cancel_refusal(user, leave):
    """Why this user may not cancel the request, or None if they may. HR managers cancel pending or approved leave
    of anyone; employees may only withdraw their own request while it is still pending. Department heads review,
    but don't cancel, their team's leave."""
    if leave.status not in (ReviewStatus.PENDING, ReviewStatus.APPROVED):
        return already_done(leave)
    if is_hr_manager(user):
        return None
    if is_own(user, leave.employee.user_id):
        if leave.status == ReviewStatus.PENDING:
            return None
        return "Approved leave can only be cancelled by HR. Please ask HR if your plans have changed."
    return "Only HR or the employee who asked for this leave can cancel it."


def can_cancel_leave(user, leave):
    return cancel_refusal(user, leave) is None


def already_done(leave):
    return f"This request was already {leave.get_status_display().lower()}."


def lock_leave(leave):
    """Lock the employee row, then the request row, and reload the request in place. Always in that order, so a
    review and a cancel of the same employee's leave can't deadlock, and two of their requests can't both pass the
    balance check at once. Decisions are then made on what is saved now, not on the copy a page showed earlier.
    Call inside transaction.atomic."""
    employee_id = leave.employee_id
    list(Employee.objects.select_for_update().filter(pk=employee_id).order_by().values_list("pk", flat=True))
    try:
        leave.refresh_from_db(from_queryset=LeaveRequest.objects.select_for_update())
    except LeaveRequest.DoesNotExist:
        raise LeaveError("This request no longer exists.") from None
    if leave.employee_id != employee_id:
        raise LeaveError("This request was changed while you were looking at it. Please open it again.")


@transaction.atomic
def review_leave(leave, *, approve, reviewer, note=""):
    """Approve or reject a pending request. The request is locked and re-read first, so a request already reviewed
    by someone else is refused rather than reviewed twice. Approval checks the balance again under that lock, since
    other leave may have been approved since this request was made, and raises LeaveError if it no longer fits.
    The notification and the Make event go out only once the change is saved."""
    problem = review_refusal(reviewer, leave)
    if problem:
        raise LeaveError(problem)
    lock_leave(leave)
    if leave.status != ReviewStatus.PENDING:
        raise LeaveError(already_done(leave))
    if approve:
        problems = balance_problems(leave)
        if problems:
            raise LeaveError(" ".join(problems))
    leave.status = ReviewStatus.APPROVED if approve else ReviewStatus.REJECTED
    leave.reviewed_by = reviewer
    leave.reviewed_at = timezone.now()
    if note:
        leave.review_note = note
    leave.save()
    record(reviewer, Action.LEAVE_APPROVED if approve else Action.LEAVE_REJECTED, leave, details=note)
    if approve:
        for day in working_days(leave.start_date, leave.end_date):
            Attendance.objects.update_or_create(
                employee=leave.employee, date=day,
                defaults={"status": Attendance.Status.ON_LEAVE, "note": f"{leave.leave_type} leave"},
            )
    event_data = leave_approved_data(leave) if approve else None
    title = (f"Leave {'approved' if approve else 'rejected'}: {leave.leave_type}, "
             f"{leave.start_date:%d %b} – {leave.end_date:%d %b}")
    body, url = leave.review_note, reverse("admin:hr_leaverequest_change", args=[leave.pk])
    employee_user = leave.employee.user

    def after_commit():
        if event_data is not None:
            send_event("leave.approved", event_data)
        notify(employee_user, title, body=body, url=url,
               kind=Notification.Kind.SUCCESS if approve else Notification.Kind.WARNING, exclude=reviewer)

    transaction.on_commit(after_commit)


@transaction.atomic
def cancel_leave(leave, by):
    """Cancel a pending or approved request (see cancel_refusal for who may). The request is locked and re-read
    first, so it can't be cancelled after someone else has already changed it. For approved leave, the "on leave"
    attendance it created is removed, so the days go back into the balance."""
    problem = cancel_refusal(by, leave)
    if problem:
        raise LeaveError(problem)
    lock_leave(leave)
    problem = cancel_refusal(by, leave)  # again, on what is saved now
    if problem:
        raise LeaveError(problem)
    was_approved = leave.status == ReviewStatus.APPROVED
    leave.status = ReviewStatus.CANCELLED
    leave.save(update_fields=["status"])
    record(by, Action.LEAVE_CANCELLED, leave, details="Was approved" if was_approved else "Was pending")
    if was_approved:
        Attendance.objects.filter(
            employee=leave.employee, date__range=(leave.start_date, leave.end_date), status=Attendance.Status.ON_LEAVE
        ).delete()
        title = f"Leave cancelled: {leave.leave_type}, {leave.start_date:%d %b} – {leave.end_date:%d %b}"
        url = reverse("admin:hr_leaverequest_change", args=[leave.pk])
        employee_user = leave.employee.user
        transaction.on_commit(lambda: notify(
            employee_user, title, body="HR cancelled this approved leave. The days are back in your balance.",
            url=url, kind=Notification.Kind.WARNING, exclude=by))


def announce_request(obj, what, summary):
    """Tell HR managers and the employee's department head that a leave request is waiting."""
    head = obj.employee.department.head
    notify(users_with_perm("hr.manage_hr") + ([head] if head else []),
           f"{what} request from {obj.employee.full_name}", body=summary,
           url=reverse(f"admin:hr_{obj._meta.model_name}_change", args=[obj.pk]),
           kind=Notification.Kind.APPROVAL, exclude=obj.employee.user)


# ---------- Attendance ----------

def check_in(employee):
    """Record today's arrival. Returns (record, outcome): "checked_in", "already" or "on_leave".

    If HR marked the day before the employee arrived, the check-in time is still recorded, and "absent" becomes
    present or late.
    """
    now = timezone.localtime()
    office_start = datetime.strptime(settings.HR_OFFICE_START, "%H:%M").time()
    late_after = (datetime.combine(now.date(), office_start) + timedelta(minutes=settings.HR_LATE_GRACE_MINUTES)).time()
    arrived = now.time().replace(microsecond=0)
    status = Attendance.Status.LATE if now.time() > late_after else Attendance.Status.PRESENT
    record, created = Attendance.objects.get_or_create(
        employee=employee, date=now.date(), defaults={"check_in": arrived, "status": status},
    )
    if created:
        return record, "checked_in"
    if record.check_in:
        return record, "already"
    if record.status == Attendance.Status.ON_LEAVE:
        return record, "on_leave"
    record.check_in = arrived
    if record.status == Attendance.Status.ABSENT:
        record.status = status
    record.save(update_fields=["check_in", "status"])
    return record, "checked_in"


def check_out(employee):
    now = timezone.localtime()
    record = Attendance.objects.filter(employee=employee, date=now.date()).first()
    if record is None or record.check_in is None:
        return None
    record.check_out = now.time().replace(microsecond=0)
    record.save(update_fields=["check_out"])
    return record
