from datetime import timedelta

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.validators import FileExtensionValidator, MinValueValidator
from django.db import models
from django.utils import timezone

from .documents import ALLOWED_EXTENSIONS as DOCUMENT_EXTENSIONS


class Subject(models.Model):
    name = models.CharField(max_length=100, unique=True)
    code = models.CharField(max_length=20, blank=True)

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return self.name


class Grade(models.Model):
    name = models.CharField(max_length=50, unique=True, help_text='e.g. "Class 8" or "Grade 10".')
    order = models.PositiveSmallIntegerField(default=0, help_text="Lower numbers are listed first.")

    class Meta:
        ordering = ["order", "name"]

    def __str__(self):
        return self.name


class SubjectAssignment(models.Model):
    """A subject (optionally one grade of it) a teacher, HOD or read-only user works on. People see and act on
    question bank material only for their assignments; super admins and exam controllers see every subject."""

    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="subject_assignments")
    subject = models.ForeignKey(Subject, on_delete=models.CASCADE, related_name="assignments")
    grade = models.ForeignKey(Grade, on_delete=models.CASCADE, null=True, blank=True, related_name="assignments",
                              help_text="Leave blank for every grade of the subject.")

    class Meta:
        ordering = ["user__username", "subject", "grade"]
        verbose_name = "subject assignment"
        constraints = [
            models.UniqueConstraint(fields=["user", "subject", "grade"], name="one_assignment_per_grade"),
            models.UniqueConstraint(fields=["user", "subject"], condition=models.Q(grade__isnull=True),
                                    name="one_all_grades_assignment"),
        ]

    def __str__(self):
        return f"{self.user} · {self.subject} · {self.grade or 'all grades'}"


class KnowledgeSource(models.Model):
    """A textbook, syllabus, lesson plan or other file that Claude reads when generating questions for a subject and
    grade."""

    MAX_UPLOAD_MB = 20

    class MaterialType(models.TextChoices):
        TEXTBOOK = "textbook", "Textbook"
        SYLLABUS = "syllabus", "Syllabus"
        LESSON_PLAN = "lesson_plan", "Lesson plan"
        SAMPLE_QUESTIONS = "sample_questions", "Sample questions / past paper"
        NOTES = "notes", "Teaching notes"

    subject = models.ForeignKey(Subject, on_delete=models.CASCADE, related_name="sources")
    grade = models.ForeignKey(Grade, on_delete=models.CASCADE, related_name="sources")
    title = models.CharField(max_length=200)
    material_type = models.CharField("type", max_length=20, choices=MaterialType.choices, default=MaterialType.TEXTBOOK)
    chapter = models.CharField(max_length=200, blank=True,
                               help_text='e.g. "Chapter 3: Photosynthesis". Leave blank if the file covers the whole book.')
    topic = models.CharField(max_length=200, blank=True, help_text="Optional. The topics this file covers.")
    file = models.FileField(
        upload_to="knowledge/%Y/%m/",
        validators=[FileExtensionValidator(DOCUMENT_EXTENSIONS)],
        help_text=f"PDF, Word (.docx), TXT or Markdown, up to {MAX_UPLOAD_MB} MB. Split very large books into chapters.",
    )
    is_active = models.BooleanField(
        default=True, help_text="Only active sources are used when generating questions."
    )
    content_text = models.TextField("extracted text", blank=True, editable=False,
                                    help_text="Read from the file on upload. Empty for scanned PDFs.")
    uploaded_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, editable=False
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["subject", "grade", "chapter", "title"]
        verbose_name = "book / knowledge source"
        verbose_name_plural = "books & knowledge sources"

    def __str__(self):
        return f"{self.title} ({self.subject}, {self.grade})"

    @property
    def label(self):
        """How the file is named to Claude, so it can tell a syllabus from a textbook chapter."""
        parts = [f"{self.get_material_type_display()}: {self.title}", self.chapter, self.topic and f"topics: {self.topic}"]
        return " · ".join(p for p in parts if p)

    @property
    def is_pdf(self):
        return self.file.name.lower().endswith(".pdf")


