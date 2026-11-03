import json
import os
import shutil
import tempfile
from datetime import date, timedelta
from io import BytesIO, StringIO
from types import SimpleNamespace
from unittest import mock

import pymupdf
from docx import Document
from django.apps import apps
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group, Permission
from django.core import mail
from django.core.exceptions import ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.management import call_command
from django.db.models import UniqueConstraint
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from audit.models import AuditEvent
from notifications.models import Notification

from . import ai, workflow
from .forms import GenerateQuestionsForm
from .models import (
    AcademicEvent, AIUsage, BlueprintItem, FinalPaper, FinalQuestion, Grade, KnowledgeSource, Question, QuestionBlueprint, Subject,
    SubjectAssignment,
)
from .reminders import ensure_reminders

MEDIA = tempfile.mkdtemp()

# The AI settings are read from .env (setdefault), so a developer's AI_PROVIDER=gemini would otherwise route these
# tests to Gemini. Pin the provider the tests were written for; Gemini tests override it in test_llm.py.
AI_DEFAULTS = dict(AI_PROVIDER="anthropic", AI_MODEL="", AI_FINAL_MODEL="", GEMINI_PAID_TIER=False)


def claude_reply(n):
    return {"questions": [
        {"section": "whatever", "question_type": "short", "text": f"Question {i}?", "options": [], "answer": "A",
         "explanation": "", "marks": 99, "source_reference": "Ch 1"}
        for i in range(n)
    ]}


@override_settings(MEDIA_ROOT=MEDIA, **AI_DEFAULTS)
class BlueprintTestCase(TestCase):
    @classmethod
    def tearDownClass(cls):
        super().tearDownClass()
        shutil.rmtree(MEDIA, ignore_errors=True)

    def setUp(self):
        User = get_user_model()
        self.hod = User.objects.create_user("hod", password="Pw-12345!", is_staff=True)
        self.hod.user_permissions.add(Permission.objects.get(codename="review_blueprint"))
        self.hod = User.objects.get(pk=self.hod.pk)  # reload to clear the permission cache
        self.controller = User.objects.create_superuser("controller", "c@example.com", "Pw-12345!")
        subject, grade = Subject.objects.create(name="Science"), Grade.objects.create(name="Class 8")
        KnowledgeSource.objects.create(subject=subject, grade=grade, title="Book",
                                       file=SimpleUploadedFile("book.txt", b"Plants make food."))
        self.bp = QuestionBlueprint.objects.create(
            title="Science mid-term", subject=subject, grade=grade, assigned_to=self.hod, due_date=date(2026, 12, 1),
            status=QuestionBlueprint.Status.APPROVED,
        )
        BlueprintItem.objects.create(blueprint=self.bp, order=1, section="A", question_type="mcq", count=2, marks_each=1)
        BlueprintItem.objects.create(blueprint=self.bp, order=2, section="B", question_type="short", count=1, marks_each=5)


