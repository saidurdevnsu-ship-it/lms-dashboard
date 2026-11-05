"""Blueprint workflow: teacher/SME → HOD review → exam controller → confidential final paper.

Notifications about the final paper never include its questions, and links for teachers and HODs point to the
blueprint, which they're allowed to see.
"""

from django.db import transaction
from django.urls import reverse
from django.utils import timezone

from audit.services import Action, record
from learning.approvals import SELF_APPROVAL_MESSAGE, is_own
from notifications.models import Notification
from notifications.services import notify, users_with_perm

from .models import FinalPaper, QuestionBlueprint
from .papers import blueprint_slots
from .scope import covers

Status = QuestionBlueprint.Status
FINAL_PERM = "questionbank.manage_final_papers"


class WorkflowError(Exception):
    pass


def can_review(user):
    return user.has_perm("questionbank.review_blueprint")


def can_manage_final(user):
    return user.is_active and (user.is_superuser or user.has_perm(FINAL_PERM))


INVOLVED_MESSAGE = ("You wrote, edited or reviewed this blueprint, so you can't generate or open its confidential "
                    "final paper. Ask another exam controller.")


def involved_blueprint_ids(user):
    """Blueprints the user created, was assigned, reviewed or edited (admin history). Nobody, superusers included,
    may see the final paper of one of these: they'd know the exam in advance."""
    from django.contrib.admin.models import LogEntry
    from django.contrib.contenttypes.models import ContentType
    from django.db.models import Q

    ids = set(QuestionBlueprint.objects.filter(Q(created_by=user) | Q(assigned_to=user) | Q(reviewed_by=user))
              .values_list("pk", flat=True))
    edited = LogEntry.objects.filter(user=user, content_type=ContentType.objects.get_for_model(QuestionBlueprint))
    ids |= {int(pk) for pk in edited.values_list("object_id", flat=True) if pk and pk.isdigit()}
    return ids


def involved_in(bp, user):
    return bp.pk in involved_blueprint_ids(user)


def can_open_final(user, bp):
    """Whether the user may generate or see the final paper for this blueprint."""
    return can_manage_final(user) and not involved_in(bp, user)


def _url(bp):
    return reverse("admin:questionbank_questionblueprint_change", args=[bp.pk])


def _name(user):
    return user.get_full_name() or user.get_username()


def notify_assigned(bp, by):
    if bp.assigned_to_id and bp.assigned_to != by:
        notify(bp.assigned_to, f"New question blueprint assigned: {bp.title}",
               body=f"{bp.subject} · {bp.grade} · due {bp.due_date:%d %b %Y}. Upload sample questions and the paper structure.",
               url=_url(bp), kind=Notification.Kind.REMINDER)


def problems_before_submit(bp):
    problems = []
    if not bp.items.exists():
        problems.append("Add at least one section (question type, how many, marks) before submitting.")
    return problems


@transaction.atomic
def submit(bp, user):
    if bp.status not in QuestionBlueprint.EDITABLE_BY_AUTHOR:
        raise WorkflowError("Only draft or returned blueprints can be submitted.")
    problems = problems_before_submit(bp)
    if problems:
        raise WorkflowError(" ".join(problems))
    bp.status = Status.IN_REVIEW
    bp.submitted_at = timezone.now()
    bp.save()
    record(user, Action.BLUEPRINT_SUBMITTED, bp)
    reviewers = [u for u in users_with_perm("questionbank.review_blueprint") if covers(u, bp.subject, bp.grade)]
    notify(reviewers, f"Blueprint to review: {bp.title}",
           body=f"From {_name(user)} · {bp.total_questions} questions · {bp.total_marks} marks.",
           url=_url(bp), kind=Notification.Kind.APPROVAL, exclude=user)


def reviews_own(bp, user):
    """A HOD can't review a blueprint they wrote themselves; another HOD must."""
    return is_own(user, bp.assigned_to_id)