class Question(models.Model):
    class QuestionType(models.TextChoices):
        MCQ = "mcq", "Multiple choice"
        SHORT = "short", "Short answer"
        LONG = "long", "Long answer / creative"
        TRUE_FALSE = "true_false", "True / false"
        FILL_BLANK = "fill_blank", "Fill in the blank"

    class Difficulty(models.TextChoices):
        EASY = "easy", "Easy"
        MEDIUM = "medium", "Medium"
        HARD = "hard", "Hard"

    class Status(models.TextChoices):
        DRAFT = "draft", "Draft (needs review)"
        APPROVED = "approved", "Approved"
        REJECTED = "rejected", "Rejected"

    subject = models.ForeignKey(Subject, on_delete=models.PROTECT, related_name="questions")
    grade = models.ForeignKey(Grade, on_delete=models.PROTECT, related_name="questions")
    topic = models.CharField(max_length=200, blank=True)
    question_type = models.CharField(max_length=12, choices=QuestionType.choices)
    difficulty = models.CharField(max_length=8, choices=Difficulty.choices, default=Difficulty.MEDIUM)
    text = models.TextField("question")
    options = models.JSONField(
        default=list, blank=True, help_text='Multiple-choice options as a JSON list, e.g. ["A", "B", "C", "D"].'
    )
    answer = models.TextField()
    explanation = models.TextField(blank=True)
    source_reference = models.CharField(
        max_length=255, blank=True, help_text="Chapter / page in the book the question is based on."
    )
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.DRAFT, db_index=True)
    ai_generated = models.BooleanField(default=False, editable=False)
    duplicate_of = models.ForeignKey("self", on_delete=models.SET_NULL, null=True, blank=True, editable=False,
                                     related_name="+", help_text="An existing question this one closely resembles.")
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, editable=False
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["subject", "grade", "status"])]
        permissions = [("approve_question", "Can approve or reject questions in the bank (HOD)")]

    def __str__(self):
        return self.text if len(self.text) <= 80 else f"{self.text[:77]}..."


class AcademicEvent(models.Model):
    """An entry in the academic calendar: terms, Cambridge exam series, internal exams, deadlines and breaks."""

    class EventType(models.TextChoices):
        TERM = "term", "Term"
        EXAM_SERIES = "exam_series", "Cambridge exam series"
        INTERNAL_EXAM = "internal_exam", "Mock / internal exam"
        QUESTION_DEADLINE = "question_deadline", "Question paper deadline"
        BREAK = "break", "Holiday / break"
        OTHER = "other", "Other"

    class Series(models.TextChoices):
        FEB_MAR = "feb_mar", "February/March"
        MAY_JUN = "may_jun", "May/June"
        OCT_NOV = "oct_nov", "October/November"

    class Qualification(models.TextChoices):
        PRIMARY = "primary", "Cambridge Primary"
        LOWER_SECONDARY = "lower_secondary", "Cambridge Lower Secondary (Checkpoint)"
        IGCSE = "igcse", "Cambridge IGCSE / O Level"
        A_LEVEL = "a_level", "Cambridge International AS & A Level"

    title = models.CharField(max_length=200)
    event_type = models.CharField(max_length=20, choices=EventType.choices)
    exam_series = models.CharField(max_length=10, choices=Series.choices, blank=True,
                                   help_text="For Cambridge exam series.")
    qualification = models.CharField(max_length=20, choices=Qualification.choices, blank=True)
    start_date = models.DateField()
    end_date = models.DateField(null=True, blank=True, help_text="Leave blank for a single-day event.")
    subject = models.ForeignKey(Subject, on_delete=models.CASCADE, null=True, blank=True, related_name="events")
    grade = models.ForeignKey(Grade, on_delete=models.CASCADE, null=True, blank=True, related_name="events")
    description = models.TextField(blank=True)

    class Meta:
        ordering = ["start_date", "title"]
        verbose_name = "calendar event"
        verbose_name_plural = "academic calendar"

    def __str__(self):
        return f"{self.title} ({self.start_date:%d %b %Y})"

    def clean(self):
        super().clean()
        if self.end_date and self.start_date and self.end_date < self.start_date:
            raise ValidationError({"end_date": "The end date can't be before the start date."})

    @property
    def last_day(self):
        return self.end_date or self.start_date


