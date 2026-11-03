"""Question generation with the AI provider (Claude or Gemini, see questionbank.llm), grounded in the uploaded
books for a subject and grade."""

import base64
import logging
import threading

import anthropic  # noqa: F401  (kept so tests can patch ai.anthropic.Anthropic, the same module llm uses)
from audit.services import Action, record

from . import llm
from .checks import find_similar, normalize
from .documents import extract_text
from .models import KnowledgeSource, Question
from .papers import blueprint_slots

logger = logging.getLogger(__name__)

# The API accepts requests up to 32 MB; base64 adds about a third, so keep raw files well under that.
MAX_TOTAL_SOURCE_BYTES = 22 * 1024 * 1024

SYSTEM_PROMPT = """\
You are an experienced school teacher and exam setter. You write assessment questions for students \
strictly from the textbook material provided to you.

Ground every question in the provided books: do not introduce facts, figures or definitions that are \
not supported by them. Match the vocabulary and depth to the stated grade. Each question must have a \
single, unambiguous correct answer (for long/creative questions, give a model answer or marking points). \
Avoid near-duplicate questions, and spread questions across the requested topic rather than clustering \
on one paragraph.

For multiple-choice questions give exactly four options, with plausible distractors, and make "answer" \
the exact text of the correct option. For true/false questions, "answer" is "True" or "False". For all \
other types, "options" is an empty list.

In "source_reference", name the chapter, section or page the question is based on, as it appears in the book.
"""

OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "questions": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "text": {"type": "string"},
                    "options": {"type": "array", "items": {"type": "string"}},
                    "answer": {"type": "string"},
                    "explanation": {"type": "string"},
                    "source_reference": {"type": "string"},
                },
                "required": ["text", "options", "answer", "explanation", "source_reference"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["questions"],
    "additionalProperties": False,
}

LANGUAGES = {"en": "English", "bn": "Bangla (বাংলা)"}


# One exception class for the whole app: llm raises it, and admin.py / tests catch ai.GenerationError.
GenerationError = llm.GenerationError
claude_client = llm.claude_client
final_papers_allowed = llm.final_papers_allowed


def ai_status():
    """For display: provider, its label, whether its API key is set, the bank and final models, and whether final
    papers may be written (False on Gemini without GEMINI_PAID_TIER). Makes no API call."""
    return llm.status()


def claude_status():
    """The older name templates use (claude.configured, claude.model); same dict as ai_status()."""
    return ai_status()


def _source_block(source):
    """PDFs go to the AI as PDFs, so it also sees figures and tables; other files as their extracted text. The block
    uses Anthropic's document format; llm translates it for Gemini."""
    with source.file.open("rb") as fh:
        if source.is_pdf:
            doc_source = {
                "type": "base64",
                "media_type": "application/pdf",
                "data": base64.standard_b64encode(fh.read()).decode("ascii"),
            }
        else:
            text = source.content_text or extract_text(fh, source.file.name)
            doc_source = {"type": "text", "media_type": "text/plain", "data": text}
    return {"type": "document", "source": doc_source, "title": source.label}


def _build_request(sources, *, subject, grade, topic, question_type, difficulty, count, language, examples=()):
    total = sum(s.file.size for s in sources)
    if total > MAX_TOTAL_SOURCE_BYTES:
        raise GenerationError(
            f"The active books for {subject} / {grade} add up to {total / 1024 / 1024:.0f} MB, which is too "
            "large to send in one request. Deactivate some books or upload individual chapters instead."
        )

    documents = [_source_block(s) for s in sources]
    # Cache the books: repeated generations for the same subject and grade reuse them at a fraction of the cost.
    documents[-1]["cache_control"] = {"type": "ephemeral"}

    type_label = Question.QuestionType(question_type).label
    instructions = (
        f"Subject: {subject}\n"
        f"Grade: {grade}\n"
        f"Topic / chapter: {topic or 'Any part of the provided books'}\n"
        f"Question type: {type_label}\n"
        f"Difficulty: {Question.Difficulty(difficulty).label}\n"
        f"Number of questions: {count}\n"
        f"Write the questions, options, answers and explanations in {LANGUAGES[language]}.\n\n"
        + _examples_text(examples)
        + f"Write exactly {count} {type_label.lower()} questions from the books above."
    )
    return [{"role": "user", "content": [*documents, {"type": "text", "text": instructions}]}]


