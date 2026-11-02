"""Deadline reminders for question blueprints and calendar deadlines, created as in-app notifications."""

from datetime import timedelta

from django.urls import reverse
from django.utils import timezone

from notifications.models import Notification
from notifications.services import notify

from .models import AcademicEvent, FinalPaper, Question, QuestionBlueprint
from .scope import allowed_subjects, covers
from .workflow import can_manage_final

# Remind 7, 3 and 1 day(s) before a deadline, then once when it's overdue.
BUCKETS = ((1, "1d"), (3, "3d"), (7, "7d"))

# Warn exam controllers this many days before an exam whose final paper isn't locked.
EXAM_WARNING_DAYS = 7


def _starts(days_left):
    if days_left == 0:
        return "starts today"
    if days_left == 1:
        return "starts tomorrow"
    return f"starts in {days_left} days"


def _bucket(days_left):
    if days_left < 0:
        return "overdue"
    for limit, name in BUCKETS:
        if days_left <= limit:
            return name
    return None


def _when(days_left):
    if days_left < 0:
        return f"{-days_left} day{'s' if days_left != -1 else ''} overdue"
    if days_left == 0:
        return "due today"
    if days_left == 1:
        return "due tomorrow"
    return f"due in {days_left} days"


def _works_on(user, event):
    """Whether a question deadline concerns this user: events without a subject concern everyone; otherwise the
    people assigned to that subject (and grade), and anyone writing blueprints or questions for it."""
    if event.subject_id is None:
        return True
    assigned = (covers(user, event.subject, event.grade) if event.grade_id
                else allowed_subjects(user).filter(pk=event.subject_id).exists())
    return (assigned or QuestionBlueprint.objects.filter(assigned_to=user, subject_id=event.subject_id).exists()
            or Question.objects.filter(created_by=user, subject_id=event.subject_id).exists())


def ensure_reminders(user):
    """Create today's due reminders for the user. Returns the notifications that are new."""
    today = timezone.localdate()
    created = []

    for bp in QuestionBlueprint.objects.filter(assigned_to=user, status__in=QuestionBlueprint.EDITABLE_BY_AUTHOR):
        days_left = (bp.due_date - today).days
        bucket = _bucket(days_left)
        if bucket is None:
            continue
        created += notify(
            user,
            f"Question blueprint “{bp.title}”: {_when(days_left)}",
            body="Add the sections and sample questions, then submit it for HOD review so the final paper can be generated.",
            url=reverse("admin:questionbank_questionblueprint_change", args=[bp.pk]),
            kind=Notification.Kind.WARNING if bucket == "overdue" else Notification.Kind.REMINDER,
            dedupe_key=f"blueprint-due:{bp.pk}:{bucket}",
        )

    if user.has_perm("questionbank.add_question"):
        upcoming = AcademicEvent.objects.filter(
            event_type=AcademicEvent.EventType.QUESTION_DEADLINE,
            start_date__range=(today, today + timedelta(days=7)),
        ).select_related("subject", "grade")
        for event in upcoming:
            if not _works_on(user, event):
                continue
            scope = " · ".join(str(x) for x in (event.subject, event.grade) if x)
            created += notify(
                user,
                f"Question deadline: {event.title} ({_when((event.start_date - today).days)})",
                body=(f"{scope}. " if scope else "") + "Make sure your questions are generated and reviewed in time.",
                url=reverse("admin:questionbank_question_generate"),
                kind=Notification.Kind.REMINDER,
                dedupe_key=f"event-deadline:{event.pk}",
            )

    if can_manage_final(user):
        soon = (QuestionBlueprint.objects
                .filter(exam__start_date__range=(today, today + timedelta(days=EXAM_WARNING_DAYS)))
                .exclude(final_paper__status=FinalPaper.Status.LOCKED)
                .select_related("exam"))
        for bp in soon:
            days_left = (bp.exam.start_date - today).days
            created += notify(
                user,
                f"Exam paper not final: {bp.title} ({_starts(days_left)})",
                body=f"{bp.exam.title} starts on {bp.exam.start_date:%d %b %Y}. The final paper isn't locked yet.",
                url=reverse("admin:questionbank_questionblueprint_change", args=[bp.pk]),
                kind=Notification.Kind.WARNING,
                dedupe_key=f"exam-paper:{bp.pk}:{_bucket(days_left)}",
            )
    return created