class QuestionBlueprint(models.Model):
    """What an exam should look like, written by a teacher / subject expert. Claude writes the final paper from it."""

    class Status(models.TextChoices):
        DRAFT = "draft", "Draft"
        IN_REVIEW = "in_review", "Waiting for HOD review"
        RETURNED = "returned", "Returned for changes"
        APPROVED = "approved", "Approved · with exam controller"
        FINALIZED = "finalized", "Final paper prepared"

    EDITABLE_BY_AUTHOR = (Status.DRAFT, Status.RETURNED)

    title = models.CharField(max_length=200, help_text='e.g. "Science Mid-term, Class 8".')
    subject = models.ForeignKey(Subject, on_delete=models.PROTECT, related_name="blueprints")
    grade = models.ForeignKey(Grade, on_delete=models.PROTECT, related_name="blueprints")
    exam = models.ForeignKey(
        AcademicEvent, on_delete=models.SET_NULL, null=True, blank=True, related_name="blueprints",
        limit_choices_to={"event_type__in": [AcademicEvent.EventType.EXAM_SERIES, AcademicEvent.EventType.INTERNAL_EXAM]},
    )
    assigned_to = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="assigned_blueprints",
                                    help_text="Teacher or subject matter expert who writes this blueprint.")
    due_date = models.DateField(help_text="Deadline for submitting the blueprint for HOD review.")
    duration_minutes = models.PositiveSmallIntegerField(default=60, validators=[MinValueValidator(1)])
    language = models.CharField(max_length=2, choices=[("en", "English"), ("bn", "Bangla (বাংলা)")], default="en")
    sample_file = models.FileField(
        "sample question paper", upload_to="blueprints/%Y/%m/", blank=True,
        validators=[FileExtensionValidator(DOCUMENT_EXTENSIONS)],
        help_text="Optional. A past or sample paper (PDF, Word, TXT or Markdown) showing the style you want.",
    )
    materials = models.ManyToManyField(
        KnowledgeSource, blank=True, related_name="blueprints", verbose_name="materials to use",
        help_text="Optional. The chapters, syllabus or files the exam covers. Leave empty to use every active "
                  "material for the subject and grade.",
    )
    instructions = models.TextField("instructions for candidates", blank=True,
                                    default="Answer all questions. Write clearly.")
    guidance = models.TextField("guidance for the AI", blank=True,
                                help_text="Anything the final paper must respect, e.g. chapters to avoid or command words to use.")
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.DRAFT, db_index=True)

    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, editable=False,
                                   related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    submitted_at = models.DateTimeField(null=True, blank=True, editable=False)
    reviewed_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
                                    editable=False, related_name="+")
    reviewed_at = models.DateTimeField(null=True, blank=True, editable=False)
    review_note = models.TextField("HOD comments", blank=True)

    class Meta:
        ordering = ["due_date", "title"]
        verbose_name = "question blueprint"
        permissions = [
            ("review_blueprint", "Can review and approve question blueprints (HOD)"),
            ("manage_final_papers", "Can generate and see confidential final papers (exam controller)"),
        ]

    def __str__(self):
        return self.title

    @property
    def total_marks(self):
        return sum(item.count * item.marks_each for item in self.items.all())

    @property
    def total_questions(self):
        return sum(item.count for item in self.items.all())


class BlueprintItem(models.Model):
    """One section of a blueprint: how many questions of which kind, with an optional sample."""

    blueprint = models.ForeignKey(QuestionBlueprint, on_delete=models.CASCADE, related_name="items")
    order = models.PositiveSmallIntegerField(default=1)
    section = models.CharField(max_length=50, blank=True, help_text='e.g. "Section A".')
    question_type = models.CharField(max_length=12, choices=Question.QuestionType.choices)
    difficulty = models.CharField(max_length=8, choices=Question.Difficulty.choices, default=Question.Difficulty.MEDIUM)
    topic = models.CharField("topic / chapter", max_length=200, blank=True)
    count = models.PositiveSmallIntegerField("questions", default=5, validators=[MinValueValidator(1)])
    marks_each = models.PositiveSmallIntegerField(default=1, validators=[MinValueValidator(1)])
    sample_question = models.TextField(blank=True, help_text="An example of the style and level you expect.")

    class Meta:
        ordering = ["order", "id"]
        verbose_name = "blueprint section"

    def __str__(self):
        return f"{self.section or 'Section'}: {self.count} × {self.get_question_type_display()}"