# Approved bank questions sent as style examples: at most this many, and this many characters in total.
MAX_EXAMPLES = 8
MAX_EXAMPLE_CHARS = 6000


def approved_examples(*, subject, grade, question_type, difficulty, topic=""):
    """Questions HODs approved for the same subject, grade and type, closest first (same difficulty, then same
    topic, then newest). Sending them with each request lets generation follow what the school accepts, without
    any model training. Never used for confidential final papers: teachers can see the bank."""
    recent = list(Question.objects.filter(subject=subject, grade=grade, question_type=question_type,
                                          status=Question.Status.APPROVED).order_by("-created_at")[:50])
    topic = topic.strip().lower()
    recent.sort(key=lambda q: (q.difficulty != difficulty, not (topic and topic in q.topic.lower())))
    chosen, used = [], 0
    for q in recent:
        size = len(q.text) + len(q.answer) + sum(len(o) for o in q.options)
        if used + size > MAX_EXAMPLE_CHARS:
            continue
        chosen.append(q)
        used += size
        if len(chosen) == MAX_EXAMPLES:
            break
    return chosen


def _examples_text(examples):
    if not examples:
        return ""
    lines = ["Approved questions from this school's question bank for the same subject, grade and type. Match "
             "their style, depth, wording level and format, but write new questions: do not repeat or reword "
             "any of them.", ""]
    for n, q in enumerate(examples, 1):
        lines.append(f"Example {n} ({q.get_difficulty_display().lower()}): {q.text}")
        if q.options:
            lines.append("Options: " + " | ".join(q.options))
        lines.append(f"Answer: {q.answer}")
    return "\n".join(lines) + "\n\n"


def generate_questions(*, subject, grade, topic, question_type, difficulty, count, language, user, sources=None,
                       examples=None):
    """Ask the AI for questions and save them as drafts. Returns the saved Question objects.

    sources limits the AI to chosen materials (e.g. some chapters); by default it reads all active ones.
    examples are approved bank questions to imitate; by default approved_examples() picks them.
    """
    sources = list(sources) if sources else list(
        KnowledgeSource.objects.filter(subject=subject, grade=grade, is_active=True))
    if not sources:
        raise GenerationError(
            f"No active books are uploaded for {subject} / {grade}. "
            "Add one under Question bank → Books & knowledge sources first."
        )

    if examples is None:
        examples = approved_examples(subject=subject, grade=grade, question_type=question_type,
                                     difficulty=difficulty, topic=topic)
    messages = _build_request(
        sources, subject=subject, grade=grade, topic=topic, question_type=question_type,
        difficulty=difficulty, count=count, language=language, examples=examples,
    )

    items = _ask_claude(SYSTEM_PROMPT, messages, OUTPUT_SCHEMA, purpose="questions", user=user)["questions"]

    questions = [
        Question(
            subject=subject,
            grade=grade,
            topic=topic,
            question_type=question_type,
            difficulty=difficulty,
            text=item["text"].strip(),
            options=[o.strip() for o in item["options"]] if question_type == Question.QuestionType.MCQ else [],
            answer=item["answer"].strip(),
            explanation=item["explanation"].strip(),
            source_reference=item["source_reference"].strip()[:255],
            status=Question.Status.DRAFT,
            ai_generated=True,
            created_by=user,
        )
        for item in items
        if item.get("text", "").strip()
    ]

    # Drop repeats within this batch; flag questions that closely match one already in the bank.
    bank = [(pk, normalize(text)) for pk, text in Question.objects.filter(subject=subject, grade=grade)
            .exclude(status=Question.Status.REJECTED).values_list("pk", "text")]
    kept, batch = [], []
    for q in questions:
        if find_similar(q.text, batch) is not None:
            continue
        q.duplicate_of_id = find_similar(q.text, bank)
        batch.append((len(batch), normalize(q.text)))
        kept.append(q)
    # Saved one by one, not with bulk_create: on MySQL bulk_create returns the questions without their new ids,
    # and callers need them. A batch is only a handful of questions.
    from django.db import transaction
    with transaction.atomic():
        for q in kept:
            q.save()
    return kept