class FinalPaperTests(BlueprintTestCase):
    def make_paper(self):
        with mock.patch.object(ai, "_ask_claude", return_value=claude_reply(3)):
            return ai.generate_final_paper(self.bp, self.controller)

    def test_slots_follow_blueprint_order_and_details(self):
        from .papers import blueprint_slots
        slots = blueprint_slots(list(self.bp.items.all()))
        self.assertEqual([(s.section, s.question_type, s.marks) for s in slots],
                         [("A", "mcq", 1), ("A", "mcq", 1), ("B", "short", 5)])
        self.assertEqual(slots[0].type_label, "Multiple choice")

    def test_marks_sections_and_types_follow_the_blueprint(self):
        with mock.patch.object(ai, "_ask_claude", return_value=claude_reply(3)):
            paper = ai.generate_final_paper(self.bp, self.controller)
        questions = list(paper.questions.all())
        self.assertEqual([q.section for q in questions], ["A", "A", "B"])
        self.assertEqual([q.question_type for q in questions], ["mcq", "mcq", "short"])
        self.assertEqual(paper.total_marks, 7)

    def test_generated_paper_matches_its_blueprint(self):
        self.assertEqual(workflow.paper_problems(self.make_paper()), [])

    def test_paper_missing_a_question_cannot_be_locked(self):
        paper = self.make_paper()
        paper.questions.last().delete()
        with self.assertRaisesMessage(workflow.WorkflowError, "The blueprint asks for 3 questions; this paper has 2."):
            workflow.lock_final(paper, self.controller)

    def test_changed_marks_cannot_be_locked(self):
        paper = self.make_paper()
        first = paper.questions.first()
        first.marks = 4
        first.save()
        problems = workflow.paper_problems(paper)
        self.assertIn("Question 1 should be Multiple choice in A for 1 mark(s).", problems)
        self.assertIn("Total marks are 10; the blueprint's total is 7.", problems)
        with self.assertRaises(workflow.WorkflowError):
            workflow.lock_final(paper, self.controller)

    def test_page_lists_what_to_fix_before_locking(self):
        paper = self.make_paper()
        paper.questions.last().delete()
        self.client.force_login(self.controller)
        response = self.client.get(reverse("admin:questionbank_finalpaper_change", args=[paper.pk]))
        self.assertContains(response, "The blueprint asks for 3 questions; this paper has 2.")

    def post_questions(self, paper, **first_changes):
        """Submit the final paper's admin form as the browser does, changing fields of the first question."""
        questions = list(paper.questions.all())
        data = {"controller_notes": "", "questions-TOTAL_FORMS": str(len(questions)),
                "questions-INITIAL_FORMS": str(len(questions)), "questions-MIN_NUM_FORMS": "0",
                "questions-MAX_NUM_FORMS": "1000", "_save": "Save"}
        for i, q in enumerate(questions):
            values = {"id": q.pk, "paper": paper.pk, "order": q.order, "section": q.section,
                      "question_type": q.question_type, "marks": q.marks, "text": q.text,
                      "options": json.dumps(q.options), "answer": q.answer, "explanation": q.explanation,
                      "source_reference": q.source_reference}
            if i == 0:
                values.update(first_changes)
            data.update({f"questions-{i}-{k}": v for k, v in values.items()})
            # options has a callable default, so the page also carries its initial value in a hidden input.
            data[f"initial-questions-{i}-options"] = json.dumps(q.options)
        self.client.force_login(self.controller)
        return self.client.post(reverse("admin:questionbank_finalpaper_change", args=[paper.pk]), data)

    def test_edited_question_is_marked_unchecked(self):
        paper = self.make_paper()
        FinalQuestion.objects.filter(paper=paper).update(check_notes="")
        self.assertEqual(self.post_questions(paper, text="A better question?").status_code, 302)
        first, second = paper.questions.all()[:2]
        self.assertEqual(first.text, "A better question?")
        self.assertEqual(first.check_notes, "Edited after the automatic checks. Check it by hand.")
        self.assertEqual(second.check_notes, "")

    def test_saving_without_edits_keeps_check_findings(self):
        paper = self.make_paper()
        FinalQuestion.objects.filter(paper=paper).update(check_notes="Fine.")
        self.client.force_login(self.controller)
        page = self.client.get(reverse("admin:questionbank_finalpaper_change", args=[paper.pk]))
        self.assertContains(page, 'name="initial-questions-0-options"')  # what post_questions imitates
        self.assertEqual(self.post_questions(paper).status_code, 302)
        self.assertEqual(paper.questions.first().check_notes, "Fine.")

    def test_wrong_number_of_questions_is_not_saved(self):
        with mock.patch.object(ai, "_ask_claude", return_value=claude_reply(2)):
            with self.assertRaisesMessage(ai.GenerationError, "wrote 2 questions but the blueprint asks for 3"):
                ai.generate_final_paper(self.bp, self.controller)
        self.assertFalse(FinalPaper.objects.filter(questions__isnull=False).exists())

    def one_question(self, text="Name the gas plants take in.?"):
        return {"questions": [{"section": "x", "question_type": "short", "text": text, "options": [],
                               "answer": "Carbon dioxide", "explanation": "", "marks": 9,
                               "source_reference": "Ch 2"}]}

    def test_regenerate_replaces_only_that_question(self):
        paper = self.make_paper()
        before = list(paper.questions.values_list("text", flat=True))
        with mock.patch.object(ai, "_ask_claude", return_value=self.one_question()) as ask:
            q = ai.regenerate_final_question(paper, 3, self.controller)
        self.assertEqual((q.order, q.section, q.question_type, q.marks), (3, "B", "short", 5))
        self.assertEqual(q.text, "Name the gas plants take in.?")
        self.assertIn("AI review", q.check_notes)
        self.assertEqual(list(paper.questions.values_list("text", flat=True))[:2], before[:2])
        prompt = ask.call_args.args[1][0]["content"][-1]["text"]
        self.assertIn("Question 0?", prompt)  # tells Claude what not to repeat
        self.assertEqual(workflow.paper_problems(paper), [])

    def test_regenerated_question_close_to_another_is_flagged(self):
        paper = self.make_paper()
        with mock.patch.object(ai, "_ask_claude", return_value=self.one_question("Question 0?")):
            q = ai.regenerate_final_question(paper, 3, self.controller)
        self.assertIn("Very similar to question 1.", q.check_notes)

    def test_locked_paper_question_cannot_be_regenerated(self):
        paper = self.make_paper()
        workflow.lock_final(paper, self.controller)
        with mock.patch.object(ai, "_ask_claude", return_value=self.one_question()):
            with self.assertRaisesMessage(ai.GenerationError, "locked"):
                ai.regenerate_final_question(paper, 1, self.controller)

    def test_unknown_question_number_is_refused(self):
        with self.assertRaisesMessage(ai.GenerationError, "no question 9"):
            ai.regenerate_final_question(self.make_paper(), 9, self.controller)

    def test_question_regeneration_runs_in_the_background_and_notifies(self):
        paper = self.make_paper()
        with mock.patch.object(ai.threading, "Thread") as thread:
            ai.start_question_regeneration(paper, 2, self.controller)
        thread.return_value.start.assert_called_once()
        paper.refresh_from_db()
        self.assertTrue(paper.is_generating)
        with self.assertRaisesMessage(ai.GenerationError, "already writing"):
            ai.start_question_regeneration(paper, 2, self.controller)
        with mock.patch.object(ai, "_ask_claude", return_value=self.one_question()):
            ai.rewrite_final_question(paper.pk, 2, self.controller.pk)
        paper.refresh_from_db()
        self.assertFalse(paper.is_generating)
        self.assertEqual(paper.questions.get(order=2).text, "Name the gas plants take in.?")
        self.assertTrue(Notification.objects.filter(recipient=self.controller,
                                                    title="Question 2 ready: Science mid-term").exists())

    def test_page_regenerates_one_question(self):
        paper = self.make_paper()
        self.client.force_login(self.controller)
        url = reverse("admin:questionbank_finalpaper_change", args=[paper.pk])
        self.assertContains(self.client.get(url), 'name="_regenerate_one"')
        with mock.patch.object(ai.threading, "Thread"):
            response = self.client.post(url, {"_regenerate_one": "1", "question_order": "2"}, follow=True)
        self.assertContains(response, "The AI is writing a new question 2")
        paper.refresh_from_db()
        self.assertTrue(paper.is_generating)

    def test_page_rejects_a_bad_question_number(self):
        paper = self.make_paper()
        self.client.force_login(self.controller)
        url = reverse("admin:questionbank_finalpaper_change", args=[paper.pk])
        response = self.client.post(url, {"_regenerate_one": "1", "question_order": "x"}, follow=True)
        self.assertContains(response, "Choose a question number.")
        paper.refresh_from_db()
        self.assertFalse(paper.is_generating)

    def test_generation_runs_in_the_background_and_notifies(self):
        with mock.patch.object(ai.threading, "Thread") as thread:
            paper = ai.start_final_paper(self.bp, self.controller)
        thread.return_value.start.assert_called_once()
        self.assertTrue(paper.is_generating)
        with self.assertRaisesMessage(ai.GenerationError, "already writing"):
            ai.start_final_paper(self.bp, self.controller)

        with mock.patch.object(ai, "_ask_claude", return_value=claude_reply(3)):
            ai.write_final_paper(paper.pk, self.controller.pk)
        paper.refresh_from_db()
        self.assertFalse(paper.is_generating)
        self.assertEqual(paper.questions.count(), 3)
        self.assertTrue(Notification.objects.filter(recipient=self.controller, title__startswith="Final paper ready").exists())
        self.assertEqual(list(AuditEvent.objects.order_by("pk").values_list("action", flat=True)),
                         [AuditEvent.Action.FINAL_STARTED, AuditEvent.Action.FINAL_GENERATED])

    def test_failed_background_generation_is_shown(self):
        with mock.patch.object(ai.threading, "Thread"):
            paper = ai.start_final_paper(self.bp, self.controller)
        with mock.patch.object(ai, "_ask_claude", side_effect=ai.GenerationError("Claude is busy")):
            ai.write_final_paper(paper.pk, self.controller.pk)
        paper.refresh_from_db()
        self.assertEqual(paper.generation_error, "Claude is busy")
        self.assertFalse(paper.is_generating)


