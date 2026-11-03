import calendar as pycalendar
from datetime import date, datetime, time, timedelta

from django.contrib import admin, messages
from django.core.exceptions import PermissionDenied
from django.db.models import Count, Q, Sum
from django.http import Http404, HttpResponse, HttpResponseRedirect
from django.shortcuts import get_object_or_404, redirect
from django.template.response import TemplateResponse
from django.urls import path, reverse
from django.utils import timezone
from django.utils.html import format_html, format_html_join
from django.utils.http import urlencode

from audit.services import Action, record
from learning.approvals import SELF_APPROVAL_MESSAGE, is_own, others_only

from . import export, workflow
from .scope import allowed_subjects, covers, has_no_assignments, in_scope
from .ai import (GenerationError, approved_examples, claude_status, generate_questions, start_final_paper,
                 start_question_regeneration)
from .forms import BlueprintForm, GenerateQuestionsForm, KnowledgeSourceForm
from .models import (
    AcademicEvent, AIUsage, BlueprintItem, FinalPaper, FinalQuestion, Grade, KnowledgeSource, Question,
    QuestionBlueprint, Subject, SubjectAssignment,
)
from .workflow import can_manage_final, can_review


@admin.register(Subject)
class SubjectAdmin(admin.ModelAdmin):
    list_display = ("name", "code")
    search_fields = ("name", "code")


@admin.register(Grade)
class GradeAdmin(admin.ModelAdmin):
    list_display = ("name", "order")
    list_editable = ("order",)


class SubjectScopeMixin:
    """Limit a subject-and-grade model to the user's subject assignments: the list, the subject choices, and the
    subject/grade a form may save."""

    scope_list = True

    def get_queryset(self, request):
        qs = super().get_queryset(request)
        return in_scope(qs, request.user) if self.scope_list else qs

    def formfield_for_foreignkey(self, db_field, request, **kwargs):
        if db_field.name == "subject":
            kwargs["queryset"] = allowed_subjects(request.user)
        return super().formfield_for_foreignkey(db_field, request, **kwargs)

    def get_form(self, request, obj=None, **kwargs):
        form = super().get_form(request, obj, **kwargs)
        user = request.user

        class ScopedForm(form):
            def clean(self):
                cleaned = super().clean()
                subject, grade = cleaned.get("subject"), cleaned.get("grade")
                if subject and grade and not covers(user, subject, grade):
                    self.add_error("grade", f"You aren't assigned to {subject} for {grade}.")
                return cleaned

        return ScopedForm

    def changelist_view(self, request, extra_context=None):
        if has_no_assignments(request.user):
            self.message_user(request, "You aren't assigned to any subjects yet, so there's nothing to show. Ask the "
                                       "super admin to add your subjects under Subject assignments.", messages.WARNING)
        return super().changelist_view(request, extra_context)


@admin.register(SubjectAssignment)
class SubjectAssignmentAdmin(admin.ModelAdmin):
    """Who works on which subjects. Only super admins manage this, so nobody can widen their own access."""

    list_display = ("user", "subject", "grade")
    list_filter = ("subject", "grade")
    search_fields = ("user__username", "user__first_name", "user__last_name", "subject__name")
    autocomplete_fields = ("user",)

    def has_module_permission(self, request):
        return request.user.is_superuser

    def has_view_permission(self, request, obj=None):
        return request.user.is_superuser

    def has_add_permission(self, request):
        return request.user.is_superuser

    def has_change_permission(self, request, obj=None):
        return request.user.is_superuser

    def has_delete_permission(self, request, obj=None):
        return request.user.is_superuser