def _ask_model(system, messages, schema, *, effort="medium", purpose, user=None, final=False):
    """Send one structured-output request to the configured provider and return the parsed JSON object.
    final=True (confidential final papers, their review pass, single-question rewrites) uses AI_FINAL_MODEL and is
    refused on Gemini's free tier. purpose (an AIUsage.Purpose value) and user label the recorded token counts."""
    return llm.ask(system, messages, schema, effort=effort, purpose=purpose, user=user, final=final)


def _ask_claude(*args, **kwargs):
    """The permanent seam for every AI request in this module, whichever provider is active (the name predates
    Gemini support and is kept on purpose). Every caller here goes through it, and tests patch
    mock.patch.object(ai, "_ask_claude") to stub the AI. Callers look it up as a module global at call time, and it
    looks up _ask_model at call time too, so patching either name intercepts every request; a plain alias
    (_ask_claude = _ask_model) would not, as callers would bind to only one of the two names."""
    return _ask_model(*args, **kwargs)


# ---------- Confidential final paper from a blueprint ----------

FINAL_SYSTEM_PROMPT = """\
You are the chief examiner setting a confidential school exam paper. You are given the subject's textbooks, \
an exam blueprint written by a subject teacher (sections, question types, difficulty, topics, marks and sample \
questions), and possibly a sample paper.

Write a brand-new paper that follows the blueprint exactly: the same sections, the requested number of \
questions of each type, difficulty and marks. Use the sample questions and sample paper only as a guide to \
style, format and level. Never copy or lightly reword a sample question; the teacher who wrote the blueprint \
must not be able to predict the final questions.

Ground every question in the textbooks provided and match the grade level. Each question must have one \
unambiguous correct answer (for long/creative questions give a model answer or marking points). For \
multiple-choice questions give exactly four options and make "answer" the exact text of the correct option; \
for true/false, "answer" is "True" or "False"; for other types "options" is an empty list. In \
"source_reference" name the chapter, section or page the question is based on.
"""