@transaction.atomic
def approve(bp, user, note=""):
    if bp.status != Status.IN_REVIEW:
        raise WorkflowError("Only blueprints waiting for review can be approved.")
    if reviews_own(bp, user):
        raise WorkflowError(SELF_APPROVAL_MESSAGE)
    bp.status = Status.APPROVED
    bp.reviewed_by, bp.reviewed_at = user, timezone.now()
    if note:
        bp.review_note = note
    bp.save()
    record(user, Action.BLUEPRINT_APPROVED, bp, details=note)
    notify(bp.assigned_to, f"Blueprint approved: {bp.title}",
           body="It's with the exam controller, who will prepare the final paper.",
           url=_url(bp), kind=Notification.Kind.SUCCESS, exclude=user)
    notify(users_with_perm(FINAL_PERM), f"Blueprint ready for the final paper: {bp.title}",
           body=f"{bp.subject} · {bp.grade} · approved by {_name(user)}. Generate the final paper with AI.",
           url=_url(bp), kind=Notification.Kind.APPROVAL, exclude=user)


@transaction.atomic
def return_to_author(bp, user, note):
    if bp.status != Status.IN_REVIEW:
        raise WorkflowError("Only blueprints waiting for review can be returned.")
    if reviews_own(bp, user):
        raise WorkflowError(SELF_APPROVAL_MESSAGE)
    if not note.strip():
        raise WorkflowError("Add a comment so the teacher knows what to change.")
    bp.status = Status.RETURNED
    bp.reviewed_by, bp.reviewed_at, bp.review_note = user, timezone.now(), note
    bp.save()
    record(user, Action.BLUEPRINT_RETURNED, bp, details=note)
    notify(bp.assigned_to, f"Changes requested: {bp.title}", body=note,
           url=_url(bp), kind=Notification.Kind.WARNING, exclude=user)


def paper_problems(paper):
    """Ways the paper no longer matches its blueprint, e.g. after questions were added, deleted or re-marked by
    hand. A paper is only locked when this is empty."""
    slots = blueprint_slots(list(paper.blueprint.items.all()))
    questions = list(paper.questions.order_by("order", "id"))
    problems = []
    if len(questions) != len(slots):
        problems.append(f"The blueprint asks for {len(slots)} questions; this paper has {len(questions)}.")
    for n, (q, slot) in enumerate(zip(questions, slots), 1):
        if (q.section, q.question_type, q.marks) != (slot.section, slot.question_type, slot.marks):
            problems.append(f"Question {n} should be {slot.type_label} in {slot.section} for {slot.marks} mark(s).")
    expected, actual = sum(s.marks for s in slots), sum(q.marks for q in questions)
    if actual != expected:
        problems.append(f"Total marks are {actual}; the blueprint's total is {expected}.")
    return problems


@transaction.atomic
def lock_final(paper, user):
    """Lock the copy that is in the database, not the one the page was rendered from: the row is read again
    under select_for_update and every rule is re-checked on it, so a paper that changed while the page was open
    (a generation run started, a question was deleted) is not locked by mistake and no stale field is written
    back. Only the lock fields are saved."""
    current = FinalPaper.objects.select_for_update().select_related("blueprint").get(pk=paper.pk)
    if current.status == FinalPaper.Status.LOCKED:
        raise WorkflowError("This final paper is already locked.")
    if current.is_generating:
        raise WorkflowError("The AI is still writing this paper. Lock it once it's ready.")
    if not current.questions.exists():
        raise WorkflowError("Generate the paper before locking it.")
    problems = paper_problems(current)
    if problems:
        raise WorkflowError(" ".join(problems) + " Fix these, then lock.")
    current.status = FinalPaper.Status.LOCKED
    current.locked_by, current.locked_at = user, timezone.now()
    current.save(update_fields=["status", "locked_by", "locked_at"])
    # Keep the caller's copy in step, so it doesn't look unlocked afterwards.
    paper.status, paper.locked_by, paper.locked_at = current.status, current.locked_by, current.locked_at
    record(user, Action.FINAL_LOCKED, current)
    bp = current.blueprint
    bp.status = Status.FINALIZED
    bp.save(update_fields=["status", "updated_at"])
    # Teachers and HODs learn that it's done, nothing more — and only once the lock really went through.
    transaction.on_commit(lambda: notify(
        [bp.assigned_to, bp.reviewed_by], f"Final paper prepared: {bp.title}",
        body="The exam controller has finalized the paper. Its questions stay confidential.",
        url=_url(bp), kind=Notification.Kind.SUCCESS, exclude=user), robust=True)