@admin.register(KnowledgeSource)
class KnowledgeSourceAdmin(SubjectScopeMixin, admin.ModelAdmin):
    form = KnowledgeSourceForm
    list_display = ("title", "material_type", "subject", "grade", "chapter", "words", "is_active", "uploaded_by",
                    "created_at")
    readonly_fields = ("text_preview",)
    list_filter = ("material_type", "subject", "grade", "is_active")
    search_fields = ("title", "chapter", "topic")
    list_select_related = ("subject", "grade", "uploaded_by")

    @staticmethod
    def _owns(request, obj):
        # Teachers edit and remove the files they uploaded; HODs and the super admin manage all of them.
        user = request.user
        return obj is None or obj.uploaded_by_id == user.pk or user.is_superuser or can_review(user)

    def has_change_permission(self, request, obj=None):
        return super().has_change_permission(request, obj) and self._owns(request, obj)

    def has_delete_permission(self, request, obj=None):
        return super().has_delete_permission(request, obj) and self._owns(request, obj)

    @admin.display(description="Words")
    def words(self, obj):
        return len(obj.content_text.split()) or "—"

    @admin.display(description="Extracted text")
    def text_preview(self, obj):
        if not obj.content_text:
            return "No text found yet. Scanned PDFs have no text layer; Claude still reads them as images."
        more = " …" if len(obj.content_text) > 1500 else ""
        return format_html('<div style="white-space:pre-wrap;max-height:16em;overflow:auto">{}{}</div>',
                           obj.content_text[:1500], more)

    def save_model(self, request, obj, form, change):
        if not change:
            obj.uploaded_by = request.user
        super().save_model(request, obj, form, change)