class FinalPaper(models.Model):
    """The confidential exam paper Claude writes from an approved blueprint.

    Only users with questionbank.manage_final_papers (exam controller) and superusers can see it.
    It is kept apart from the question bank so teachers and HODs never see the final questions.
    """

    class Status(models.TextChoices):
        GENERATED = "generated", "Generated (can be regenerated)"
        LOCKED = "locked", "Locked · final"

    blueprint = models.OneToOneField(QuestionBlueprint, on_delete=models.PROTECT, related_name="final_paper")
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.GENERATED)
    model_used = models.CharField(max_length=50, blank=True, editable=False)
    generated_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, editable=False,
                                     related_name="+")
    generated_at = models.DateTimeField(null=True, blank=True, editable=False)
    locked_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
                                  editable=False, related_name="+")
    locked_at = models.DateTimeField(null=True, blank=True, editable=False)
    controller_notes = models.TextField(blank=True)
    # Claude writes the paper in the background; these show progress and the last failure on the page.
    generation_started_at = models.DateTimeField(null=True, blank=True, editable=False)
    generation_error = models.TextField(blank=True, editable=False)
    check_summary = models.TextField("automatic checks", blank=True, editable=False)

    GENERATION_TIMEOUT = timedelta(minutes=15)

    class Meta:
        ordering = ["-generated_at"]
        verbose_name = "final paper (confidential)"
        verbose_name_plural = "final papers (confidential)"
        default_permissions = ()  # access is controlled only by manage_final_papers

    def __str__(self):
        return f"Final · {self.blueprint.title}"

    @property
    def is_generating(self):
        """True while a background generation runs. After the timeout, a stuck run no longer blocks a new one."""
        started = self.generation_started_at
        return started is not None and timezone.now() - started < self.GENERATION_TIMEOUT

    @property
    def total_marks(self):
        return sum(q.marks for q in self.questions.all())


class FinalQuestion(models.Model):
    paper = models.ForeignKey(FinalPaper, on_delete=models.CASCADE, related_name="questions")
    order = models.PositiveSmallIntegerField(default=1)
    section = models.CharField(max_length=50, blank=True)
    question_type = models.CharField(max_length=12, choices=Question.QuestionType.choices)
    text = models.TextField("question")
    options = models.JSONField(default=list, blank=True)
    answer = models.TextField()
    explanation = models.TextField(blank=True)
    marks = models.PositiveSmallIntegerField(default=1)
    source_reference = models.CharField(max_length=255, blank=True)
    check_notes = models.TextField("check findings", blank=True, editable=False,
                                   help_text="Problems the automatic checks found. Fix the question or ignore them.")

    class Meta:
        ordering = ["order", "id"]
        verbose_name = "question"
        default_permissions = ()

    def __str__(self):
        return f"Q{self.order}"


class AIUsage(models.Model):
    """Tokens used by each Claude request, so cost and the need for retrieval (RAG) can be judged from real data.
    Holds counts only, never questions, so final papers stay confidential."""

    class Purpose(models.TextChoices):
        QUESTIONS = "questions", "Question bank generation"
        FINAL_PAPER = "final_paper", "Final paper"
        FINAL_REVIEW = "final_review", "Final paper review"
        FINAL_QUESTION = "final_question", "Single final question"

    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
                             related_name="+")
    purpose = models.CharField(max_length=20, choices=Purpose.choices)
    model = models.CharField(max_length=50)
    input_tokens = models.PositiveIntegerField(default=0)
    output_tokens = models.PositiveIntegerField(default=0)
    cache_read_tokens = models.PositiveIntegerField(default=0)
    cache_write_tokens = models.PositiveIntegerField(default=0)
    request_id = models.CharField(max_length=100, blank=True)

    class Meta:
        ordering = ["-created_at"]
        verbose_name = verbose_name_plural = "AI usage"
        default_permissions = ()  # super admins only; see AIUsageAdmin

    def __str__(self):
        return f"{self.get_purpose_display()} · {self.input_tokens} in / {self.output_tokens} out"
