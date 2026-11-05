from django import template
from django.contrib import admin
from django.db.models import Q
from django.urls import reverse
from django.utils import timezone

from hr.models import Employee
from questionbank.models import AcademicEvent, KnowledgeSource, Question, QuestionBlueprint, SubjectAssignment
from questionbank.workflow import can_manage_final, can_review

register = template.Library()


def _visible_queryset(request, model):
    """The model's admin queryset for this user, or None if they can't view it.

    Going through the ModelAdmin keeps the dashboard numbers scoped exactly like
    the changelists (e.g. teachers only count their own blueprints).
    """
    model_admin = admin.site.get_model_admin(model)
    if not model_admin.has_view_permission(request):
        return None
    return model_admin.get_queryset(request)


@register.simple_tag(takes_context=True)
def dashboard_stats(context):
    request = context["request"]
    user = request.user
    stats = {}

    hour = timezone.localtime().hour
    if hour < 12:
        stats["greeting"] = "Good morning"
    elif hour < 17:
        stats["greeting"] = "Good afternoon"
    else:
        stats["greeting"] = "Good evening"

    roles = list(user.groups.values_list("name", flat=True))
    if not user.is_superuser:
        assignments = (SubjectAssignment.objects
                       .filter(user=user)
                       .select_related("subject")
                       .values_list("subject__name", flat=True)
                       .distinct())
        subjects = list(dict.fromkeys(assignments))  # dedupe, preserve order
    else:
        subjects = []
    meta_parts = roles[:2] + subjects[:3]
    stats["user_meta"] = " · ".join(meta_parts)

    questions = _visible_queryset(request, Question)
    if questions is not None:
        stats["questions_total"] = questions.count()
        stats["questions_approved"] = questions.filter(status=Question.Status.APPROVED).count()
        stats["questions_draft"] = questions.filter(status=Question.Status.DRAFT).count()

    blueprints = _visible_queryset(request, QuestionBlueprint)
    if blueprints is not None:
        bp = blueprints.select_related("subject", "grade", "assigned_to")
        rows = list(bp.filter(Q(assigned_to=user), status__in=QuestionBlueprint.EDITABLE_BY_AUTHOR)[:6])
        if can_review(user):
            rows += list(bp.filter(status=QuestionBlueprint.Status.IN_REVIEW).exclude(pk__in=[r.pk for r in rows])[:6])
        if can_manage_final(user):
            rows += list(bp.filter(status=QuestionBlueprint.Status.APPROVED).exclude(pk__in=[r.pk for r in rows])[:6])
        today = timezone.localdate()
        for r in rows:
            r.days_left = (r.due_date - today).days
        stats["exam_prep"] = rows

    events = _visible_queryset(request, AcademicEvent)
    if events is not None:
        stats["upcoming_events"] = list(events.filter(start_date__gte=timezone.localdate())[:5])

    sources = _visible_queryset(request, KnowledgeSource)
    if sources is not None:
        stats["books_total"] = sources.filter(is_active=True).count()

    # "Needs your attention" items for the sidebar
    attention = []
    draft_count = stats.get("questions_draft", 0)
    if draft_count:
        attention.append({
            "kind": "warn",
            "label": f"{draft_count} AI-generated question{'s' if draft_count != 1 else ''} to review",
            "meta": "Question bank · drafts",
            "url": reverse("admin:questionbank_question_changelist") + "?status__exact=draft",
        })
    for bp in (stats.get("exam_prep") or [])[:4]:
        if bp.days_left <= 7:
            if bp.days_left < 0:
                due_text = f"{abs(bp.days_left)} day{'s' if abs(bp.days_left) != 1 else ''} overdue"
            elif bp.days_left == 0:
                due_text = "due today"
            else:
                due_text = f"due {bp.due_date.strftime('%d %b').lstrip('0')}"
            attention.append({
                "kind": "blue",
                "label": bp.title,
                "meta": f"{bp.subject} · {bp.grade} · {due_text}",
                "url": reverse("admin:questionbank_questionblueprint_change", args=[bp.pk]),
            })
    stats["attention"] = attention
    stats["has_employee"] = Employee.objects.filter(user=user, is_active=True).exists()

    return stats