class AIUsageTests(BlueprintTestCase):
    def fake_response(self, text):
        return SimpleNamespace(
            _request_id="req_1", model="claude-test", stop_reason="end_turn",
            content=[SimpleNamespace(type="text", text=text)],
            usage=SimpleNamespace(input_tokens=1200, output_tokens=300, cache_read_input_tokens=1000,
                                  cache_creation_input_tokens=0))

    def ask(self, **kwargs):
        stream = mock.MagicMock()
        stream.__enter__.return_value.get_final_message.return_value = self.fake_response('{"questions": []}')
        with mock.patch.object(ai.anthropic, "Anthropic") as client, \
                mock.patch.dict("os.environ", {"ANTHROPIC_API_KEY": "test-key", "GEMINI_API_KEY": ""}):
            client.return_value.beta.messages.stream.return_value = stream
            return ai._ask_claude("system", [], ai.OUTPUT_SCHEMA, **kwargs)

    def test_every_claude_call_is_recorded(self):
        self.ask(purpose=AIUsage.Purpose.QUESTIONS, user=self.controller)
        usage = AIUsage.objects.get()
        self.assertEqual((usage.purpose, usage.user, usage.model), ("questions", self.controller, "claude-test"))
        self.assertEqual((usage.input_tokens, usage.output_tokens, usage.cache_read_tokens), (1200, 300, 1000))

    def test_recording_failure_does_not_break_generation(self):
        with mock.patch.object(AIUsage.objects, "create", side_effect=RuntimeError("db down")):
            self.assertEqual(self.ask(purpose="questions"), {"questions": []})

    def test_super_admin_sees_this_months_totals(self):
        AIUsage.objects.create(purpose="questions", model="m", input_tokens=1000, output_tokens=200)
        AIUsage.objects.create(purpose="final_paper", model="m", input_tokens=3000, output_tokens=800)
        self.client.force_login(self.controller)
        response = self.client.get(reverse("admin:questionbank_aiusage_changelist"))
        self.assertContains(response, "This month: 2 requests · 4000 input tokens · 1000 output tokens")

    def test_hod_cannot_see_usage(self):
        self.client.force_login(self.hod)
        self.assertEqual(self.client.get(reverse("admin:questionbank_aiusage_changelist")).status_code, 403)


class AuthorCannotSeeOwnPaperTests(BlueprintTestCase):
    """Whoever wrote, edited or reviewed a blueprint must not generate or read its confidential paper, even with
    a role that sees final papers (Admin has every permission)."""

    def paper(self):
        with mock.patch.object(ai, "_ask_claude", return_value=claude_reply(3)):
            return ai.generate_final_paper(self.bp, self.controller)

    def test_creator_reviewer_or_editor_is_involved(self):
        self.assertFalse(workflow.involved_in(self.bp, self.controller))
        for field in ("created_by", "reviewed_by"):
            QuestionBlueprint.objects.filter(pk=self.bp.pk).update(**{field: self.controller})
            self.assertTrue(workflow.involved_in(QuestionBlueprint.objects.get(pk=self.bp.pk), self.controller), field)
            QuestionBlueprint.objects.filter(pk=self.bp.pk).update(**{field: None})
        from django.contrib.admin.models import CHANGE, LogEntry
        from django.contrib.contenttypes.models import ContentType
        LogEntry.objects.create(user=self.controller, content_type=ContentType.objects.get_for_model(QuestionBlueprint),
                                object_id=str(self.bp.pk), object_repr="x", action_flag=CHANGE)
        self.assertTrue(workflow.involved_in(self.bp, self.controller))

    def test_involved_controller_cannot_generate(self):
        QuestionBlueprint.objects.filter(pk=self.bp.pk).update(created_by=self.controller)
        self.bp.refresh_from_db()
        with mock.patch.object(ai.threading, "Thread"):
            with self.assertRaisesMessage(ai.GenerationError, "wrote, edited or reviewed"):
                ai.start_final_paper(self.bp, self.controller)

    def test_involved_controller_cannot_open_print_or_list_the_paper(self):
        paper = self.paper()
        QuestionBlueprint.objects.filter(pk=self.bp.pk).update(reviewed_by=self.controller)
        self.client.force_login(self.controller)
        page = self.client.get(reverse("admin:questionbank_finalpaper_change", args=[paper.pk]))
        self.assertNotEqual(page.status_code, 200)
        self.assertEqual(self.client.get(reverse("admin:questionbank_final_print", args=[paper.pk])).status_code, 403)
        listing = self.client.get(reverse("admin:questionbank_finalpaper_changelist"))
        self.assertNotContains(listing, "Science mid-term")

    def test_uninvolved_controller_still_can(self):
        paper = self.paper()
        self.client.force_login(self.controller)
        self.assertEqual(self.client.get(reverse("admin:questionbank_finalpaper_change", args=[paper.pk])).status_code, 200)