@admin.register(Question)
class QuestionAdmin(SubjectScopeMixin, admin.ModelAdmin):
    change_list_template = "admin/questionbank/question/change_list.html"
    list_display = ("__str__", "subject", "grade", "question_type", "difficulty", "status", "ai_generated",
                    "duplicate_flag")
    list_filter = ("status", "subject", "grade", "question_type", "difficulty", "ai_generated",
                   ("duplicate_of", admin.EmptyFieldListFilter))
    search_fields = ("text", "answer", "topic", "source_reference")
    readonly_fields = ("ai_generated", "duplicate_of", "created_by", "created_at", "updated_at", "options_preview")
    actions = ("approve", "reject")
    fieldsets = (
        (None, {"fields": ("subject", "grade", "topic", "question_type", "difficulty", "status")}),
        ("Question", {"fields": ("text", "options", "options_preview", "answer", "explanation", "source_reference")}),
        ("History", {"fields": ("ai_generated", "duplicate_of", "created_by", "created_at", "updated_at"),
                     "classes": ("collapse",)}),
    )

    @admin.display(description="Duplicate?")
    def duplicate_flag(self, obj):
        if not obj.duplicate_of_id:
            return ""
        url = reverse("admin:questionbank_question_change", args=[obj.duplicate_of_id])
        return format_html('<a href="{}">Like #{}</a>', url, obj.duplicate_of_id)

    @admin.display(description="Options")
    def options_preview(self, obj):
        if not obj.options:
            return "—"
        return format_html_join("", "<div>{}. {}</div>", ((chr(65 + i), o) for i, o in enumerate(obj.options)))

    def has_approve_permission(self, request):
        return request.user.has_perm("questionbank.approve_question")

    def get_readonly_fields(self, request, obj=None):
        readonly = list(super().get_readonly_fields(request, obj))
        # Only approvers set the status, and never on a question they created (new questions start as drafts).
        if obj is None or not self.has_approve_permission(request) or is_own(request.user, obj.created_by_id):
            readonly.append("status")
        return readonly

    def save_model(self, request, obj, form, change):
        if not change:
            obj.created_by = request.user
        elif (obj.status == Question.Status.APPROVED and form.changed_data
              and not (self.has_approve_permission(request) and not is_own(request.user, obj.created_by_id))):
            # An approved question edited by someone who can't approve it goes back for review.
            obj.status = Question.Status.DRAFT
            self.message_user(request, "The question was approved before; your changes send it back to draft for "
                                       "an HOD to approve again.", messages.WARNING)
        super().save_model(request, obj, form, change)

    def _set_status(self, request, queryset, status, action):
        own = queryset.filter(created_by=request.user).count()
        updated = others_only(queryset, request.user, "created_by").update(status=status)
        if updated:
            record(request.user, action, details=f"{updated} question(s)", request=request)
        verb = "Approved" if status == Question.Status.APPROVED else "Rejected"
        self.message_user(request, f"{verb} {updated} question(s).", messages.SUCCESS if updated else messages.WARNING)
        if own:
            self.message_user(request, f"Skipped {own} question(s) you created. {SELF_APPROVAL_MESSAGE}",
                              messages.WARNING)

    @admin.action(description="Approve selected questions", permissions=["approve"])
    def approve(self, request, queryset):
        self._set_status(request, queryset, Question.Status.APPROVED, Action.QUESTIONS_APPROVED)

    @admin.action(description="Reject selected questions", permissions=["approve"])
    def reject(self, request, queryset):
        self._set_status(request, queryset, Question.Status.REJECTED, Action.QUESTIONS_REJECTED)

    def get_urls(self):
        return [
            path("generate/", self.admin_site.admin_view(self.generate_view), name="questionbank_question_generate"),
            *super().get_urls(),
        ]

    def generate_view(self, request):
        if not self.has_add_permission(request):
            raise PermissionDenied

        # Links from a question paper pre-fill its subject and grade (?subject=1&grade=2).
        initial = {k: request.GET[k] for k in ("subject", "grade", "topic") if request.GET.get(k)}
        form = GenerateQuestionsForm(request.POST or None, initial=initial, user=request.user)
        if request.method == "POST" and form.is_valid():
            data = form.cleaned_data
            examples = approved_examples(subject=data["subject"], grade=data["grade"],
                                         question_type=data["question_type"], difficulty=data["difficulty"],
                                         topic=data["topic"])
            try:
                created = generate_questions(**data, user=request.user, examples=examples)
            except GenerationError as exc:
                form.add_error(None, str(exc))
            else:
                record(request.user, Action.QUESTIONS_GENERATED, request=request,
                       details=f"{len(created)} {data['question_type']} question(s), {data['subject']} / {data['grade']}"
                               + (f", topic: {data['topic']}" if data['topic'] else "")
                               + (f", {len(examples)} approved example(s)" if examples else ""))
                dups = sum(1 for q in created if q.duplicate_of_id)
                self.message_user(
                    request,
                    f"Generated {len(created)} {data['subject']} question(s) for {data['grade']}"
                    + (f", following {len(examples)} approved question(s) from your bank as examples" if examples
                       else "")
                    + ". They're saved as drafts. Review them and approve the ones you want to keep."
                    + (f" {dups} look like questions already in the bank; see the “Duplicate?” column." if dups else ""),
                    messages.WARNING if dups else messages.SUCCESS,
                )
                changelist = reverse("admin:questionbank_question_changelist")
                query = urlencode({
                    "subject__id__exact": data["subject"].pk,
                    "grade__id__exact": data["grade"].pk,
                    "status__exact": Question.Status.DRAFT,
                })
                return redirect(f"{changelist}?{query}")

        context = {
            **self.admin_site.each_context(request),
            "opts": self.model._meta,
            "title": "Generate questions with AI",
            "form": form,
            "sources_by_pair": self._source_counts(request.user),
            "can_add_source": request.user.has_perm("questionbank.add_knowledgesource"),
            "claude": claude_status(),
        }
        return TemplateResponse(request, "admin/questionbank/question/generate.html", context)

    @staticmethod
    def _source_counts(user):
        counts = {}
        active = in_scope(KnowledgeSource.objects.filter(is_active=True), user)
        for subject_id, grade_id in active.values_list("subject_id", "grade_id"):
            key = f"{subject_id}:{grade_id}"
            counts[key] = counts.get(key, 0) + 1
        return counts


# ---------- Academic calendar ----------

EVENT_COLORS = {
    AcademicEvent.EventType.TERM: "blue",
    AcademicEvent.EventType.EXAM_SERIES: "violet",
    AcademicEvent.EventType.INTERNAL_EXAM: "amber",
    AcademicEvent.EventType.QUESTION_DEADLINE: "red",
    AcademicEvent.EventType.BREAK: "teal",
    AcademicEvent.EventType.OTHER: "slate",
}


