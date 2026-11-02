"""Question generation with Claude, grounded in the uploaded books for a subject and grade."""

import base64
import json
import logging
import threading

import anthropic
from django.conf import settings

from audit.services import Action, record

from .checks import find_similar, normalize
from .documents import extract_text
from .models import KnowledgeSource, Question

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


class GenerationError(Exception):
    """A problem the user can act on; the message is shown in the admin."""


def claude_status():
    """For display: whether an API key is configured and which model is used. Makes no API call."""
    import os

    return {"configured": bool(os.environ.get("ANTHROPIC_API_KEY")), "model": settings.QUESTION_GENERATION_MODEL}


def _source_block(source):
    """PDFs go to Claude as PDFs, so it also sees figures and tables; other files as their extracted text."""
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


def _build_request(sources, *, subject, grade, topic, question_type, difficulty, count, language):
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
        f"Write exactly {count} {type_label.lower()} questions from the books above."
    )
    return [{"role": "user", "content": [*documents, {"type": "text", "text": instructions}]}]


def generate_questions(*, subject, grade, topic, question_type, difficulty, count, language, user, sources=None):
    """Ask Claude for questions and save them as drafts. Returns the saved Question objects.

    sources limits Claude to chosen materials (e.g. some chapters); by default it reads all active ones.
    """
    sources = list(sources) if sources else list(
        KnowledgeSource.objects.filter(subject=subject, grade=grade, is_active=True))
    if not sources:
        raise GenerationError(
            f"No active books are uploaded for {subject} / {grade}. "
            "Add one under Question bank → Books & knowledge sources first."
        )

    messages = _build_request(
        sources, subject=subject, grade=grade, topic=topic, question_type=question_type,
        difficulty=difficulty, count=count, language=language,
    )

    items = _ask_claude(SYSTEM_PROMPT, messages, OUTPUT_SCHEMA)["questions"]

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


def _ask_claude(system, messages, schema, *, effort="medium"):
    """Send one structured-output request to Claude and return the parsed JSON object."""
    try:
        client = anthropic.Anthropic()
        with client.beta.messages.stream(
            model=settings.QUESTION_GENERATION_MODEL,
            max_tokens=64000,
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
            system=system,
            output_config={
                "effort": effort,
                "format": {"type": "json_schema", "schema": schema},
            },
            messages=messages,
        ) as stream:
            response = stream.get_final_message()
    except anthropic.AuthenticationError:
        raise GenerationError("The Claude API key is missing or invalid. Set ANTHROPIC_API_KEY on the server.")
    except anthropic.PermissionDeniedError:
        raise GenerationError("The Claude API key doesn't have access to this model.")
    except anthropic.RateLimitError:
        raise GenerationError("Claude is busy right now (rate limit). Please try again in a minute.")
    except anthropic.BadRequestError as exc:
        logger.warning("Claude rejected the question request: %s", exc)
        raise GenerationError(f"Claude couldn't process this request: {exc.message}")
    except anthropic.APIStatusError as exc:
        logger.warning("Claude API error %s: %s", exc.status_code, exc)
        raise GenerationError("Claude returned a server error. Please try again shortly.")
    except anthropic.APIConnectionError:
        raise GenerationError("Couldn't reach the Claude API. Check the server's internet connection.")
    except anthropic.AnthropicError as exc:
        logger.warning("Claude client error: %s", exc)
        raise GenerationError("The Claude API isn't configured on this server. Set ANTHROPIC_API_KEY.")

    logger.info(
        "Question generation %s: %s in / %s out tokens, %s cached",
        response._request_id, response.usage.input_tokens, response.usage.output_tokens,
        response.usage.cache_read_input_tokens,
    )

    if response.stop_reason == "refusal":
        raise GenerationError("Claude declined to generate questions for this material.")
    if response.stop_reason == "max_tokens":
        raise GenerationError("The answer was cut off. Try asking for fewer questions at a time.")

    text = "".join(block.text for block in response.content if block.type == "text")
    try:
        data = json.loads(text)
        data[schema["required"][0]]
    except (ValueError, KeyError, TypeError):
        logger.warning("Unparseable question output (request %s): %r", response._request_id, text[:500])
        raise GenerationError("Claude's reply couldn't be read. Please try again.")
    return data


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