@override_settings(**AI_DEFAULTS)
class ClaudeClientTests(TestCase):
    """Keys that aren't scoped to a workspace need the anthropic-workspace-id header on every request."""

    @override_settings(ANTHROPIC_WORKSPACE_ID="wrkspc_test")
    def test_workspace_id_is_sent_when_set(self):
        with mock.patch.object(ai.anthropic, "Anthropic") as client, \
                mock.patch.dict("os.environ", {"ANTHROPIC_API_KEY": "test-key"}):
            ai.claude_client()
        client.assert_called_once_with(default_headers={"anthropic-workspace-id": "wrkspc_test"})

    @override_settings(ANTHROPIC_WORKSPACE_ID="")
    def test_no_workspace_header_when_not_set(self):
        with mock.patch.object(ai.anthropic, "Anthropic") as client, \
                mock.patch.dict("os.environ", {"ANTHROPIC_API_KEY": "test-key"}):
            ai.claude_client()
        client.assert_called_once_with()

    def test_missing_key_gives_a_plain_message_instead_of_crashing(self):
        with mock.patch.dict("os.environ", {"ANTHROPIC_API_KEY": "", "GEMINI_API_KEY": ""}):
            with self.assertRaisesMessage(ai.GenerationError, "ANTHROPIC_API_KEY"):
                ai._ask_claude("system", [], ai.OUTPUT_SCHEMA, purpose="questions")


class LearningFromApprovedTests(BlueprintTestCase):
    """Approved bank questions are sent as style examples, so generation follows what HODs accept."""

    def add(self, text, status="approved", **fields):
        values = {"subject": self.bp.subject, "grade": self.bp.grade, "question_type": "short",
                  "difficulty": "easy", "answer": "A", "text": text, "status": status, **fields}
        return Question.objects.create(**values)

    def prompt(self, **kwargs):
        reply = {"questions": [{"text": "New?", "options": [], "answer": "A", "explanation": "", "source_reference": ""}]}
        args = {"subject": self.bp.subject, "grade": self.bp.grade, "topic": "", "question_type": "short",
                "difficulty": "easy", "count": 1, "language": "en", "user": self.controller, **kwargs}
        with mock.patch.object(ai, "_ask_claude", return_value=reply) as ask:
            ai.generate_questions(**args)
        return ask.call_args.args[1][0]["content"][-1]["text"]

    def test_only_approved_questions_of_the_same_kind_are_examples(self):
        self.add("Approved short question?")
        self.add("Draft question?", status="draft")
        self.add("Rejected question?", status="rejected")
        self.add("Approved MCQ?", question_type="mcq")
        self.add("Other grade question?", grade=Grade.objects.create(name="Class 9"))
        prompt = self.prompt()
        self.assertIn("Approved short question?", prompt)
        for text in ("Draft question?", "Rejected question?", "Approved MCQ?", "Other grade question?"):
            self.assertNotIn(text, prompt)
        self.assertIn("do not repeat or reword", prompt)

    def test_closest_matches_come_first_and_the_number_is_limited(self):
        for n in range(ai.MAX_EXAMPLES + 3):
            self.add(f"Hard question {n}?", difficulty="hard")
        self.add("Easy one on photosynthesis?", topic="Photosynthesis")
        examples = ai.approved_examples(subject=self.bp.subject, grade=self.bp.grade, question_type="short",
                                        difficulty="easy", topic="photosynthesis")
        self.assertEqual(len(examples), ai.MAX_EXAMPLES)
        self.assertEqual(examples[0].text, "Easy one on photosynthesis?")

    def test_no_examples_section_without_approved_questions(self):
        self.assertNotIn("Approved questions from this school", self.prompt())

    def test_teacher_is_told_how_many_examples_were_used(self):
        self.add("Approved short question?")
        reply = {"questions": [{"text": "New?", "options": [], "answer": "A", "explanation": "", "source_reference": ""}]}
        self.client.force_login(self.controller)
        with mock.patch.object(ai, "_ask_claude", return_value=reply):
            response = self.client.post(reverse("admin:questionbank_question_generate"), {
                "subject": self.bp.subject.pk, "grade": self.bp.grade.pk, "topic": "", "question_type": "short",
                "difficulty": "easy", "count": 1, "language": "en"}, follow=True)
        self.assertContains(response, "following 1 approved question(s) from your bank as examples")

    def test_final_papers_never_use_bank_questions(self):
        self.add("Approved short question?")
        with mock.patch.object(ai, "_ask_claude", return_value=claude_reply(3)) as ask:
            ai.generate_final_paper(self.bp, self.controller)
        self.assertNotIn("Approved short question?", ask.call_args_list[0].args[1][0]["content"][-1]["text"])


class DocumentUploadTests(BlueprintTestCase):
    def upload(self, name, content):
        self.client.force_login(self.controller)
        return self.client.post(reverse("admin:questionbank_knowledgesource_add"), {
            "subject": self.bp.subject.pk, "grade": self.bp.grade.pk, "material_type": "lesson_plan",
            "title": "Lesson 1", "chapter": "Chapter 1", "topic": "", "is_active": "on",
            "file": SimpleUploadedFile(name, content),
        })

    def test_word_file_text_is_extracted_and_sent_to_claude(self):
        doc = Document()
        doc.add_paragraph("Photosynthesis happens in chloroplasts.")
        doc.add_table(rows=1, cols=2).rows[0].cells[0].text = "Light"
        buffer = BytesIO()
        doc.save(buffer)
        self.upload("lesson.docx", buffer.getvalue())
        source = KnowledgeSource.objects.get(title="Lesson 1")
        self.assertIn("chloroplasts", source.content_text)
        self.assertIn("Light", source.content_text)
        block = ai._source_block(source)
        self.assertEqual(block["source"]["type"], "text")
        self.assertIn("chloroplasts", block["source"]["data"])
        self.assertEqual(block["title"], "Lesson plan: Lesson 1 · Chapter 1")

    def test_damaged_word_file_is_refused(self):
        response = self.upload("broken.docx", b"not really a word file")
        self.assertContains(response, "couldn")
        self.assertFalse(KnowledgeSource.objects.filter(title="Lesson 1").exists())