@admin.register(AcademicEvent)
class AcademicEventAdmin(admin.ModelAdmin):
    change_list_template = "admin/questionbank/academicevent/change_list.html"
    list_display = ("title", "type_badge", "qualification", "exam_series", "start_date", "end_date", "subject", "grade")
    list_filter = ("event_type", "qualification", "exam_series", "subject", "grade")
    search_fields = ("title", "description")
    date_hierarchy = "start_date"
    fieldsets = (
        (None, {"fields": ("title", "event_type", "start_date", "end_date")}),
        ("Cambridge", {"fields": ("qualification", "exam_series"),
                       "description": "Copy dates from the official Cambridge International exam timetable."}),
        ("Scope (optional)", {"fields": ("subject", "grade", "description")}),
    )

    @admin.display(description="Type", ordering="event_type")
    def type_badge(self, obj):
        return format_html('<span class="cal-chip cal-chip--{}">{}</span>', EVENT_COLORS[obj.event_type],
                           obj.get_event_type_display())

    def get_urls(self):
        return [path("calendar/", self.admin_site.admin_view(self.calendar_view), name="questionbank_calendar"),
                *super().get_urls()]

    def calendar_view(self, request):
        if not self.has_view_permission(request):
            raise PermissionDenied
        today = timezone.localdate()
        try:
            year, month = int(request.GET.get("year", today.year)), int(request.GET.get("month", today.month))
            first = date(year, month, 1)
        except ValueError:
            first = today.replace(day=1)
        cal = pycalendar.Calendar(firstweekday=6)  # weeks start on Sunday
        weeks = cal.monthdatescalendar(first.year, first.month)
        grid_start, grid_end = weeks[0][0], weeks[-1][-1]
        events = list(
            AcademicEvent.objects.filter(start_date__lte=grid_end)
            .filter(Q(end_date__gte=grid_start) | Q(end_date__isnull=True, start_date__gte=grid_start))
            .select_related("subject", "grade")
        )
        by_day = {}
        for e in events:
            day = max(e.start_date, grid_start)
            while day <= min(e.last_day, grid_end):
                by_day.setdefault(day, []).append({"event": e, "color": EVENT_COLORS[e.event_type],
                                                   "is_start": day == e.start_date})
                day += timedelta(days=1)
        prev_month = (first - timedelta(days=1)).replace(day=1)
        next_month = (first + timedelta(days=32)).replace(day=1)
        context = {
            **self.admin_site.each_context(request),
            "opts": self.model._meta,
            "title": "Academic calendar",
            "month": first,
            "today": today,
            "weeks": [[{"date": d, "in_month": d.month == first.month, "events": by_day.get(d, [])} for d in w] for w in weeks],
            "prev_month": prev_month,
            "next_month": next_month,
            "legend": [(label, EVENT_COLORS[value]) for value, label in AcademicEvent.EventType.choices],
            "upcoming": AcademicEvent.objects.filter(start_date__gte=today).select_related("subject", "grade")[:8],
            "can_add": self.has_add_permission(request),
        }
        return TemplateResponse(request, "admin/questionbank/academicevent/calendar.html", context)


# ---------- Question blueprints ----------

class BlueprintItemInline(admin.TabularInline):
    model = BlueprintItem
    extra = 1
    fields = ("order", "section", "question_type", "difficulty", "topic", "count", "marks_each", "sample_question")

    def formfield_for_dbfield(self, db_field, request, **kwargs):
        field = super().formfield_for_dbfield(db_field, request, **kwargs)
        if db_field.name == "sample_question":
            field.widget.attrs.update(rows=2, cols=40)
        return field

    def _editable(self, request, obj):
        return obj is None or QuestionBlueprintAdmin.is_editable(request.user, obj)

    def has_add_permission(self, request, obj=None):
        return self._editable(request, obj)

    def has_change_permission(self, request, obj=None):
        return self._editable(request, obj)

    def has_delete_permission(self, request, obj=None):
        return self._editable(request, obj)

    def has_view_permission(self, request, obj=None):
        return True