def generate_final_paper(blueprint, user):
    """Write (or rewrite) the confidential final paper for an approved blueprint. Returns the FinalPaper."""
    from django.db import transaction
    from django.utils import timezone

    from .models import FinalPaper, FinalQuestion

    items = list(blueprint.items.all())
    if not items:
        raise GenerationError("This blueprint has no sections yet.")
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
    messages = [{"role": "user", "content": [*documents, {"type": "text", "text": _blueprint_text(blueprint, items)}]}]

    data = _ask_claude(FINAL_SYSTEM_PROMPT, messages, FINAL_SCHEMA, effort="high")
    questions = [q for q in data["questions"] if q.get("text", "").strip()]
    # One slot per question the blueprint asks for, in blueprint order: (section, type, marks).
    slots = [(item.section or f"Section {n}", item.question_type, item.marks_each)
             for n, item in enumerate(items, 1) for _ in range(item.count)]
    if len(questions) != len(slots):
        raise GenerationError(f"Claude wrote {len(questions)} questions but the blueprint asks for {len(slots)}, "
                              "so the paper wasn't saved. Please generate it again.")
    # The blueprint decides sections, types and marks, so the totals always match it.
    for q, (section, question_type, marks) in zip(questions, slots):
        q.update(section=section, question_type=question_type, marks=marks)

    notes, summary = check_final_questions(questions, items, documents)

    with transaction.atomic():
        paper, _ = FinalPaper.objects.select_for_update().get_or_create(blueprint=blueprint)
        if paper.status == FinalPaper.Status.LOCKED:
            raise GenerationError("This final paper is locked and can't be regenerated.")
        paper.questions.all().delete()
        FinalQuestion.objects.bulk_create([
            FinalQuestion(
                paper=paper, order=n, section=q["section"].strip()[:50], question_type=q["question_type"],
                text=q["text"].strip(), answer=q["answer"].strip(), explanation=q["explanation"].strip(),
                options=[o.strip() for o in q["options"]] if q["question_type"] == Question.QuestionType.MCQ else [],
                marks=q["marks"], source_reference=q["source_reference"].strip()[:255],
                check_notes="\n".join(notes[n]),
            )
            for n, q in enumerate(questions, 1)
        ])
        paper.check_summary = summary
        paper.model_used = settings.QUESTION_GENERATION_MODEL
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


def check_final_questions(questions, items, documents):
    """Flag repeated questions, copied blueprint samples, and (by a second Claude pass over the same, cached
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
        for r in _ask_claude(REVIEW_SYSTEM_PROMPT, review_messages, REVIEW_SCHEMA)["reviews"]:
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


def start_final_paper(blueprint, user):
    """Have Claude write the final paper in a background thread, since it can take several minutes and a web
    request would time out. The user gets a notification when it's done. Returns the FinalPaper."""
    from django.db import transaction
    from django.utils import timezone

    from .models import FinalPaper

    with transaction.atomic():
        paper, _ = FinalPaper.objects.select_for_update().get_or_create(blueprint=blueprint)
        if paper.status == FinalPaper.Status.LOCKED:
            raise GenerationError("This final paper is locked and can't be regenerated.")
        if paper.is_generating:
            raise GenerationError("Claude is already writing this paper. You'll get a notification when it's ready.")
        paper.generation_started_at, paper.generation_error = timezone.now(), ""
        paper.save(update_fields=["generation_started_at", "generation_error"])
    record(user, Action.FINAL_STARTED, paper)
    threading.Thread(target=_in_thread, args=(write_final_paper, paper.pk, user.pk), daemon=True).start()
    return paper


def _in_thread(func, *args):
    from django.db import connection

    try:
        func(*args)
    finally:
        connection.close()  # each thread has its own database connection


def write_final_paper(paper_pk, user_pk):
    """The background part of start_final_paper: generate, record the outcome and notify the user."""
    from django.contrib.auth import get_user_model
    from django.urls import reverse

    from notifications.models import Notification
    from notifications.services import notify

    from .models import FinalPaper

    paper = FinalPaper.objects.select_related("blueprint").get(pk=paper_pk)
    user = get_user_model().objects.get(pk=user_pk)
    try:
        generate_final_paper(paper.blueprint, user)
        error = ""
    except GenerationError as exc:
        error = str(exc)
    except Exception:
        logger.exception("Final paper generation failed for blueprint %s", paper.blueprint_id)
        error = "Something went wrong while writing the paper. Please try again."
    FinalPaper.objects.filter(pk=paper_pk).update(generation_started_at=None, generation_error=error)
    record(user, Action.FINAL_FAILED if error else Action.FINAL_GENERATED, paper, details=error)
    url = reverse("admin:questionbank_finalpaper_change", args=[paper_pk])
    if error:
        notify(user, f"Final paper not generated: {paper.blueprint.title}", body=error, url=url,
               kind=Notification.Kind.WARNING)
    else:
        notify(user, f"Final paper ready: {paper.blueprint.title}", body="Review the questions, then lock the paper.",
               url=url, kind=Notification.Kind.SUCCESS)