class QualityCheckTests(BlueprintTestCase):
    def generate_questions(self, texts):
        reply = {"questions": [{"text": t, "options": [], "answer": "A", "explanation": "", "source_reference": ""}
                               for t in texts]}
        with mock.patch.object(ai, "_ask_claude", return_value=reply):
            return ai.generate_questions(subject=self.bp.subject, grade=self.bp.grade, topic="",
                                         question_type="short", difficulty="easy", count=len(texts),
                                         language="en", user=self.controller)

    def test_duplicates_are_dropped_within_a_batch_and_flagged_against_the_bank(self):
        first = self.generate_questions(["What do plants need for photosynthesis?"])[0]
        created = self.generate_questions([
            "What do plants need for photosynthesis ?",   # same as the bank question
            "Name the green pigment in leaves.",
            "Name the green pigment in leaves",            # repeat within this batch
        ])
        self.assertEqual(len(created), 2)
        self.assertEqual(created[0].duplicate_of_id, first.pk)
        self.assertIsNone(created[1].duplicate_of_id)

    def test_generated_questions_come_back_saved(self):
        # bulk_create can't return new ids on MySQL; callers must still get saved questions with their ids.
        created = self.generate_questions(["Name the green pigment in leaves.", "What do roots absorb?"])
        self.assertTrue(all(q.pk for q in created))
        self.assertEqual({q.pk for q in created}, set(Question.objects.values_list("pk", flat=True)))

    def test_final_paper_flags_copied_samples_repeats_and_ai_review_findings(self):
        item = self.bp.items.get(section="A")
        item.sample_question = "Which gas do plants release during photosynthesis?"
        item.save()
        reply = claude_reply(3)
        reply["questions"][0]["text"] = "Which gas do plants release during photosynthesis?"
        reply["questions"][2]["text"] = reply["questions"][1]["text"]
        review = {"reviews": [
            {"number": 1, "in_syllabus": True, "answer_correct": True, "issue": ""},
            {"number": 2, "in_syllabus": False, "answer_correct": False, "issue": "Chapter 9 isn't in the books."},
            {"number": 3, "in_syllabus": True, "answer_correct": True, "issue": ""},
        ]}
        with mock.patch.object(ai, "_ask_claude", side_effect=[reply, review]):
            paper = ai.generate_final_paper(self.bp, self.controller)
        notes = [q.check_notes for q in paper.questions.all()]
        self.assertIn("sample question", notes[0])
        self.assertIn("May not be covered", notes[1])
        self.assertIn("The answer may be wrong", notes[1])
        self.assertIn("Chapter 9", notes[1])
        self.assertIn("Very similar to question 2", notes[2])
        self.assertTrue(paper.check_summary.startswith("3 of 3 questions flagged"))

    def test_final_paper_is_saved_even_if_the_review_pass_fails(self):
        with mock.patch.object(ai, "_ask_claude", side_effect=[claude_reply(3), ai.GenerationError("busy")]):
            paper = ai.generate_final_paper(self.bp, self.controller)
        self.assertEqual(paper.questions.count(), 3)
        self.assertIn("couldn't run", paper.check_summary)


class MaterialChoiceTests(BlueprintTestCase):
    def setUp(self):
        super().setUp()
        self.book = KnowledgeSource.objects.get(title="Book")
        self.chapter = KnowledgeSource.objects.create(
            subject=self.bp.subject, grade=self.bp.grade, title="Chapter 5", chapter="Chapter 5",
            file=SimpleUploadedFile("ch5.txt", b"Respiration releases energy."))

    @staticmethod
    def sent_titles(ask):
        content = ask.call_args.args[1][0]["content"]
        return [block["title"] for block in content if block["type"] == "document"]

    def test_only_chosen_materials_reach_claude(self):
        with mock.patch.object(ai, "_ask_claude", return_value=claude_reply(1)) as ask:
            ai.generate_questions(subject=self.bp.subject, grade=self.bp.grade, topic="", question_type="short",
                                  difficulty="easy", count=1, language="en", user=self.controller,
                                  sources=[self.chapter])
        self.assertEqual(self.sent_titles(ask), ["Textbook: Chapter 5 · Chapter 5"])

    def test_blueprint_materials_limit_the_final_paper(self):
        self.bp.materials.add(self.chapter)
        with mock.patch.object(ai, "_ask_claude", return_value=claude_reply(3)) as ask:
            ai.generate_final_paper(self.bp, self.controller)
        self.assertEqual(self.sent_titles(ask), ["Textbook: Chapter 5 · Chapter 5"])

    def test_generate_page_lists_materials_with_their_subject_and_grade(self):
        self.client.force_login(self.controller)
        response = self.client.get(reverse("admin:questionbank_question_generate"))
        self.assertContains(response, f'data-pair="{self.bp.subject.pk}:{self.bp.grade.pk}"', count=2)

    def test_material_for_another_subject_is_refused(self):
        other = KnowledgeSource.objects.create(
            subject=Subject.objects.create(name="History"), grade=self.bp.grade, title="History book",
            file=SimpleUploadedFile("h.txt", b"Kings"))
        form = GenerateQuestionsForm(data={
            "subject": self.bp.subject.pk, "grade": self.bp.grade.pk, "question_type": "short",
            "difficulty": "easy", "count": 1, "language": "en", "sources": [other.pk]})
        self.assertFalse(form.is_valid())
        self.assertIn("isn't for Science / Class 8", form.non_field_errors()[0])


class ExportTests(BlueprintTestCase):
    def setUp(self):
        super().setUp()
        reply = claude_reply(3)
        reply["questions"][0].update(text="সালোকসংশ্লেষণ কী?", options=["ক", "খ", "গ", "ঘ"], answer="ক")
        with mock.patch.object(ai, "_ask_claude", return_value=reply):
            self.paper = ai.generate_final_paper(self.bp, self.controller)

    def download(self, fmt, user=None, answers=False):
        self.client.force_login(user or self.controller)
        url = reverse("admin:questionbank_final_export", args=[self.paper.pk, fmt])
        return self.client.get(url + ("?answers=1" if answers else ""))

    def test_unlocked_paper_cannot_be_downloaded(self):
        self.assertEqual(self.download("pdf").status_code, 302)

    def test_locked_paper_downloads_as_pdf_and_word(self):
        workflow.lock_final(self.paper, self.controller)
        pdf = self.download("pdf")
        self.assertEqual(pdf["Content-Type"], "application/pdf")
        self.assertIn("science-mid-term-question-paper.pdf", pdf["Content-Disposition"])
        text = pymupdf.open(stream=pdf.content, filetype="pdf")[0].get_text()
        self.assertIn("Total marks: 7", text)
        self.assertIn("Question 1?", text)

        word = Document(BytesIO(self.download("docx", answers=True).content))
        body = "\n".join(p.text for p in word.paragraphs)
        self.assertIn("সালোকসংশ্লেষণ কী?", body)
        self.assertIn("Answer: ক", body)
        self.assertEqual(AuditEvent.objects.filter(action=AuditEvent.Action.FINAL_EXPORTED).count(), 2)

    def test_hod_cannot_download(self):
        workflow.lock_final(self.paper, self.controller)
        self.assertEqual(self.download("pdf", user=self.hod).status_code, 403)