@admin.register(QuestionBlueprint)
class QuestionBlueprintAdmin(SubjectScopeMixin, admin.ModelAdmin):
    scope_list = False  # get_queryset below decides who sees which blueprints
    change_form_template = "admin/questionbank/questionblueprint/change_form.html"
    form = BlueprintForm
    list_display = ("title", "subject", "grade", "assigned_to", "due_display", "marks_display", "status_badge")
    list_filter = ("status", "subject", "grade", "exam")
    search_fields = ("title", "assigned_to__username", "assigned_to__first_name", "assigned_to__last_name")
    list_select_related = ("subject", "grade", "assigned_to")
    autocomplete_fields = ("assigned_to",)
    filter_horizontal = ("materials",)
    inlines = (BlueprintItemInline,)

    @staticmethod
    def is_editable(user, bp):
        if bp.status in (QuestionBlueprint.Status.APPROVED, QuestionBlueprint.Status.FINALIZED):
            return False
        if bp.status == QuestionBlueprint.Status.IN_REVIEW:
            return can_review(user)
        return can_review(user) or bp.assigned_to_id == user.pk

    def get_queryset(self, request):
        qs = super().get_queryset(request)
        user = request.user
        if user.is_superuser:
            return qs
        visible = Q(assigned_to=user) | Q(created_by=user)
        if can_review(user):
            # HODs review the blueprints of the subjects they're assigned to.
            visible |= Q(pk__in=in_scope(qs, user).values("pk"))
        if can_manage_final(user):
            visible |= Q(status__in=[QuestionBlueprint.Status.APPROVED, QuestionBlueprint.Status.FINALIZED])
        return qs.filter(visible)

    def get_fieldsets(self, request, obj=None):
        main = ["title", "subject", "grade", "exam", "due_date", "duration_minutes", "language"]
        if obj is not None or can_review(request.user):
            main.insert(4, "assigned_to")
        sets = [
            (None, {"fields": main}),
            ("Samples & guidance", {"fields": ["materials", "sample_file", "instructions", "guidance"],
                                    "description": "Add each section below with an optional sample question. Claude "
                                                   "writes new questions in that style; samples are never reused."}),
        ]
        if obj:
            sets.append(("Review", {"fields": ["status", "totals", "review_note", "reviewed_by", "reviewed_at",
                                               "submitted_at"]}))
        return sets

    def get_readonly_fields(self, request, obj=None):
        always = ["status", "totals", "review_note", "reviewed_by", "reviewed_at", "submitted_at"]
        if obj is None:
            return always
        if not self.is_editable(request.user, obj):
            return always + ["title", "subject", "grade", "exam", "assigned_to", "due_date", "duration_minutes",
                             "language", "materials", "sample_file", "instructions", "guidance"]
        return always + ([] if can_review(request.user) else ["assigned_to"])

    def has_change_permission(self, request, obj=None):
        if obj is not None and not self.is_editable(request.user, obj):
            return False
        return super().has_change_permission(request, obj)

    def formfield_for_foreignkey(self, db_field, request, **kwargs):
        if db_field.name == "assigned_to":
            from django.contrib.auth import get_user_model
            kwargs["queryset"] = get_user_model().objects.filter(is_active=True, is_staff=True)
        return super().formfield_for_foreignkey(db_field, request, **kwargs)

    def get_changeform_initial_data(self, request):
        initial = super().get_changeform_initial_data(request)
        initial.setdefault("assigned_to", request.user.pk)
        return initial

    def save_model(self, request, obj, form, change):
        if not change:
            obj.created_by = request.user
            if not can_review(request.user):
                obj.assigned_to = request.user
        super().save_model(request, obj, form, change)
        if not change:
            workflow.notify_assigned(obj, request.user)

    @admin.display(description="Totals")
    def totals(self, obj):
        return f"{obj.total_questions} questions · {obj.total_marks} marks"

    @admin.display(description="Marks")
    def marks_display(self, obj):
        return obj.total_marks

    @admin.display(description="Due", ordering="due_date")
    def due_display(self, obj):
        late = obj.status in QuestionBlueprint.EDITABLE_BY_AUTHOR and obj.due_date < timezone.localdate()
        return format_html('<span class="{}">{}</span>', "due-late" if late else "", obj.due_date.strftime("%d %b %Y"))

    @admin.display(description="Status", ordering="status")
    def status_badge(self, obj):
        return format_html('<span class="hr-badge hr-badge--bp-{}">{}</span>', obj.status, obj.get_status_display())

    ACTIONS = {"_submit_review": "submit", "_approve": "approve", "_return": "return", "_generate_final": "generate"}

    def changeform_view(self, request, object_id=None, form_url="", extra_context=None):
        action = next((name for key, name in self.ACTIONS.items() if key in request.POST), None)
        if request.method == "POST" and object_id and action:
            return self._run_action(request, object_id, action)
        extra_context = extra_context or {}
        if object_id:
            bp = self.get_object(request, object_id)
            if bp:
                user = request.user
                may_open = workflow.can_open_final(user, bp)
                final = FinalPaper.objects.filter(blueprint=bp).first() if may_open else None
                extra_context.update({
                    "bp": bp,
                    "can_submit": bp.status in QuestionBlueprint.EDITABLE_BY_AUTHOR
                    and (bp.assigned_to_id == user.pk or can_review(user)),
                    "can_review_now": bp.status == QuestionBlueprint.Status.IN_REVIEW and can_review(user)
                    and not workflow.reviews_own(bp, user),
                    "can_generate": may_open and bp.status == QuestionBlueprint.Status.APPROVED
                    and (final is None or final.status != FinalPaper.Status.LOCKED),
                    "final": final,
                    "is_controller": may_open,
                    "claude": claude_status(),
                })
        return super().changeform_view(request, object_id, form_url, extra_context)

    def _run_action(self, request, object_id, action):
        bp = self.get_object(request, object_id)
        if bp is None:
            raise PermissionDenied
        user, note = request.user, request.POST.get("review_note", "")
        try:
            if action == "submit":
                if not (bp.assigned_to_id == user.pk or can_review(user)):
                    raise PermissionDenied
                workflow.submit(bp, user)
                msg = "Submitted for HOD review. You'll get a notification when it's approved or returned."
            elif action == "approve":
                if not can_review(user):
                    raise PermissionDenied
                workflow.approve(bp, user, note)
                msg = "Approved. The exam controller has been notified to prepare the final paper."
            elif action == "return":
                if not can_review(user):
                    raise PermissionDenied
                workflow.return_to_author(bp, user, note)
                msg = "Returned to the teacher with your comments."
            else:
                if not can_manage_final(user) or bp.status != QuestionBlueprint.Status.APPROVED:
                    raise PermissionDenied
                start_final_paper(bp, user)  # refuses people who wrote, edited or reviewed this blueprint
                msg = ("Claude is writing the final paper. It takes a few minutes; you'll get a notification "
                       "when it's ready.")
        except (workflow.WorkflowError, GenerationError) as exc:
            self.message_user(request, str(exc), messages.ERROR)
        else:
            self.message_user(request, msg, messages.SUCCESS)
        return HttpResponseRedirect(reverse("admin:questionbank_questionblueprint_change", args=[bp.pk]))