FINAL_SCHEMA = {
    "type": "object",
    "properties": {
        "questions": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "section": {"type": "string"},
                    "question_type": {"type": "string", "enum": [c for c, _ in Question.QuestionType.choices]},
                    "text": {"type": "string"},
                    "options": {"type": "array", "items": {"type": "string"}},
                    "answer": {"type": "string"},
                    "explanation": {"type": "string"},
                    "marks": {"type": "integer"},
                    "source_reference": {"type": "string"},
                },
                "required": ["section", "question_type", "text", "options", "answer", "explanation", "marks",
                             "source_reference"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["questions"],
    "additionalProperties": False,
}


def _blueprint_text(bp, items):
    lines = [
        f"Exam: {bp.title}",
        f"Subject: {bp.subject} · Grade: {bp.grade}" + (f" · Exam: {bp.exam}" if bp.exam else ""),
        f"Duration: {bp.duration_minutes} minutes · Total marks: {sum(i.count * i.marks_each for i in items)}",
        f"Language: write everything in {LANGUAGES[bp.language]}.",
        "",
        "Blueprint sections:",
    ]
    for n, item in enumerate(items, 1):
        lines.append(
            f"{n}. {item.section or f'Section {n}'}: {item.count} × {item.get_question_type_display()} "
            f"(question_type \"{item.question_type}\"), {item.get_difficulty_display().lower()}, "
            f"{item.marks_each} mark(s) each" + (f", topic: {item.topic}" if item.topic else "")
        )
        if item.sample_question.strip():
            lines.append(f"   Sample (style only, do not reuse): {item.sample_question.strip()}")
    if bp.guidance.strip():
        lines += ["", f"Additional guidance from the department: {bp.guidance.strip()}"]
    lines += ["", f"Write exactly {sum(i.count for i in items)} questions, in blueprint order."]
    return "\n".join(lines)


def blueprint_materials(blueprint):
    """The materials the blueprint names, or every active material for its subject and grade if it names none."""
    active = KnowledgeSource.objects.filter(subject=blueprint.subject, grade=blueprint.grade, is_active=True)
    chosen = active.filter(pk__in=blueprint.materials.values("pk"))
    return chosen if blueprint.materials.exists() else active


def _blueprint_documents(blueprint):
    """The blueprint's materials (and sample paper) as document blocks, with the books cached."""
    sources = list(blueprint_materials(blueprint))
    if not sources:
        raise GenerationError(
            f"No active books are uploaded for {blueprint.subject} / {blueprint.grade}. "
            "Upload one under Books & knowledge sources so the paper is grounded in the syllabus."
        )
    total = sum(s.file.size for s in sources) + (blueprint.sample_file.size if blueprint.sample_file else 0)
    if total > MAX_TOTAL_SOURCE_BYTES:
        raise GenerationError("The books and sample paper are too large to send together. Upload chapters instead.")
    documents = [_source_block(s) for s in sources]
    documents[-1]["cache_control"] = {"type": "ephemeral"}
    if blueprint.sample_file:
        sample = KnowledgeSource(title="Sample paper (style reference only)", file=blueprint.sample_file,
                                 material_type=KnowledgeSource.MaterialType.SAMPLE_QUESTIONS)
        documents.append(_source_block(sample))
    return documents


def _final_fields(q):
    """The fields of a FinalQuestion that come from the AI's reply, cleaned up."""
    return {
        "text": q["text"].strip(),
        "options": [o.strip() for o in q["options"]] if q["question_type"] == Question.QuestionType.MCQ else [],
        "answer": q["answer"].strip(),
        "explanation": q["explanation"].strip(),
        "source_reference": q["source_reference"].strip()[:255],
    }


def generate_final_paper(blueprint, user):
    """Write (or rewrite) the confidential final paper for an approved blueprint. Returns the FinalPaper."""
    from django.db import transaction
    from django.utils import timezone

    from .models import FinalPaper, FinalQuestion

    items = list(blueprint.items.all())
    if not items:
        raise GenerationError("This blueprint has no sections yet.")
    documents = _blueprint_documents(blueprint)
    messages = [{"role": "user", "content": [*documents, {"type": "text", "text": _blueprint_text(blueprint, items)}]}]

    data = _ask_claude(FINAL_SYSTEM_PROMPT, messages, FINAL_SCHEMA, effort="high", purpose="final_paper", user=user,
                       final=True)
    questions = [q for q in data["questions"] if q.get("text", "").strip()]
    slots = blueprint_slots(items)
    if len(questions) != len(slots):
        raise GenerationError(f"The AI wrote {len(questions)} questions but the blueprint asks for {len(slots)}, "
                              "so the paper wasn't saved. Please generate it again.")
    # The blueprint decides sections, types and marks, so the totals always match it.
    for q, slot in zip(questions, slots):
        q.update(section=slot.section, question_type=slot.question_type, marks=slot.marks)

    notes, summary = check_final_questions(questions, items, documents, user=user)

    with transaction.atomic():
        paper, _ = FinalPaper.objects.select_for_update().get_or_create(blueprint=blueprint)
        if paper.status == FinalPaper.Status.LOCKED:
            raise GenerationError("This final paper is locked and can't be regenerated.")
        paper.questions.all().delete()
        FinalQuestion.objects.bulk_create([
            FinalQuestion(
                paper=paper, order=n, section=q["section"].strip()[:50], question_type=q["question_type"],
                marks=q["marks"], check_notes="\n".join(notes[n]), **_final_fields(q),
            )
            for n, q in enumerate(questions, 1)
        ])
        paper.check_summary = summary
        paper.model_used = llm.model_for(final=True)[:FinalPaper._meta.get_field("model_used").max_length]
        paper.generated_by, paper.generated_at = user, timezone.now()
        paper.save()
    return paper


REVIEW_SYSTEM_PROMPT = """\
You are a second examiner checking a draft school exam paper against the provided materials (textbooks, \
syllabus, lesson plans). For each question, decide whether it is covered by the materials, whether the given \
answer is correct and supported by them, and whether the question is ambiguous or has more than one correct answer. \
Report every question by its number. Keep "issue" empty when the question is fine; otherwise describe the problem \
in one short sentence for the exam controller.
"""

REVIEW_SCHEMA = {
    "type": "object",
    "properties": {
        "reviews": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "number": {"type": "integer"},
                    "in_syllabus": {"type": "boolean"},
                    "answer_correct": {"type": "boolean"},
                    "issue": {"type": "string"},
                },
                "required": ["number", "in_syllabus", "answer_correct", "issue"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["reviews"],
    "additionalProperties": False,
}


def check_final_questions(questions, items, documents, user=None):
    """Flag repeated questions, copied blueprint samples, and (by a second AI pass over the same, cached
    materials) questions outside the syllabus or with wrong answers. Returns ({number: [notes]}, summary)."""
    notes = {n: [] for n in range(1, len(questions) + 1)}
    seen = []
    samples = [(item.order, normalize(item.sample_question)) for item in items if item.sample_question.strip()]
    for n, q in enumerate(questions, 1):
        earlier = find_similar(q["text"], seen)
        if earlier is not None:
            notes[n].append(f"Very similar to question {earlier}.")
        if find_similar(q["text"], samples) is not None:
            notes[n].append("Close to a sample question in the blueprint, so its author could predict it.")
        seen.append((n, normalize(q["text"])))

    listing = []
    for n, q in enumerate(questions, 1):
        listing.append(f"{n}. {q['text']}")
        listing += [f"   {letter}) {o}" for letter, o in zip("ABCDEFGH", q["options"])]
        listing.append(f"   Answer: {q['answer']}")
    review_messages = [{"role": "user", "content": [
        *documents, {"type": "text", "text": "Check this draft paper:\n\n" + "\n".join(listing)}]}]
    reviewed = True
    try:
        for r in _ask_claude(REVIEW_SYSTEM_PROMPT, review_messages, REVIEW_SCHEMA,
                             purpose="final_review", user=user, final=True)["reviews"]:
            if r["number"] not in notes:
                continue
            if not r["in_syllabus"]:
                notes[r["number"]].append("May not be covered by the materials.")
            if not r["answer_correct"]:
                notes[r["number"]].append("The answer may be wrong.")
            if r["issue"].strip():
                notes[r["number"]].append(r["issue"].strip())
    except (GenerationError, KeyError, TypeError) as exc:
        logger.warning("Final paper review pass failed: %s", exc)
        reviewed = False

    flagged = sum(1 for n in notes if notes[n])
    summary = (f"{flagged} of {len(questions)} questions flagged; see “Check findings” on each."
               if flagged else f"All {len(questions)} questions passed the automatic checks.")
    if not reviewed:
        summary += " The AI review of syllabus fit and answers couldn't run this time; check those by hand."
    return notes, summary


REGENERATED_NOTE = "Regenerated on its own; the AI review of syllabus fit and answers didn't check it. Check it by hand."


def regenerate_final_question(paper, order, user):
    """Replace one question of an unlocked final paper with a new one for the same blueprint slot. Cheaper and
    faster than regenerating the whole paper, and keeps the exam controller's edits to the others."""
    from django.db import transaction

    from .models import FinalPaper, FinalQuestion

    blueprint = paper.blueprint
    if paper.status == FinalPaper.Status.LOCKED:
        raise GenerationError("This final paper is locked and can't be regenerated.")
    questions = list(paper.questions.order_by("order", "id"))
    target = next((q for q in questions if q.order == order), None)
    if target is None:
        raise GenerationError(f"This paper has no question {order}.")
    slots = blueprint_slots(list(blueprint.items.all()))
    position = questions.index(target)
    if position >= len(slots):
        raise GenerationError(f"Question {order} isn't part of the blueprint. Edit or delete it by hand.")
    slot = slots[position]
    others = [q for q in questions if q.pk != target.pk]

    lines = [
        f"Exam: {blueprint.title}",
        f"Subject: {blueprint.subject} · Grade: {blueprint.grade}",
        f"Language: write everything in {LANGUAGES[blueprint.language]}.",
        "",
        f"Write exactly 1 new question to replace question {order}:",
        f"- Section: {slot.section}",
        f"- Type: {slot.type_label} (question_type \"{slot.question_type}\")",
        f"- Difficulty: {Question.Difficulty(slot.difficulty).label.lower()}",
        f"- Marks: {slot.marks}" + (f", topic: {slot.topic}" if slot.topic else ""),
    ]
    if slot.sample:
        lines.append(f"- Sample (style only, do not reuse): {slot.sample}")
    if blueprint.guidance.strip():
        lines.append(f"Additional guidance from the department: {blueprint.guidance.strip()}")
    lines += ["", f"The question being replaced (write a different one): {target.text}",
              "It must not repeat or resemble these questions already in the paper:",
              *[f"- {q.text}" for q in others]]
    messages = [{"role": "user", "content": [*_blueprint_documents(blueprint),
                                             {"type": "text", "text": "\n".join(lines)}]}]

    data = _ask_claude(FINAL_SYSTEM_PROMPT, messages, FINAL_SCHEMA, effort="high", purpose="final_question", user=user,
                       final=True)
    new = [q for q in data["questions"] if q.get("text", "").strip()]
    if len(new) != 1:
        raise GenerationError(f"The AI wrote {len(new)} questions instead of 1. Please try again.")
    reply = {**new[0], "question_type": slot.question_type}

    notes = []
    earlier = find_similar(reply["text"], [(q.order, normalize(q.text)) for q in others])
    if earlier is not None:
        notes.append(f"Very similar to question {earlier}.")
    if slot.sample and find_similar(reply["text"], [(0, normalize(slot.sample))]) is not None:
        notes.append("Close to a sample question in the blueprint, so its author could predict it.")
    notes.append(REGENERATED_NOTE)

    with transaction.atomic():
        current = FinalPaper.objects.select_for_update().get(pk=paper.pk)
        if current.status == FinalPaper.Status.LOCKED:
            raise GenerationError("This final paper is locked and can't be regenerated.")
        FinalQuestion.objects.filter(pk=target.pk).update(
            section=slot.section, question_type=slot.question_type, marks=slot.marks,
            check_notes="\n".join(notes), **_final_fields(reply))
    return FinalQuestion.objects.get(pk=target.pk)


def _claim(paper, user):
    """Mark the paper as being written. Call inside a transaction holding select_for_update on the paper."""
    from django.utils import timezone

    from .models import FinalPaper
    from .workflow import INVOLVED_MESSAGE, can_open_final

    if not can_open_final(user, paper.blueprint):
        raise GenerationError(INVOLVED_MESSAGE)
    # Checked here too (llm.ask also refuses), so the paper isn't marked "being written" for a request that
    # would only fail in the background thread.
    if not final_papers_allowed():
        raise GenerationError(llm.FINAL_NOT_ALLOWED)
    if paper.status == FinalPaper.Status.LOCKED:
        raise GenerationError("This final paper is locked and can't be regenerated.")
    if paper.is_generating:
        raise GenerationError("The AI is already writing this paper. You'll get a notification when it's ready.")
    paper.generation_started_at, paper.generation_error = timezone.now(), ""
    paper.save(update_fields=["generation_started_at", "generation_error"])


def start_final_paper(blueprint, user):
    """Have the AI write the final paper in a background thread, since it can take several minutes and a web
    request would time out. The user gets a notification when it's done. Returns the FinalPaper."""
    from django.db import transaction

    from .models import FinalPaper

    with transaction.atomic():
        paper, _ = FinalPaper.objects.select_for_update().get_or_create(blueprint=blueprint)
        _claim(paper, user)
    record(user, Action.FINAL_STARTED, paper)
    threading.Thread(target=_in_thread, args=(write_final_paper, paper.pk, user.pk), daemon=True).start()
    return paper


def start_question_regeneration(paper, order, user):
    """Like start_final_paper, for one question. Uses the same "being written" flag, so the paper can't be
    regenerated twice at once."""
    from django.db import transaction

    from .models import FinalPaper

    with transaction.atomic():
        paper = FinalPaper.objects.select_for_update().get(pk=paper.pk)
        _claim(paper, user)
    record(user, Action.FINAL_STARTED, paper, details=f"Regenerating question {order}")
    threading.Thread(target=_in_thread, args=(rewrite_final_question, paper.pk, order, user.pk),
                     daemon=True).start()
    return paper


def _in_thread(func, *args):
    from django.db import connection

    try:
        func(*args)
    finally:
        connection.close()  # each thread has its own database connection


def _run_and_report(paper_pk, user_pk, job, label):
    """The background part: run job(paper, user), record the outcome and notify the user.
    label names what was written in the notification, e.g. "Final paper" or "Question 3"."""
    from django.contrib.auth import get_user_model
    from django.urls import reverse

    from notifications.models import Notification
    from notifications.services import notify

    from .models import FinalPaper

    paper = FinalPaper.objects.select_related("blueprint").get(pk=paper_pk)
    user = get_user_model().objects.get(pk=user_pk)
    try:
        job(paper, user)
        error = ""
    except GenerationError as exc:
        error = str(exc)
    except Exception:
        logger.exception("%s generation failed for blueprint %s", label, paper.blueprint_id)
        error = "Something went wrong while writing the paper. Please try again."
    FinalPaper.objects.filter(pk=paper_pk).update(generation_started_at=None, generation_error=error)
    record(user, Action.FINAL_FAILED if error else Action.FINAL_GENERATED, paper,
           details=error or ("" if label == "Final paper" else label))
    url = reverse("admin:questionbank_finalpaper_change", args=[paper_pk])
    if error:
        notify(user, f"{label} not generated: {paper.blueprint.title}", body=error, url=url,
               kind=Notification.Kind.WARNING)
    else:
        notify(user, f"{label} ready: {paper.blueprint.title}", body="Review the questions, then lock the paper.",
               url=url, kind=Notification.Kind.SUCCESS)


def write_final_paper(paper_pk, user_pk):
    """The background part of start_final_paper."""
    _run_and_report(paper_pk, user_pk, lambda paper, user: generate_final_paper(paper.blueprint, user),
                    "Final paper")


def rewrite_final_question(paper_pk, order, user_pk):
    """The background part of start_question_regeneration."""
    _run_and_report(paper_pk, user_pk, lambda paper, user: regenerate_final_question(paper, order, user),
                    f"Question {order}")