@override_settings(MAKE_WEBHOOK_URL="", MAKE_MAILHOOK_ADDRESS="")
class ReminderTests(BlueprintTestCase):
    def setUp(self):
        super().setUp()
        User = get_user_model()
        self.teacher = User.objects.create_user("teacher", "teacher@example.com", "Pw-12345!", is_staff=True)
        self.teacher.user_permissions.add(Permission.objects.get(codename="add_question"))
        self.teacher = User.objects.get(pk=self.teacher.pk)
        self.bp.assigned_to, self.bp.status = self.teacher, QuestionBlueprint.Status.DRAFT
        self.bp.due_date = date.today()
        self.bp.save()

    def deadline(self, subject):
        return AcademicEvent.objects.create(title="Papers due", event_type=AcademicEvent.EventType.QUESTION_DEADLINE,
                                            start_date=date.today(), subject=subject)

    def test_deadline_reminders_only_for_their_subject(self):
        self.deadline(self.bp.subject)
        self.deadline(Subject.objects.create(name="History"))
        titles = [n.title for n in ensure_reminders(self.teacher)]
        self.assertEqual(sum("Question deadline" in t for t in titles), 1)

    def test_daily_command_emails_new_reminders_once(self):
        call_command("send_reminders", stdout=StringIO())
        self.assertEqual([m.to for m in mail.outbox], [["teacher@example.com"]])
        self.assertIn("Science mid-term", mail.outbox[0].body)
        call_command("send_reminders", stdout=StringIO())
        self.assertEqual(len(mail.outbox), 1)

    def exam_in(self, days):
        self.bp.exam = AcademicEvent.objects.create(
            title="Mid-term exams", event_type=AcademicEvent.EventType.INTERNAL_EXAM,
            start_date=timezone.localdate() + timedelta(days=days))
        self.bp.save()

    def test_exam_controller_is_warned_when_an_exam_is_near_and_the_paper_is_not_final(self):
        self.exam_in(5)
        created = ensure_reminders(self.controller)
        self.assertEqual([n.title for n in created], ["Exam paper not final: Science mid-term (starts in 5 days)"])
        self.assertEqual(ensure_reminders(self.controller), [])     # once per reminder step
        self.assertEqual(ensure_reminders(self.hod), [])            # only exam controllers

    def test_no_exam_warning_far_ahead_or_once_the_paper_is_locked(self):
        self.exam_in(30)
        self.assertEqual(ensure_reminders(self.controller), [])
        self.exam_in(2)
        with mock.patch.object(ai, "_ask_claude", return_value=claude_reply(3)):
            workflow.lock_final(ai.generate_final_paper(self.bp, self.controller), self.controller)
        self.assertEqual(ensure_reminders(self.controller), [])


class RoleTests(BlueprintTestCase):
    def setUp(self):
        super().setUp()
        call_command("setup_academics", stdout=StringIO())
        User = get_user_model()
        self.teacher = User.objects.create_user("teacher", password="Pw-12345!", is_staff=True)
        self.teacher.groups.add(Group.objects.get(name="Teacher / Subject Expert"))
        self.viewer = User.objects.create_user("viewer", password="Pw-12345!", is_staff=True)
        self.viewer.groups.add(Group.objects.get(name="Read-Only"))
        for user in (self.teacher, self.viewer):
            SubjectAssignment.objects.create(user=user, subject=self.bp.subject)
        self.book = KnowledgeSource.objects.get()  # uploaded by nobody in particular

    def upload(self):
        return self.client.post(reverse("admin:questionbank_knowledgesource_add"), {
            "subject": self.bp.subject.pk, "grade": self.bp.grade.pk, "material_type": "syllabus",
            "title": "Science syllabus", "chapter": "", "topic": "", "is_active": "on",
            "file": SimpleUploadedFile("syllabus.txt", b"Unit 1: Cells"),
        })

    def test_teacher_uploads_and_edits_only_own_materials(self):
        self.client.force_login(self.teacher)
        self.upload()
        mine = KnowledgeSource.objects.get(title="Science syllabus")
        self.assertEqual(mine.uploaded_by, self.teacher)
        self.assertEqual(mine.label, "Syllabus: Science syllabus")
        change = "admin:questionbank_knowledgesource_change"
        self.assertContains(self.client.get(reverse(change, args=[mine.pk])), 'name="_save"')
        self.assertNotContains(self.client.get(reverse(change, args=[self.book.pk])), 'name="_save"')

    def test_read_only_user_can_view_but_not_add(self):
        self.client.force_login(self.viewer)
        self.assertEqual(self.client.get(reverse("admin:questionbank_knowledgesource_changelist")).status_code, 200)
        self.assertEqual(self.client.get(reverse("admin:questionbank_question_changelist")).status_code, 200)
        self.assertEqual(self.upload().status_code, 403)
        self.assertEqual(self.client.get(reverse("admin:questionbank_questionblueprint_changelist")).status_code, 403)


class ReviewTests(BlueprintTestCase):
    def test_hod_cannot_approve_own_blueprint(self):
        self.bp.status = QuestionBlueprint.Status.IN_REVIEW
        self.bp.save()
        with self.assertRaisesMessage(workflow.WorkflowError, "your own work"):
            workflow.approve(self.bp, self.hod)
        workflow.approve(self.bp, self.controller)  # someone else can
        self.assertEqual(self.bp.status, QuestionBlueprint.Status.APPROVED)

    def test_super_admin_cannot_approve_own_blueprint_either(self):
        self.bp.status, self.bp.assigned_to = QuestionBlueprint.Status.IN_REVIEW, self.controller
        self.bp.save()
        with self.assertRaisesMessage(workflow.WorkflowError, "your own work"):
            workflow.approve(self.bp, self.controller)