# ---------- Confidential final papers (exam controller + superadmin only) ----------

class FinalQuestionInline(admin.StackedInline):
    model = FinalQuestion
    extra = 0
    fields = (("order", "section", "question_type", "marks"), "text", "options", "answer", "explanation", "source_reference",
              "check_notes")
    readonly_fields = ("check_notes",)

    def _editable(self, request, obj):
        return (obj is not None and workflow.can_open_final(request.user, obj.blueprint)
                and obj.status != FinalPaper.Status.LOCKED)

    def has_view_permission(self, request, obj=None):
        return can_manage_final(request.user) if obj is None else workflow.can_open_final(request.user, obj.blueprint)

    def has_add_permission(self, request, obj=None):
        return self._editable(request, obj)

    def has_change_permission(self, request, obj=None):
        return self._editable(request, obj)

    def has_delete_permission(self, request, obj=None):
        return self._editable(request, obj)


@admin.register(FinalPaper)
class FinalPaperAdmin(admin.ModelAdmin):
    """Every permission check goes through can_manage_final, so HODs and teachers can't open these pages
    even if their role has every other question bank permission."""

    change_form_template = "admin/questionbank/finalpaper/change_form.html"
    list_display = ("blueprint", "subject", "grade", "question_count", "marks", "status", "generated_at")
    list_filter = ("status", "blueprint__subject", "blueprint__grade")
    list_select_related = ("blueprint__subject", "blueprint__grade")
    readonly_fields = ("blueprint", "status", "model_used", "generated_by", "generated_at", "locked_by", "locked_at")
    fields = ("blueprint", "status", "model_used", "generated_by", "generated_at", "locked_by", "locked_at",
              "controller_notes")
    inlines = (FinalQuestionInline,)

    # Hand edits after generation aren't covered by the automatic checks; say so on the question.
    CHECKED_FIELDS = {"text", "options", "answer"}
    EDITED_NOTE = "Edited after the automatic checks. Check it by hand."
    ADDED_NOTE = "Added by hand after the automatic checks. Check it by hand."

    def save_formset(self, request, form, formset, change):
        if formset.model is FinalQuestion:
            for f in formset.forms:
                if not f.has_changed() or f in formset.deleted_forms:
                    continue
                if f.instance.pk is None:
                    f.instance.check_notes = self.ADDED_NOTE
                elif self.CHECKED_FIELDS.intersection(f.changed_data):
                    f.instance.check_notes = self.EDITED_NOTE
        super().save_formset(request, form, formset, change)

    def get_queryset(self, request):
        # Papers of blueprints this user wrote, edited or reviewed aren't listed or reachable at all.
        return super().get_queryset(request).exclude(blueprint__in=workflow.involved_blueprint_ids(request.user))

    def has_module_permission(self, request):
        return can_manage_final(request.user)

    def has_view_permission(self, request, obj=None):
        return can_manage_final(request.user) if obj is None else workflow.can_open_final(request.user, obj.blueprint)

    def has_change_permission(self, request, obj=None):
        if obj is None:
            return can_manage_final(request.user)
        return workflow.can_open_final(request.user, obj.blueprint) and obj.status != FinalPaper.Status.LOCKED

    def has_add_permission(self, request):
        return False  # created from an approved blueprint

    def has_delete_permission(self, request, obj=None):
        return False

    @admin.display(description="Subject")
    def subject(self, obj):
        return obj.blueprint.subject

    @admin.display(description="Grade")
    def grade(self, obj):
        return obj.blueprint.grade

    @admin.display(description="Questions")
    def question_count(self, obj):
        return obj.questions.count()

    @admin.display(description="Marks")
    def marks(self, obj):
        return obj.total_marks

    def changeform_view(self, request, object_id=None, form_url="", extra_context=None):
        if not can_manage_final(request.user):
            raise PermissionDenied
        if request.method == "POST" and object_id and {"_lock", "_regenerate", "_regenerate_one"} & set(request.POST):
            paper = get_object_or_404(self.get_queryset(request), pk=object_id)
            try:
                if "_lock" in request.POST:
                    workflow.lock_final(paper, request.user)
                    self.message_user(request, "Locked. This is now the final paper.", messages.SUCCESS)
                elif "_regenerate_one" in request.POST:
                    try:
                        order = int(request.POST.get("question_order", ""))
                    except ValueError:
                        raise GenerationError("Choose a question number.")
                    if not paper.questions.filter(order=order).exists():
                        raise GenerationError(f"This paper has no question {order}.")
                    start_question_regeneration(paper, order, request.user)
                    self.message_user(request, f"Claude is writing a new question {order}. You'll get a "
                                               "notification when it's ready.", messages.SUCCESS)
                else:
                    start_final_paper(paper.blueprint, request.user)
                    self.message_user(request, "Claude is writing a new set of questions. It takes a few minutes; "
                                               "you'll get a notification when it's ready.", messages.SUCCESS)
            except (workflow.WorkflowError, GenerationError) as exc:
                self.message_user(request, str(exc), messages.ERROR)
            return HttpResponseRedirect(reverse("admin:questionbank_finalpaper_change", args=[paper.pk]))
        final = self.get_object(request, object_id) if object_id else None
        extra_context = {**(extra_context or {}), "final": final,
                         "paper_problems": workflow.paper_problems(final) if final else []}
        return super().changeform_view(request, object_id, form_url, extra_context)

    def get_urls(self):
        return [path("<int:pk>/print/", self.admin_site.admin_view(self.print_view), name="questionbank_final_print"),
                path("<int:pk>/export/<str:fmt>/", self.admin_site.admin_view(self.export_view),
                     name="questionbank_final_export"),
                *super().get_urls()]

    def _paper_for_export(self, request, pk):
        if not can_manage_final(request.user):
            raise PermissionDenied
        paper = get_object_or_404(FinalPaper.objects.select_related("blueprint__subject", "blueprint__grade",
                                                                    "blueprint__exam"), pk=pk)
        if not workflow.can_open_final(request.user, paper.blueprint):
            raise PermissionDenied
        return paper

    def print_view(self, request, pk):
        paper = self._paper_for_export(request, pk)
        answers = request.GET.get("answers") == "1"
        record(request.user, Action.FINAL_EXPORTED, paper, request=request,
               details="Printed with answer key" if answers else "Printed question paper")
        return TemplateResponse(request, "questionbank/final_print.html", export.paper_context(paper, answers))

    EXPORTS = {
        "pdf": (export.export_pdf, "application/pdf"),
        "docx": (export.export_docx, "application/vnd.openxmlformats-officedocument.wordprocessingml.document"),
    }

    def export_view(self, request, pk, fmt):
        """Download the locked paper or its answer key as PDF or Word."""
        paper = self._paper_for_export(request, pk)
        if fmt not in self.EXPORTS:
            raise Http404
        if paper.status != FinalPaper.Status.LOCKED:
            self.message_user(request, "Lock the paper before downloading it. Use the print preview to check it first.",
                              messages.ERROR)
            return HttpResponseRedirect(reverse("admin:questionbank_finalpaper_change", args=[paper.pk]))
        answers = request.GET.get("answers") == "1"
        build, content_type = self.EXPORTS[fmt]
        record(request.user, Action.FINAL_EXPORTED, paper, request=request,
               details=f"Downloaded {'answer key' if answers else 'question paper'} as {fmt.upper()}")
        response = HttpResponse(build(paper, answers), content_type=content_type)
        response["Content-Disposition"] = f'attachment; filename="{export.filename(paper, answers, fmt)}"'
        return response



@admin.register(AIUsage)
class AIUsageAdmin(admin.ModelAdmin):
    """Super admins only: token counts per Claude request, to judge cost before investing in retrieval (RAG)."""

    change_list_template = "admin/questionbank/aiusage/change_list.html"
    list_display = ("created_at", "purpose", "user", "model", "input_tokens", "output_tokens", "cache_read_tokens")
    list_filter = ("purpose", "model")

    def has_module_permission(self, request):
        return request.user.is_active and request.user.is_superuser

    def has_view_permission(self, request, obj=None):
        return self.has_module_permission(request)

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False

    def changelist_view(self, request, extra_context=None):
        # Aware start of this month in local time; avoids MySQL date functions that need time zone tables.
        start = timezone.make_aware(datetime.combine(timezone.localdate().replace(day=1), time.min))
        totals = AIUsage.objects.filter(created_at__gte=start).aggregate(
            requests=Count("id"), input=Sum("input_tokens"), output=Sum("output_tokens"))
        return super().changelist_view(request, {**(extra_context or {}), "month_totals": totals})