class SubjectScopeTests(BlueprintTestCase):
    """A Physics teacher or HOD must not see or act on Science work."""

    def setUp(self):
        super().setUp()
        call_command("setup_academics", stdout=StringIO())
        User = get_user_model()
        self.physics = Subject.objects.create(name="Physics")
        self.science, self.class8 = self.bp.subject, self.bp.grade
        self.class9 = Grade.objects.create(name="Class 9")

        def person(name, role, *assignments):
            user = User.objects.create_user(name, password="Pw-12345!", is_staff=True)
            user.groups.add(Group.objects.get(name=role))
            for subject, grade in assignments:
                SubjectAssignment.objects.create(user=user, subject=subject, grade=grade)
            return user

        self.physics_teacher = person("phy-teacher", "Teacher / Subject Expert", (self.physics, None))
        self.physics_hod = person("phy-hod", "Head of Department", (self.physics, None))
        self.science_hod = person("sci-hod", "Head of Department", (self.science, self.class8))
        self.unassigned = person("new-teacher", "Teacher / Subject Expert")
        q = lambda subject, grade, text: Question.objects.create(subject=subject, grade=grade, question_type="short",
                                                                 text=text, answer="A", created_by=self.controller)
        self.science_q = q(self.science, self.class8, "Science class 8 question")
        self.science9_q = q(self.science, self.class9, "Science class 9 question")
        self.physics_q = q(self.physics, self.class8, "Physics question")

    def question_list(self, user):
        self.client.force_login(user)
        return self.client.get(reverse("admin:questionbank_question_changelist"))

    def test_teacher_sees_only_own_subject(self):
        response = self.question_list(self.physics_teacher)
        self.assertContains(response, "Physics question")
        self.assertNotContains(response, "Science class 8 question")
        url = reverse("admin:questionbank_question_change", args=[self.science_q.pk])
        self.assertEqual(self.client.get(url).status_code, 302)  # "doesn't exist" for them

    def test_second_all_grades_assignment_is_rejected(self):
        # Checked by validation, not only the database: MySQL doesn't create the conditional unique constraint.
        duplicate = SubjectAssignment(user=self.physics_teacher, subject=self.physics, grade=None)
        with self.assertRaises(ValidationError):
            duplicate.full_clean()

    def test_only_known_conditional_constraints_exist(self):
        # settings silences models.W036, MySQL's "conditional constraint not created" warning. That's safe only for
        # constraints that are also enforced by validation. A new one must be checked like the one above, then
        # listed here; otherwise MySQL would skip it silently.
        conditional = {
            f"{model._meta.label}.{c.name}"
            for model in apps.get_models()
            for c in model._meta.constraints
            if isinstance(c, UniqueConstraint) and c.condition is not None
        }
        self.assertEqual(conditional, {"questionbank.SubjectAssignment.one_all_grades_assignment"})

    def test_grade_specific_assignment(self):
        response = self.question_list(self.science_hod)
        self.assertContains(response, "Science class 8 question")
        self.assertNotContains(response, "Science class 9 question")

    def test_hod_cannot_approve_another_subjects_questions(self):
        self.client.force_login(self.physics_hod)
        self.client.post(reverse("admin:questionbank_question_changelist"),
                         {"action": "approve", "_selected_action": [self.science_q.pk, self.physics_q.pk]})
        self.science_q.refresh_from_db()
        self.physics_q.refresh_from_db()
        self.assertEqual(self.science_q.status, Question.Status.DRAFT)
        self.assertEqual(self.physics_q.status, Question.Status.APPROVED)

    def test_teacher_cannot_add_material_for_another_subject(self):
        self.client.force_login(self.physics_teacher)
        self.client.post(reverse("admin:questionbank_knowledgesource_add"), {
            "subject": self.science.pk, "grade": self.class8.pk, "material_type": "textbook", "title": "Sneaky",
            "chapter": "", "topic": "", "is_active": "on", "file": SimpleUploadedFile("s.txt", b"x")})
        self.assertFalse(KnowledgeSource.objects.filter(title="Sneaky").exists())

    def test_generate_form_offers_only_assigned_subjects(self):
        form = GenerateQuestionsForm(user=self.physics_teacher)
        self.assertEqual(list(form.fields["subject"].queryset), [self.physics])

    def test_hods_see_and_are_notified_about_their_subjects_blueprints_only(self):
        self.bp.status, self.bp.assigned_to = QuestionBlueprint.Status.DRAFT, self.controller
        self.bp.save()
        workflow.submit(self.bp, self.controller)
        self.assertTrue(Notification.objects.filter(recipient=self.science_hod, title__contains="to review").exists())
        self.assertFalse(Notification.objects.filter(recipient=self.physics_hod, title__contains="to review").exists())
        self.client.force_login(self.physics_hod)
        url = reverse("admin:questionbank_questionblueprint_change", args=[self.bp.pk])
        self.assertEqual(self.client.get(url).status_code, 302)

    def test_no_assignment_means_nothing_and_a_message(self):
        response = self.question_list(self.unassigned)
        self.assertContains(response, "aren&#x27;t assigned to any subjects")
        self.assertNotContains(response, "Physics question")

    def test_exam_controller_and_super_admin_see_every_subject(self):
        response = self.question_list(self.controller)
        for text in ("Physics question", "Science class 8 question", "Science class 9 question"):
            self.assertContains(response, text)

    def test_only_super_admins_manage_assignments(self):
        self.client.force_login(self.science_hod)
        self.assertEqual(self.client.get(reverse("admin:questionbank_subjectassignment_add")).status_code, 403)


class QuestionApprovalTests(BlueprintTestCase):
    def setUp(self):
        super().setUp()
        call_command("setup_academics", stdout=StringIO())
        User = get_user_model()
        self.teacher = User.objects.create_user("teacher", password="Pw-12345!", is_staff=True)
        self.teacher.groups.add(Group.objects.get(name="Teacher / Subject Expert"))
        self.head = User.objects.create_user("head", password="Pw-12345!", is_staff=True)
        self.head.groups.add(Group.objects.get(name="Head of Department"))
        for user in (self.teacher, self.head):
            SubjectAssignment.objects.create(user=user, subject=self.bp.subject)
        make = lambda by, text: Question.objects.create(subject=self.bp.subject, grade=self.bp.grade,
                                                        question_type="short", text=text, answer="A", created_by=by)
        self.teachers_q, self.heads_q = make(self.teacher, "Teacher's question"), make(self.head, "HOD's question")

    def act(self, user, action, *questions):
        self.client.force_login(user)
        return self.client.post(reverse("admin:questionbank_question_changelist"),
                                {"action": action, "_selected_action": [q.pk for q in questions]})

    def status(self, q):
        q.refresh_from_db()
        return q.status

    def test_teacher_cannot_approve_questions(self):
        self.act(self.teacher, "approve", self.teachers_q)
        self.assertEqual(self.status(self.teachers_q), Question.Status.DRAFT)

    def test_teacher_cannot_approve_by_editing_status(self):
        self.client.force_login(self.teacher)
        url = reverse("admin:questionbank_question_change", args=[self.teachers_q.pk])
        self.assertNotContains(self.client.get(url), 'name="status"')

    def test_hod_approves_others_but_not_own(self):
        self.act(self.head, "approve", self.teachers_q, self.heads_q)
        self.assertEqual(self.status(self.teachers_q), Question.Status.APPROVED)
        self.assertEqual(self.status(self.heads_q), Question.Status.DRAFT)

    def test_editing_an_approved_question_sends_it_back_to_draft(self):
        self.act(self.head, "approve", self.teachers_q)
        self.client.force_login(self.teacher)
        self.client.post(reverse("admin:questionbank_question_change", args=[self.teachers_q.pk]), {
            "subject": self.bp.subject.pk, "grade": self.bp.grade.pk, "topic": "", "question_type": "short",
            "difficulty": "medium", "text": "Teacher's question, reworded", "options": "[]", "answer": "A",
            "explanation": "", "source_reference": ""})
        self.assertEqual(self.status(self.teachers_q), Question.Status.DRAFT)


class ProviderStatusPageTests(BlueprintTestCase):
    """The status box names the active provider and model; under Gemini the final-paper pages say papers go to
    Google, and without GEMINI_PAID_TIER the generate/regenerate buttons are disabled with a plain reason."""

    GEMINI = {"AI_PROVIDER": "gemini", "AI_MODEL": "gemini-bank-test", "AI_FINAL_MODEL": "gemini-final-test"}

    def setUp(self):
        super().setUp()
        self.client.force_login(self.controller)
        self.bp_url = reverse("admin:questionbank_questionblueprint_change", args=[self.bp.pk])

    def make_paper(self):
        with mock.patch.object(ai, "_ask_claude", return_value=claude_reply(3)):
            return ai.generate_final_paper(self.bp, self.controller)

    @override_settings(AI_PROVIDER="anthropic", AI_MODEL="", AI_FINAL_MODEL="", QUESTION_GENERATION_MODEL="claude-test")
    @mock.patch.dict(os.environ, {"ANTHROPIC_API_KEY": "test"})
    def test_default_provider_shows_claude_and_allows_final_papers(self):
        response = self.client.get(self.bp_url)
        self.assertContains(response, "AI connected: Claude · model <code>claude-test</code>")
        self.assertContains(response, 'name="_generate_final"')
        self.assertNotContains(response, "processed by Google")
        self.assertNotContains(response, "GEMINI_PAID_TIER")
        self.assertNotContains(response, "Claude is")

    @override_settings(AI_PROVIDER="anthropic")
    @mock.patch.dict(os.environ, {"ANTHROPIC_API_KEY": ""})
    def test_missing_key_names_the_providers_key(self):
        response = self.client.get(self.bp_url)
        self.assertContains(response, "The AI (Claude) isn't connected yet. Add <code>ANTHROPIC_API_KEY</code>")
        with self.settings(**self.GEMINI), mock.patch.dict(os.environ, {"GEMINI_API_KEY": ""}):
            response = self.client.get(self.bp_url)
        self.assertContains(response, "The AI (Gemini) isn't connected yet. Add <code>GEMINI_API_KEY</code>")

    @mock.patch.dict(os.environ, {"GEMINI_API_KEY": "test"})
    def test_gemini_free_tier_blocks_generate_final_with_reason(self):
        with self.settings(**self.GEMINI, GEMINI_PAID_TIER=False):
            response = self.client.get(self.bp_url)
        self.assertContains(response, "AI connected: Gemini · model <code>gemini-bank-test</code>")
        self.assertContains(response, "final papers <code>gemini-final-test</code>")
        self.assertContains(response, "Final papers can't be written with Gemini until the paid plan is switched on "
                                      "(GEMINI_PAID_TIER).")
        self.assertNotContains(response, 'name="_generate_final"')
        self.assertContains(response, 'disabled aria-describedby="ai-final-blocked"')

    @mock.patch.dict(os.environ, {"GEMINI_API_KEY": "test"})
    def test_gemini_paid_tier_allows_generate_final_with_google_note(self):
        with self.settings(**self.GEMINI, GEMINI_PAID_TIER=True):
            response = self.client.get(self.bp_url)
        self.assertContains(response, 'name="_generate_final"')
        self.assertContains(response, "Final papers are processed by Google (Gemini).")
        self.assertNotContains(response, "GEMINI_PAID_TIER")

    @mock.patch.dict(os.environ, {"GEMINI_API_KEY": "test"})
    def test_final_paper_page_under_gemini(self):
        paper = self.make_paper()
        url = reverse("admin:questionbank_finalpaper_change", args=[paper.pk])
        with self.settings(**self.GEMINI, GEMINI_PAID_TIER=True):
            response = self.client.get(url)
        self.assertContains(response, "Final papers are processed by Google (Gemini).")
        self.assertContains(response, 'name="_regenerate_one"')
        self.assertContains(response, 'name="_regenerate"')
        with self.settings(**self.GEMINI, GEMINI_PAID_TIER=False):
            response = self.client.get(url)
        self.assertContains(response, "Final papers can't be written with Gemini until the paid plan is switched on")
        self.assertNotContains(response, 'name="_regenerate_one"')
        self.assertNotContains(response, 'name="_regenerate"')
        self.assertContains(response, 'name="_lock"')  # locking an existing paper sends nothing to the AI

    @override_settings(AI_PROVIDER="anthropic")
    def test_final_paper_page_on_anthropic_has_no_google_note(self):
        paper = self.make_paper()
        response = self.client.get(reverse("admin:questionbank_finalpaper_change", args=[paper.pk]))
        self.assertContains(response, 'name="_regenerate"')
        self.assertNotContains(response, "processed by Google")
