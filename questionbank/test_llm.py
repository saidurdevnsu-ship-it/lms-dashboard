"""The AI provider layer (questionbank.llm): Anthropic stays the default, Gemini is ready behind AI_PROVIDER.

No test makes a network call: the Gemini SDK client is replaced by a fake (or, in one test, given a fake HTTP
transport so the real request body can be checked), and the Anthropic client is patched as in tests.py.
"""

import base64
import json
import runpy
from io import StringIO
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import httpx
import httpx2
from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import SimpleTestCase, TestCase, override_settings
from google.genai import errors as genai_errors
from google.genai import types as genai_types

from . import ai, llm
from .models import AIUsage, FinalPaper
from .tests import BlueprintTestCase, claude_reply

SCHEMA = {"type": "object", "properties": {"questions": {"type": "array", "items": {"type": "string"}}},
          "required": ["questions"], "additionalProperties": False}

ANTHROPIC = dict(AI_PROVIDER="anthropic", AI_MODEL="", AI_FINAL_MODEL="", QUESTION_GENERATION_MODEL="claude-test-5")
GEMINI = dict(AI_PROVIDER="gemini", AI_MODEL="", AI_FINAL_MODEL="", GEMINI_PAID_TIER=False)
GEMINI_PAID = {**GEMINI, "GEMINI_PAID_TIER": True}
GEMINI_KEY = {"GEMINI_API_KEY": "test-gemini-key", "GOOGLE_API_KEY": ""}


def reply(text='{"questions": []}', **kwargs):
    fields = dict(text=text, model="m", request_id="r", input_tokens=1, output_tokens=1, cache_read_tokens=0,
                  cache_write_tokens=0)
    fields.update(kwargs)
    return llm.Reply(**fields)


# ---------- settings ----------

class SettingsFileTests(SimpleTestCase):
    """config/settings.py reads AI_PROVIDER from the environment and refuses values it doesn't know."""

    def load(self, **env):
        path = Path(settings.BASE_DIR) / "config" / "settings.py"
        with mock.patch.dict("os.environ", env):
            return runpy.run_path(str(path))

    def test_unknown_provider_is_rejected(self):
        with self.assertRaisesMessage(ImproperlyConfigured, "AI_PROVIDER"):
            self.load(AI_PROVIDER="openai")

    def test_blank_provider_means_anthropic(self):
        self.assertEqual(self.load(AI_PROVIDER="")["AI_PROVIDER"], "anthropic")

    def test_values_are_read_from_the_environment(self):
        loaded = self.load(AI_PROVIDER=" Gemini ", AI_MODEL="gemini-x", AI_FINAL_MODEL="gemini-y",
                           GEMINI_PAID_TIER="true")
        self.assertEqual((loaded["AI_PROVIDER"], loaded["AI_MODEL"], loaded["AI_FINAL_MODEL"],
                          loaded["GEMINI_PAID_TIER"]), ("gemini", "gemini-x", "gemini-y", True))

    def test_paid_tier_is_off_unless_set_to_true(self):
        self.assertFalse(self.load(GEMINI_PAID_TIER="")["GEMINI_PAID_TIER"])


class ModelChoiceTests(SimpleTestCase):
    @override_settings(**ANTHROPIC)
    def test_anthropic_uses_question_generation_model_for_both(self):
        self.assertEqual((llm.model_for(), llm.model_for(final=True)), ("claude-test-5", "claude-test-5"))

    @override_settings(**GEMINI)
    def test_gemini_defaults(self):
        self.assertEqual((llm.model_for(), llm.model_for(final=True)), ("gemini-3.8-flash", "gemini-4-argon"))

    @override_settings(**{**GEMINI, "AI_MODEL": "gemini-3.5-flash-lite", "AI_FINAL_MODEL": "gemini-9"})
    def test_explicit_models_win(self):
        self.assertEqual((llm.model_for(), llm.model_for(final=True)), ("gemini-3.5-flash-lite", "gemini-9"))

    @override_settings(AI_PROVIDER="openai")
    def test_unknown_provider_is_rejected_at_call_time(self):
        with self.assertRaisesMessage(ImproperlyConfigured, "AI_PROVIDER"):
            llm.ask("s", [], SCHEMA, purpose="questions")


# ---------- routing and the paid-tier guard ----------

class DispatchTests(SimpleTestCase):
    def ask(self, **kwargs):
        with mock.patch.object(llm, "_ask_anthropic", return_value=reply()) as anthropic_, \
                mock.patch.object(llm, "_ask_gemini", return_value=reply()) as gemini, \
                mock.patch.object(llm, "record_usage"):
            llm.ask("system", [], SCHEMA, **kwargs)
        return anthropic_, gemini

    @override_settings(**ANTHROPIC)
    def test_anthropic_is_used_when_chosen(self):
        anthropic_, gemini = self.ask(purpose="questions")
        gemini.assert_not_called()
        self.assertEqual(anthropic_.call_args.kwargs["model"], "claude-test-5")

    @override_settings(**GEMINI)
    def test_gemini_is_used_when_chosen(self):
        anthropic_, gemini = self.ask(purpose="questions", effort="high")
        anthropic_.assert_not_called()
        self.assertEqual((gemini.call_args.kwargs["model"], gemini.call_args.kwargs["effort"]),
                         ("gemini-3.8-flash", "high"))

    @override_settings(**{**GEMINI_PAID, "AI_FINAL_MODEL": "gemini-final"})
    def test_final_requests_use_the_final_model(self):
        _, gemini = self.ask(purpose="final_paper", final=True)
        self.assertEqual(gemini.call_args.kwargs["model"], "gemini-final")

    @override_settings(**{**ANTHROPIC, "AI_FINAL_MODEL": "claude-final"})
    def test_final_purposes_count_as_final_even_without_the_flag(self):
        for purpose in ("final_paper", "final_review", "final_question"):
            anthropic_, _ = self.ask(purpose=purpose)
            self.assertEqual(anthropic_.call_args.kwargs["model"], "claude-final", purpose)

    @override_settings(**GEMINI)
    def test_final_papers_are_refused_on_gemini_without_the_paid_tier(self):
        self.assertFalse(llm.final_papers_allowed())
        for purpose in ("final_paper", "final_review", "final_question"):
            with mock.patch.object(llm, "_ask_gemini") as gemini:
                with self.assertRaisesMessage(llm.GenerationError, "paid"):
                    llm.ask("system", [], SCHEMA, purpose=purpose, final=True)
            gemini.assert_not_called()
        # Bank questions still work.
        _, gemini = self.ask(purpose="questions")
        gemini.assert_called_once()

    @override_settings(**GEMINI_PAID)
    def test_final_papers_are_allowed_with_the_paid_tier(self):
        self.assertTrue(llm.final_papers_allowed())
        _, gemini = self.ask(purpose="final_paper", final=True)
        self.assertEqual(gemini.call_args.kwargs["model"], "gemini-4-argon")

    @override_settings(**ANTHROPIC)
    def test_final_papers_are_always_allowed_on_anthropic(self):
        self.assertTrue(llm.final_papers_allowed())

    @override_settings(**{**GEMINI, "AI_MODEL": "claude-opus-5-5"})
    def test_a_claude_model_is_refused_under_gemini(self):
        with mock.patch.object(llm, "_ask_gemini") as gemini:
            with self.assertRaisesMessage(llm.GenerationError, "AI_MODEL"):
                llm.ask("system", [], SCHEMA, purpose="questions")
        gemini.assert_not_called()

    @override_settings(**{**ANTHROPIC, "AI_MODEL": "gemini-3.8-flash"})
    def test_a_gemini_model_is_refused_under_anthropic(self):
        with self.assertRaisesMessage(llm.GenerationError, "AI_MODEL"):
            llm.ask("system", [], SCHEMA, purpose="questions")

    @override_settings(**ANTHROPIC)
    def test_reply_error_is_raised_after_usage_is_recorded(self):
        with mock.patch.object(llm, "_ask_anthropic", return_value=reply(error="The answer was cut off.")), \
                mock.patch.object(llm, "record_usage") as record:
            with self.assertRaisesMessage(llm.GenerationError, "cut off"):
                llm.ask("system", [], SCHEMA, purpose="questions")
        record.assert_called_once()

    @override_settings(**ANTHROPIC)
    def test_unreadable_reply(self):
        for text in ("not json", '{"other": 1}', "[]"):
            with mock.patch.object(llm, "_ask_anthropic", return_value=reply(text)), \
                    mock.patch.object(llm, "record_usage"):
                with self.assertRaisesMessage(llm.GenerationError, "couldn't be read"):
                    llm.ask("system", [], SCHEMA, purpose="questions")

    @override_settings(**ANTHROPIC)
    def test_unreadable_final_paper_reply_is_not_logged(self):
        """A broken final-paper reply is still confidential paper content: the log names the request, never the text."""
        secret = '{"questions": [{"text": "SECRET-EXAM Explain photosynthesis'
        with mock.patch.object(llm, "_ask_anthropic", return_value=reply(secret, request_id="req_final_9")), \
                mock.patch.object(llm, "record_usage"), self.assertLogs("questionbank.llm", "DEBUG") as logs:
            with self.assertRaisesMessage(llm.GenerationError, "couldn't be read"):
                llm.ask("system", [], SCHEMA, purpose="final_paper", final=True)
        output = "\n".join(logs.output)
        self.assertNotIn("SECRET-EXAM", output)
        self.assertNotIn("photosynthesis", output)
        self.assertIn("final_paper", output)
        self.assertIn("req_final_9", output)
        self.assertIn(str(len(secret)), output)


class StatusTests(SimpleTestCase):
    @override_settings(**ANTHROPIC)
    def test_anthropic_status(self):
        with mock.patch.dict("os.environ", {"ANTHROPIC_API_KEY": "k"}):
            status = ai.ai_status()
        self.assertEqual(status, {"provider": "anthropic", "label": "Claude", "configured": True,
                                  "model": "claude-test-5", "final_model": "claude-test-5", "final_allowed": True})
        with mock.patch.dict("os.environ", {"ANTHROPIC_API_KEY": ""}):
            self.assertFalse(ai.ai_status()["configured"])

    @override_settings(**GEMINI)
    def test_gemini_status(self):
        with mock.patch.dict("os.environ", {**GEMINI_KEY, "ANTHROPIC_API_KEY": ""}):
            status = ai.ai_status()
        self.assertEqual(status, {"provider": "gemini", "label": "Gemini", "configured": True,
                                  "model": "gemini-3.8-flash", "final_model": "gemini-4-argon",
                                  "final_allowed": False})
        with mock.patch.dict("os.environ", {"GEMINI_API_KEY": "", "ANTHROPIC_API_KEY": "k"}):
            self.assertFalse(ai.ai_status()["configured"])

    def test_blank_or_whitespace_key_is_not_connected(self):
        for overrides, key in ((ANTHROPIC, "ANTHROPIC_API_KEY"), (GEMINI, "GEMINI_API_KEY")):
            for value in ("", "   ", " \t\n"):
                with self.subTest(key=key, value=value), self.settings(**overrides), \
                        mock.patch.dict("os.environ", {key: value}):
                    self.assertFalse(llm.status()["configured"])

    @override_settings(**ANTHROPIC)
    def test_claude_status_still_has_the_keys_templates_use(self):
        with mock.patch.dict("os.environ", {"ANTHROPIC_API_KEY": "k"}):
            status = ai.claude_status()
        self.assertEqual((status["configured"], status["model"]), (True, "claude-test-5"))


# ---------- the seam in ai.py ----------

@override_settings(**ANTHROPIC)
class SeamTests(BlueprintTestCase):
    def test_both_names_can_be_patched(self):
        for name in ("_ask_model", "_ask_claude"):
            with mock.patch.object(ai, name, return_value=claude_reply(3)) as ask:
                ai.generate_final_paper(self.bp, self.controller)
            self.assertEqual(ask.call_count, 2, name)  # the paper, then the review pass

    def test_old_name_passes_through_to_the_new_one(self):
        with mock.patch.object(ai, "_ask_model", return_value={"ok": 1}) as ask:
            self.assertEqual(ai._ask_claude("s", [], SCHEMA, purpose="questions"), {"ok": 1})
        ask.assert_called_once_with("s", [], SCHEMA, purpose="questions")

    def test_final_paper_paths_ask_for_the_final_model(self):
        with mock.patch.object(ai, "_ask_model", return_value=claude_reply(3)) as ask:
            paper = ai.generate_final_paper(self.bp, self.controller)
        self.assertEqual([c.kwargs.get("final") for c in ask.call_args_list], [True, True])
        self.assertEqual([c.kwargs["purpose"] for c in ask.call_args_list], ["final_paper", "final_review"])
        self.assertEqual(paper.model_used, "claude-test-5")
        one = {"questions": [claude_reply(1)["questions"][0]]}
        with mock.patch.object(ai, "_ask_model", return_value=one) as ask:
            ai.regenerate_final_question(paper, 1, self.controller)
        self.assertTrue(ask.call_args.kwargs["final"])

    @override_settings(AI_FINAL_MODEL="claude-final")
    def test_paper_records_the_final_model(self):
        with mock.patch.object(ai, "_ask_model", return_value=claude_reply(3)):
            self.assertEqual(ai.generate_final_paper(self.bp, self.controller).model_used, "claude-final")

    @override_settings(AI_FINAL_MODEL="claude-" + "x" * 80)
    def test_long_final_model_name_fits_the_field(self):
        max_length = FinalPaper._meta.get_field("model_used").max_length
        with mock.patch.object(ai, "_ask_model", return_value=claude_reply(3)):
            paper = ai.generate_final_paper(self.bp, self.controller)
        paper.refresh_from_db()
        self.assertEqual(paper.model_used, ("claude-" + "x" * 80)[:max_length])

    def test_bank_questions_are_not_final(self):
        with mock.patch.object(ai, "_ask_model", return_value={"questions": []}) as ask:
            ai.generate_questions(subject=self.bp.subject, grade=self.bp.grade, topic="", question_type="short",
                                  difficulty="medium", count=1, language="en", user=self.hod)
        self.assertFalse(ask.call_args.kwargs.get("final", False))

    @override_settings(**GEMINI)
    def test_final_paper_is_refused_before_it_starts_on_gemini_without_paid_tier(self):
        with mock.patch.object(ai.threading, "Thread") as thread:
            with self.assertRaisesMessage(ai.GenerationError, "paid"):
                ai.start_final_paper(self.bp, self.controller)
        thread.assert_not_called()
        self.assertFalse(FinalPaper.objects.filter(generation_started_at__isnull=False).exists())

    @override_settings(**GEMINI)
    def test_question_regeneration_is_refused_on_gemini_without_paid_tier(self):
        paper = FinalPaper.objects.create(blueprint=self.bp)
        with mock.patch.object(ai.threading, "Thread") as thread:
            with self.assertRaisesMessage(ai.GenerationError, "paid"):
                ai.start_question_regeneration(paper, 1, self.controller)
        thread.assert_not_called()
        paper.refresh_from_db()
        self.assertFalse(paper.is_generating)

    @override_settings(**GEMINI)
    def test_bank_questions_still_work_on_gemini_free_tier(self):
        with mock.patch.object(ai, "_ask_model", return_value={"questions": []}) as ask:
            ai.generate_questions(subject=self.bp.subject, grade=self.bp.grade, topic="", question_type="short",
                                  difficulty="medium", count=1, language="en", user=self.hod)
        ask.assert_called_once()

    @override_settings(**GEMINI_PAID)
    def test_final_paper_starts_on_gemini_with_paid_tier(self):
        with mock.patch.object(ai.threading, "Thread") as thread:
            ai.start_final_paper(self.bp, self.controller)
        thread.return_value.start.assert_called_once()


@override_settings(**{**ANTHROPIC, "AI_MODEL": "claude-bank", "AI_FINAL_MODEL": "claude-final",
                       "ANTHROPIC_WORKSPACE_ID": ""})
class AnthropicAdapterTests(TestCase):
    """Today's Claude request, unchanged apart from the model coming from AI_MODEL / AI_FINAL_MODEL."""

    def ask(self, system="system", messages=(), **kwargs):
        response = SimpleNamespace(
            _request_id="req_1", model="claude-bank", stop_reason="end_turn",
            content=[SimpleNamespace(type="text", text='{"questions": []}')],
            usage=SimpleNamespace(input_tokens=10, output_tokens=5, cache_read_input_tokens=2,
                                  cache_creation_input_tokens=3))
        stream = mock.MagicMock()
        stream.__enter__.return_value.get_final_message.return_value = response
        with mock.patch.object(llm.anthropic, "Anthropic") as client, \
                mock.patch.dict("os.environ", {"ANTHROPIC_API_KEY": "test-key"}):
            client.return_value.beta.messages.stream.return_value = stream
            self.assertEqual(llm.ask(system, list(messages), SCHEMA, **kwargs), {"questions": []})
        self.client = client
        return client.return_value.beta.messages.stream.call_args.kwargs

    def test_system_and_messages_pass_through_unchanged(self):
        """The prompt-caching markers ai.py sets must reach Claude as they are."""
        system = [{"type": "text", "text": "You write questions.", "cache_control": {"type": "ephemeral"}}]
        messages = [{"role": "user", "content": [
            {"type": "document", "title": "Book", "cache_control": {"type": "ephemeral"},
             "source": {"type": "text", "media_type": "text/plain", "data": "Plants make food."}},
            {"type": "text", "text": "Write 3 questions."}]}]
        expected_system, expected_messages = json.loads(json.dumps(system)), json.loads(json.dumps(messages))
        sent = self.ask(system, messages, purpose="questions")
        self.assertEqual(sent["system"], expected_system)
        self.assertEqual(sent["messages"], expected_messages)
        self.client.assert_called_once_with()

    @override_settings(ANTHROPIC_WORKSPACE_ID="wrkspc_test")
    def test_workspace_header_is_sent(self):
        self.ask(purpose="questions")
        self.client.assert_called_once_with(default_headers={"anthropic-workspace-id": "wrkspc_test"})

    def not_found(self, **kwargs):
        error = llm.anthropic.NotFoundError(
            "model not found", body=None,
            response=httpx.Response(404, request=httpx.Request("POST", "https://api.anthropic.com/v1/messages")))
        with mock.patch.object(llm.anthropic, "Anthropic") as client, \
                mock.patch.dict("os.environ", {"ANTHROPIC_API_KEY": "test-key"}):
            client.return_value.beta.messages.stream.side_effect = error
            with self.assertRaises(llm.GenerationError) as caught:
                llm.ask("system", [], SCHEMA, **kwargs)
        return str(caught.exception)

    def test_model_not_found_names_the_setting_in_use(self):
        self.assertIn("AI_MODEL", self.not_found(purpose="questions"))
        self.assertIn("AI_FINAL_MODEL", self.not_found(purpose="final_paper", final=True))
        with self.settings(AI_MODEL="", AI_FINAL_MODEL=""):
            for kwargs in ({"purpose": "questions"}, {"purpose": "final_paper", "final": True}):
                message = self.not_found(**kwargs)
                self.assertIn("QUESTION_GENERATION_MODEL", message)
                self.assertNotIn("AI_MODEL", message)
                self.assertNotIn("AI_FINAL_MODEL", message)

    def test_request_shape(self):
        sent = self.ask(purpose="questions", effort="medium")
        self.assertEqual(sent["model"], "claude-bank")
        self.assertEqual((sent["max_tokens"], sent["fallbacks"], sent["betas"]),
                         (64000, "default", ["server-side-fallback-2026-07-01"]))
        self.assertEqual(sent["output_config"], {"effort": "medium",
                                                 "format": {"type": "json_schema", "schema": SCHEMA}})
        usage = AIUsage.objects.get()
        self.assertEqual((usage.input_tokens, usage.output_tokens, usage.cache_read_tokens,
                          usage.cache_write_tokens, usage.request_id), (10, 5, 2, 3, "req_1"))

    def test_final_uses_the_final_model(self):
        self.assertEqual(self.ask(purpose="final_paper", final=True)["model"], "claude-final")


# ---------- the Gemini adapter ----------

def gemini_chunks(text='{"questions": ["Q1"]}', *, finish="STOP", block_reason=None, prompt=1000, cached=400,
                  candidates=200, thoughts=50, model="gemini-3.8-flash-001", response_id="resp_1"):
    """What generate_content_stream yields: text spread over two chunks, usage on the last one."""
    if block_reason:
        return [genai_types.GenerateContentResponse(
            prompt_feedback=genai_types.GenerateContentResponsePromptFeedback(block_reason=block_reason),
            usage_metadata=genai_types.GenerateContentResponseUsageMetadata(prompt_token_count=prompt),
            model_version=model, response_id=response_id)]
    half = len(text) // 2

    def chunk(part, **extra):
        return genai_types.GenerateContentResponse(
            candidates=[genai_types.Candidate(content=genai_types.Content(role="model", parts=[
                genai_types.Part(text=part)]), **extra)],
            model_version=model, response_id=response_id)

    last = chunk(text[half:], finish_reason=finish)
    last.usage_metadata = genai_types.GenerateContentResponseUsageMetadata(
        prompt_token_count=prompt, cached_content_token_count=cached, candidates_token_count=candidates,
        thoughts_token_count=thoughts)
    return [chunk(text[:half]), last]


@override_settings(**GEMINI_PAID)
class GeminiAdapterTests(TestCase):
    def setUp(self):
        patcher = mock.patch.dict("os.environ", GEMINI_KEY)
        patcher.start()
        self.addCleanup(patcher.stop)

    def ask(self, chunks=None, *, error=None, messages=(), **kwargs):
        kwargs.setdefault("purpose", "questions")
        with mock.patch.object(llm.genai, "Client") as client:
            stream = client.return_value.models.generate_content_stream
            if error is not None:
                stream.side_effect = error
            else:
                stream.return_value = iter(chunks if chunks is not None else gemini_chunks())
            try:
                return llm.ask("You write questions.", list(messages), SCHEMA, **kwargs)
            finally:
                self.client, self.sent = client, stream.call_args.kwargs if stream.called else None

    def test_json_reply_is_parsed(self):
        self.assertEqual(self.ask(), {"questions": ["Q1"]})

    def test_request_config(self):
        self.ask(effort="high")
        self.assertEqual(self.client.call_args.kwargs["api_key"], "test-gemini-key")
        self.assertEqual(self.sent["model"], "gemini-3.8-flash")
        config = self.sent["config"]
        self.assertIs(config.store, False)
        self.assertEqual(config.response_mime_type, "application/json")
        self.assertEqual(config.response_json_schema, SCHEMA)
        self.assertEqual(config.system_instruction, "You write questions.")
        self.assertEqual(config.thinking_config.thinking_level, genai_types.ThinkingLevel.HIGH)
        self.assertEqual(config.max_output_tokens, 65536)

    def test_unknown_effort_becomes_high_thinking(self):
        self.ask(effort="max")
        self.assertEqual(self.sent["config"].thinking_config.thinking_level, genai_types.ThinkingLevel.HIGH)

    def test_thought_parts_are_not_part_of_the_answer(self):
        chunks = gemini_chunks()
        chunks[0].candidates[0].content.parts.insert(0, genai_types.Part(text="thinking...", thought=True))
        self.assertEqual(self.ask(chunks), {"questions": ["Q1"]})

    def test_api_key_is_passed_explicitly_so_google_api_key_cannot_win(self):
        with mock.patch.dict("os.environ", {"GOOGLE_API_KEY": "someone-elses-key"}):
            self.ask()
        self.assertEqual(self.client.call_args.kwargs["api_key"], "test-gemini-key")

    def test_final_request_uses_final_model(self):
        self.ask(purpose="final_paper", final=True)
        self.assertEqual(self.sent["model"], "gemini-4-argon")

    def test_documents_become_parts(self):
        pdf = b"%PDF-1.4 fake"
        messages = [{"role": "user", "content": [
            {"type": "document", "title": "Science book",
             "source": {"type": "base64", "media_type": "application/pdf",
                        "data": base64.standard_b64encode(pdf).decode("ascii")}},
            {"type": "document", "title": "Syllabus", "cache_control": {"type": "ephemeral"},
             "source": {"type": "text", "media_type": "text/plain", "data": "Plants make food."}},
            {"type": "text", "text": "Write 3 questions."},
        ]}]
        self.ask(messages=messages)
        (content,) = self.sent["contents"]
        self.assertEqual(content.role, "user")
        parts = content.parts
        self.assertIn("Science book", parts[0].text)
        self.assertEqual((parts[1].inline_data.data, parts[1].inline_data.mime_type), (pdf, "application/pdf"))
        self.assertIn("Syllabus", parts[2].text)
        self.assertIn("Plants make food.", parts[2].text)
        self.assertEqual(parts[3].text, "Write 3 questions.")
        self.assertEqual(len(parts), 4)
        self.assertNotIn("cache_control", json.dumps([p.model_dump(mode="json") for p in parts]))

    def test_usage_is_recorded(self):
        self.ask(user=None)
        usage = AIUsage.objects.get()
        self.assertEqual((usage.model, usage.purpose, usage.request_id), ("gemini-3.8-flash-001", "questions",
                                                                         "resp_1"))
        # input excludes the cached part (as Anthropic reports it); thinking is billed as output.
        self.assertEqual((usage.input_tokens, usage.cache_read_tokens, usage.output_tokens,
                          usage.cache_write_tokens), (600, 400, 250, 0))

    def test_usage_without_model_version_uses_requested_model(self):
        self.ask(gemini_chunks(model=None, response_id=None))
        usage = AIUsage.objects.get()
        self.assertEqual((usage.model, usage.request_id), ("gemini-3.8-flash", ""))

    def test_missing_key(self):
        with mock.patch.dict("os.environ", {"GEMINI_API_KEY": ""}):
            with self.assertRaisesMessage(llm.GenerationError,
                                          "The Gemini API key isn't set. Add GEMINI_API_KEY to the .env file "
                                          "and restart."):
                self.ask()
        self.client.assert_not_called()

    def test_api_errors_become_plain_messages(self):
        cases = [
            (genai_errors.ClientError, 400, "INVALID_ARGUMENT", "API key not valid. Please pass a valid API key.",
             "GEMINI_API_KEY"),
            (genai_errors.ClientError, 401, "UNAUTHENTICATED", "Request had invalid authentication credentials.",
             "GEMINI_API_KEY"),
            (genai_errors.ClientError, 403, "PERMISSION_DENIED", "Permission denied.", "access"),
            (genai_errors.ClientError, 402, "PAYMENT_REQUIRED", "Prepay credit exhausted.", "credit"),
            (genai_errors.ClientError, 400, "FAILED_PRECONDITION", "Billing is not enabled.", "billing"),
            (genai_errors.ClientError, 404, "NOT_FOUND", "models/gemini-x is not found.", "AI_MODEL"),
            (genai_errors.ClientError, 429, "RESOURCE_EXHAUSTED", "Quota exceeded.", "try again"),
            (genai_errors.ClientError, 400, "INVALID_ARGUMENT", "Request contains an invalid argument.",
             "Request contains an invalid argument."),
            (genai_errors.ServerError, 500, "INTERNAL", "Internal error.", "server error"),
            (genai_errors.ServerError, 503, "UNAVAILABLE", "The model is overloaded.", "server error"),
        ]
        for cls, code, status, message, expected in cases:
            error = cls(code, {"error": {"code": code, "status": status, "message": message}})
            with self.subTest(code=code, status=status):
                with self.assertRaisesMessage(llm.GenerationError, expected):
                    self.ask(error=error)

    def test_model_not_found_names_the_setting_in_use(self):
        error = genai_errors.ClientError(404, {"error": {"code": 404, "status": "NOT_FOUND", "message": "nope"}})
        with self.assertRaises(llm.GenerationError) as caught:
            self.ask(error=error, purpose="final_paper", final=True)
        self.assertIn("AI_FINAL_MODEL", str(caught.exception))
        with self.assertRaises(llm.GenerationError) as caught:
            self.ask(error=error)
        self.assertIn("AI_MODEL", str(caught.exception))
        self.assertNotIn("AI_FINAL_MODEL", str(caught.exception))

    def test_unexpected_errors_become_a_plain_message_without_details(self):
        for error in (RuntimeError("internal detail SECRET-EXAM"), KeyError("SECRET-EXAM"), TypeError("x")):
            with self.subTest(error=type(error)), self.assertLogs("questionbank.llm", "ERROR") as logs:
                with self.assertRaises(llm.GenerationError) as caught:
                    self.ask(error=error)
                self.assertEqual(str(caught.exception), "Gemini's reply couldn't be completed. Please try again.")
                self.assertIn("Gemini client error", "\n".join(logs.output))
                self.client.return_value.__exit__.assert_called_once()

    def test_unsupported_or_empty_content_is_a_plain_message(self):
        cases = [
            [{"role": "user", "content": [{"type": "image", "source": {"type": "base64", "data": "SECRET"}}]}],
            [{"role": "user", "content": [{"type": "document", "source": {"type": "url", "url": "https://x"}}]}],
            [{"role": "user", "content": []}],
            [{"role": "user", "content": ""}],
        ]
        for messages in cases:
            with self.subTest(messages=messages):
                with self.assertRaises(llm.GenerationError) as caught:
                    self.ask(messages=messages)
                self.assertIn("couldn't be sent to Gemini", str(caught.exception))
                self.assertNotIn("SECRET", str(caught.exception))
                self.client.return_value.models.generate_content_stream.assert_not_called()

    def test_client_is_closed_after_each_request(self):
        self.ask()
        self.client.return_value.__enter__.assert_called_once()
        self.client.return_value.__exit__.assert_called_once()
        with self.assertRaises(llm.GenerationError):
            self.ask(error=genai_errors.ServerError(500, {"error": {"code": 500, "status": "INTERNAL"}}))
        self.client.return_value.__exit__.assert_called_once()

    def test_vertex_ai_mode_is_off(self):
        self.ask()
        self.assertIs(self.client.call_args.kwargs["vertexai"], False)

    def test_vertex_ai_environment_variables_are_ignored(self):
        """GOOGLE_GENAI_USE_VERTEXAI / _ENTERPRISE would switch the SDK to Google Cloud's endpoint and auth."""
        with mock.patch.dict("os.environ", {"GOOGLE_GENAI_USE_VERTEXAI": "true", "GOOGLE_GENAI_USE_ENTERPRISE": "true",
                                            "GOOGLE_CLOUD_PROJECT": "someone-elses-project"}):
            with llm.gemini_client() as client:
                self.assertIs(client._api_client.vertexai, False)

    def test_connection_errors(self):
        for error in (httpx.ConnectError("down"), httpx2.ConnectError("down"), httpx.ReadTimeout("slow")):
            with self.subTest(error=type(error)):
                with self.assertRaisesMessage(llm.GenerationError, "Couldn't reach the Gemini API"):
                    self.ask(error=error)

    def test_stops_become_plain_messages(self):
        cases = [
            ("MAX_TOKENS", "The answer was cut off. Try asking for fewer questions at a time."),
            ("SAFETY", "Gemini declined"),
            ("PROHIBITED_CONTENT", "Gemini declined"),
            ("BLOCKLIST", "Gemini declined"),
            ("SPII", "Gemini declined"),
            ("RECITATION", "quoted the book too closely"),
            ("OTHER", "Gemini stopped early"),
        ]
        for finish, expected in cases:
            with self.subTest(finish=finish):
                with self.assertRaisesMessage(llm.GenerationError, expected):
                    self.ask(gemini_chunks(finish=finish))
        # The tokens were still used, so every attempt is recorded.
        self.assertEqual(AIUsage.objects.count(), len(cases))

    def test_blocked_prompt(self):
        with self.assertRaisesMessage(llm.GenerationError, "Gemini declined"):
            self.ask(gemini_chunks(block_reason="SAFETY"))
        self.assertEqual(AIUsage.objects.get().input_tokens, 1000)

    def test_unreadable_reply(self):
        with self.assertRaisesMessage(llm.GenerationError, "couldn't be read"):
            self.ask(gemini_chunks(text='{"questions": [unfinished'))

    def test_real_request_body(self):
        """The SDK's own request, sent to a fake HTTP transport: store is off, the schema is sent, and the PDF goes
        inline (never through the Files API)."""
        from google import genai as real_genai
        seen = []

        def handler(request):
            seen.append(request)
            body = {"candidates": [{"content": {"role": "model", "parts": [{"text": '{"questions": []}'}]},
                                    "finishReason": "STOP"}],
                    "usageMetadata": {"promptTokenCount": 12, "candidatesTokenCount": 3},
                    "modelVersion": "gemini-3.8-flash", "responseId": "resp_wire"}
            return httpx.Response(200, content=f"data: {json.dumps(body)}\r\n\r\n".encode(),
                                  headers={"content-type": "text/event-stream"})

        real_client = real_genai.Client

        def fake_client(*, http_options, **kwargs):
            http_options = http_options.model_copy(update={
                "httpx_client": httpx.Client(transport=httpx.MockTransport(handler)), "retry_options": None})
            return real_client(http_options=http_options, **kwargs)

        messages = [{"role": "user", "content": [
            {"type": "document", "title": "Book", "cache_control": {"type": "ephemeral"},
             "source": {"type": "base64", "media_type": "application/pdf",
                        "data": base64.standard_b64encode(b"%PDF").decode("ascii")}},
            {"type": "text", "text": "Go."}]}]
        with mock.patch.object(llm.genai, "Client", side_effect=fake_client):
            self.assertEqual(llm.ask("sys", messages, SCHEMA, purpose="questions"), {"questions": []})
        (request,) = seen
        self.assertIn("/models/gemini-3.8-flash:streamGenerateContent", str(request.url))
        self.assertNotIn("/files", str(request.url))
        self.assertEqual(request.headers["x-goog-api-key"], "test-gemini-key")
        body = json.loads(request.content)
        self.assertIs(body["store"], False)
        self.assertEqual(body["generationConfig"]["responseJsonSchema"], SCHEMA)
        self.assertEqual(body["generationConfig"]["responseMimeType"], "application/json")
        self.assertEqual(body["contents"][0]["parts"][1]["inlineData"]["mime_type"], "application/pdf")
        self.assertNotIn("cache_control", request.content.decode())
        self.assertEqual(AIUsage.objects.get().request_id, "resp_wire")


# ---------- check_ai ----------

class CheckAICommandTests(SimpleTestCase):
    def run_command(self, name="check_ai", **ping):
        out = StringIO()
        with mock.patch.object(llm, "ping", **ping) as fake:
            call_command(name, stdout=out)
        return out.getvalue(), fake

    @override_settings(**{**ANTHROPIC, "AI_FINAL_MODEL": "claude-final"})
    def test_checks_bank_and_final_models(self):
        output, ping = self.run_command(side_effect=[reply("connected", model="claude-test-5"),
                                                     reply("connected", model="claude-final")])
        self.assertEqual([c.kwargs["final"] for c in ping.call_args_list], [False, True])
        self.assertIn("Connected. claude-test-5 replied", output)
        self.assertIn("Connected. claude-final replied", output)

    @override_settings(**ANTHROPIC)
    def test_one_check_when_both_models_are_the_same(self):
        _, ping = self.run_command(return_value=reply("connected"))
        ping.assert_called_once_with(final=False)

    @override_settings(**GEMINI)
    def test_gemini_without_paid_tier_checks_bank_model_only(self):
        output, ping = self.run_command(return_value=reply("connected", model="gemini-3.8-flash"))
        ping.assert_called_once_with(final=False)
        self.assertIn("GEMINI_PAID_TIER", output)

    @override_settings(**ANTHROPIC)
    def test_errors_become_command_errors(self):
        with self.assertRaisesMessage(CommandError, "isn't set"):
            self.run_command(side_effect=llm.GenerationError("The Claude API key isn't set."))

    @override_settings(**ANTHROPIC)
    def test_stopped_reply_becomes_a_command_error(self):
        with self.assertRaisesMessage(CommandError, "cut off"):
            self.run_command(return_value=reply("", error="The answer was cut off."))

    @override_settings(**ANTHROPIC)
    def test_check_claude_is_the_same_command(self):
        output, ping = self.run_command("check_claude", return_value=reply("connected"))
        ping.assert_called_once_with(final=False)
        self.assertIn("Connected.", output)


@override_settings(**GEMINI_PAID)
class PingTests(TestCase):
    """The tiny request check_ai sends: plain text, store off, nothing recorded as question generation."""

    def test_gemini_ping(self):
        with mock.patch.object(llm.genai, "Client") as client, mock.patch.dict("os.environ", GEMINI_KEY):
            client.return_value.models.generate_content_stream.return_value = iter(
                gemini_chunks(text="connected", model="gemini-4-argon"))
            result = llm.ping(final=True)
        sent = client.return_value.models.generate_content_stream.call_args.kwargs
        self.assertEqual((sent["model"], sent["config"].store), ("gemini-4-argon", False))
        self.assertIsNone(sent["config"].response_json_schema)
        self.assertEqual((result.text, result.model), ("connected", "gemini-4-argon"))
        self.assertFalse(AIUsage.objects.exists())

    @override_settings(**ANTHROPIC)
    def test_anthropic_ping(self):
        response = SimpleNamespace(_request_id="req_9", model="claude-test-5", stop_reason="end_turn",
                                   content=[SimpleNamespace(type="text", text="connected")],
                                   usage=SimpleNamespace(input_tokens=5, output_tokens=1, cache_read_input_tokens=0,
                                                         cache_creation_input_tokens=0))
        with mock.patch.object(llm.anthropic, "Anthropic") as client, \
                mock.patch.dict("os.environ", {"ANTHROPIC_API_KEY": "test-key"}):
            client.return_value.messages.create.return_value = response
            result = llm.ping()
        sent = client.return_value.messages.create.call_args.kwargs
        self.assertEqual((sent["model"], sent["max_tokens"]), ("claude-test-5", 256))
        self.assertEqual((result.text, result.request_id), ("connected", "req_9"))


# ---------- QA edge cases ----------

BANGLA = "সালোকসংশ্লেষণ কী? উদ্ভিদ কীভাবে খাদ্য তৈরি করে?"


class PaidTierValueTests(SimpleTestCase):
    """GEMINI_PAID_TIER is on only for true/1 (any case, spaces trimmed); anything else, 'yes' included, is off."""

    load = SettingsFileTests.load

    def test_paid_tier_values(self):
        for value, expected in (("True", True), ("1", True), (" TRUE ", True), ("true", True), ("yes", False),
                                ("false", False), ("0", False), ("", False)):
            with self.subTest(value=value):
                self.assertIs(self.load(GEMINI_PAID_TIER=value)["GEMINI_PAID_TIER"], expected)


@override_settings(**GEMINI_PAID)
class GeminiEdgeCaseTests(TestCase):
    def setUp(self):
        patcher = mock.patch.dict("os.environ", GEMINI_KEY)
        patcher.start()
        self.addCleanup(patcher.stop)

    def ask(self, chunks, messages=(), **kwargs):
        kwargs.setdefault("purpose", "questions")
        with mock.patch.object(llm.genai, "Client") as client:
            stream = client.return_value.models.generate_content_stream
            stream.return_value = iter(chunks)
            try:
                return llm.ask("You write questions.", list(messages), SCHEMA, **kwargs)
            finally:
                self.sent = stream.call_args.kwargs if stream.called else None

    def test_bangla_round_trips(self):
        text = json.dumps({"questions": [BANGLA]}, ensure_ascii=False)
        messages = [{"role": "user", "content": [
            {"type": "document", "title": "বিজ্ঞান বই",
             "source": {"type": "text", "media_type": "text/plain", "data": "উদ্ভিদ খাদ্য তৈরি করে।"}},
            {"type": "text", "text": BANGLA}]}]
        self.assertEqual(self.ask(gemini_chunks(text=text), messages), {"questions": [BANGLA]})
        parts = self.sent["contents"][0].parts
        self.assertIn("বিজ্ঞান বই", parts[0].text)
        self.assertIn("উদ্ভিদ খাদ্য তৈরি করে।", parts[0].text)
        self.assertEqual(parts[1].text, BANGLA)

    def test_bangla_on_the_wire(self):
        """The real SDK request body carries Bangla intact (UTF-8), and a Bangla reply is parsed."""
        from google import genai as real_genai
        seen = []

        def handler(request):
            seen.append(request)
            body = {"candidates": [{"content": {"role": "model", "parts": [
                {"text": json.dumps({"questions": [BANGLA]}, ensure_ascii=False)}]}, "finishReason": "STOP"}],
                "usageMetadata": {"promptTokenCount": 5, "candidatesTokenCount": 3}}
            return httpx.Response(200, content=f"data: {json.dumps(body, ensure_ascii=False)}\r\n\r\n".encode(),
                                  headers={"content-type": "text/event-stream; charset=utf-8"})

        real_client = real_genai.Client

        def fake_client(*, http_options, **kwargs):
            http_options = http_options.model_copy(update={
                "httpx_client": httpx.Client(transport=httpx.MockTransport(handler)), "retry_options": None})
            return real_client(http_options=http_options, **kwargs)

        messages = [{"role": "user", "content": [{"type": "text", "text": BANGLA}]}]
        with mock.patch.object(llm.genai, "Client", side_effect=fake_client):
            self.assertEqual(llm.ask(BANGLA, messages, SCHEMA, purpose="questions"), {"questions": [BANGLA]})
        body = json.loads(seen[0].content.decode("utf-8"))
        self.assertEqual(body["contents"][0]["parts"][0]["text"], BANGLA)
        self.assertEqual(body["systemInstruction"]["parts"][0]["text"], BANGLA)

    def test_missing_usage_metadata_records_zeros(self):
        chunks = gemini_chunks()
        for chunk in chunks:
            chunk.usage_metadata = None
        self.assertEqual(self.ask(chunks), {"questions": ["Q1"]})
        usage = AIUsage.objects.get()
        self.assertEqual((usage.input_tokens, usage.output_tokens, usage.cache_read_tokens), (0, 0, 0))

    def test_usage_metadata_with_none_counts(self):
        chunks = gemini_chunks()
        chunks[-1].usage_metadata = genai_types.GenerateContentResponseUsageMetadata()
        self.assertEqual(self.ask(chunks), {"questions": ["Q1"]})
        self.assertEqual(AIUsage.objects.get().input_tokens, 0)

    def test_no_candidates_is_a_plain_error(self):
        chunks = [genai_types.GenerateContentResponse(response_id="r0")]
        with self.assertRaisesMessage(llm.GenerationError, "Gemini's reply couldn't be read. Please try again."):
            self.ask(chunks)
        self.assertEqual(AIUsage.objects.get().input_tokens, 0)

    def test_empty_stream_is_a_plain_error(self):
        with self.assertRaisesMessage(llm.GenerationError, "couldn't be read"):
            self.ask([])

    def test_candidate_without_content_or_text(self):
        for candidate in (genai_types.Candidate(finish_reason="STOP"),
                          genai_types.Candidate(content=genai_types.Content(role="model", parts=None),
                                                finish_reason="STOP"),
                          genai_types.Candidate(content=genai_types.Content(role="model", parts=[
                              genai_types.Part(text="")]), finish_reason="STOP")):
            with self.subTest(candidate=candidate):
                with self.assertRaisesMessage(llm.GenerationError, "couldn't be read"):
                    self.ask([genai_types.GenerateContentResponse(candidates=[candidate])])

    def test_fenced_or_trailing_json_is_refused_plainly(self):
        """The shared parser is strict json.loads: structured output (response_json_schema) shouldn't produce
        fences, and if it ever does the user gets the plain 'try again' message, never a crash."""
        for text in ('```json\n{"questions": ["Q1"]}\n```', '{"questions": ["Q1"]}\nHope this helps!',
                     'Here you go: {"questions": ["Q1"]}'):
            with self.subTest(text=text):
                with self.assertRaisesMessage(llm.GenerationError, "couldn't be read"):
                    self.ask(gemini_chunks(text=text))

    def test_surrounding_whitespace_is_fine(self):
        self.assertEqual(self.ask(gemini_chunks(text='\n  {"questions": ["Q1"]}  \n')), {"questions": ["Q1"]})

    @override_settings(AI_FINAL_MODEL="claude-opus-5-5")
    def test_a_claude_final_model_is_refused_under_gemini(self):
        with mock.patch.object(llm.genai, "Client") as client:
            with self.assertRaisesMessage(llm.GenerationError, "Change AI_FINAL_MODEL (or AI_PROVIDER)"):
                llm.ask("s", [], SCHEMA, purpose="final_paper", final=True)
        client.assert_not_called()
        self.assertFalse(AIUsage.objects.exists())

    @override_settings(AI_MODEL="claude-opus-5-5")
    def test_a_claude_model_message_is_plain(self):
        with mock.patch.object(llm.genai, "Client") as client:
            with self.assertRaisesMessage(llm.GenerationError,
                                          "The AI model is set to claude-opus-5-5, which is a Claude model, but the "
                                          "server uses Gemini. Change AI_MODEL (or AI_PROVIDER)"):
                llm.ask("s", [], SCHEMA, purpose="questions")
        client.assert_not_called()


class ProviderSwitchTests(TestCase):
    """Switching AI_PROVIDER (as override_settings does mid-suite) leaks nothing: a fresh client is built per
    request, from the provider active at that moment, with that provider's default model."""

    def test_switching_back_and_forth(self):
        anthropic_response = SimpleNamespace(
            _request_id="req", model="claude-test-5", stop_reason="end_turn",
            content=[SimpleNamespace(type="text", text='{"questions": ["A"]}')],
            usage=SimpleNamespace(input_tokens=1, output_tokens=1, cache_read_input_tokens=0,
                                  cache_creation_input_tokens=0))
        with mock.patch.object(llm.genai, "Client") as gclient, \
                mock.patch.object(llm.anthropic, "Anthropic") as aclient, \
                mock.patch.dict("os.environ", {**GEMINI_KEY, "ANTHROPIC_API_KEY": "k"}):
            gclient.return_value.models.generate_content_stream.side_effect = lambda **kw: iter(gemini_chunks())
            stream = mock.MagicMock()
            stream.__enter__.return_value.get_final_message.return_value = anthropic_response
            aclient.return_value.beta.messages.stream.return_value = stream
            for settings_, expected in ((GEMINI, ["Q1"]), (ANTHROPIC, ["A"]), (GEMINI, ["Q1"]), (ANTHROPIC, ["A"])):
                with self.settings(**settings_):
                    self.assertEqual(llm.ask("s", [{"role": "user", "content": "go"}], SCHEMA,
                                             purpose="questions")["questions"], expected)
        self.assertEqual((gclient.call_count, aclient.call_count), (2, 2))
        self.assertEqual([c.kwargs["model"] for c in gclient.return_value.models.generate_content_stream
                          .call_args_list], ["gemini-3.8-flash", "gemini-3.8-flash"])
        self.assertEqual([c.kwargs["model"] for c in aclient.return_value.beta.messages.stream.call_args_list],
                         ["claude-test-5", "claude-test-5"])
        self.assertEqual(list(AIUsage.objects.values_list("model", flat=True).order_by("pk")),
                         ["gemini-3.8-flash-001", "claude-test-5"] * 2)

    def test_status_follows_the_switch(self):
        with mock.patch.dict("os.environ", {**GEMINI_KEY, "ANTHROPIC_API_KEY": ""}):
            with self.settings(**GEMINI):
                self.assertEqual((ai.ai_status()["label"], ai.ai_status()["configured"]), ("Gemini", True))
            with self.settings(**ANTHROPIC):
                self.assertEqual((ai.ai_status()["label"], ai.ai_status()["configured"]), ("Claude", False))


@override_settings(**GEMINI_PAID)
class SizeCapUnderGeminiTests(BlueprintTestCase):
    """The 22 MB guard sits in ai.py before any provider is called, so it holds for Gemini too."""

    def sized(self, size):
        from django.db.models.fields.files import FieldFile
        return mock.patch.object(FieldFile, "size", new_callable=mock.PropertyMock, return_value=size)

    def generate(self):
        return ai.generate_questions(subject=self.bp.subject, grade=self.bp.grade, topic="", question_type="short",
                                     difficulty="medium", count=1, language="bn", user=self.hod)

    def test_over_the_cap_is_refused_before_gemini(self):
        with self.sized(ai.MAX_TOTAL_SOURCE_BYTES + 1), mock.patch.object(llm.genai, "Client") as client, \
                mock.patch.dict("os.environ", GEMINI_KEY):
            with self.assertRaisesMessage(ai.GenerationError, "too large"):
                self.generate()
            with self.assertRaisesMessage(ai.GenerationError, "too large"):
                ai.generate_final_paper(self.bp, self.controller)
        client.assert_not_called()
        self.assertFalse(AIUsage.objects.exists())

    def test_at_the_cap_is_sent(self):
        with self.sized(ai.MAX_TOTAL_SOURCE_BYTES), mock.patch.object(llm.genai, "Client") as client, \
                mock.patch.dict("os.environ", GEMINI_KEY):
            client.return_value.models.generate_content_stream.return_value = iter(
                gemini_chunks(text='{"questions": []}'))
            self.assertEqual(self.generate(), [])
        client.return_value.models.generate_content_stream.assert_called_once()


@override_settings(**{**GEMINI, "AI_MODEL": "gemini-bank-test", "AI_FINAL_MODEL": "gemini-final-test",
                      "MAKE_WEBHOOK_URL": "", "MAKE_MAILHOOK_ADDRESS": ""})
class GeminiFreeTierServerSideTests(BlueprintTestCase):
    """Crafted POSTs that skip the disabled buttons are still refused on Gemini's free tier: a plain message,
    nothing started, and no paper left marked as being written."""

    def setUp(self):
        super().setUp()
        call_command("setup_academics", stdout=StringIO())
        patcher = mock.patch.dict("os.environ", {**GEMINI_KEY, "ANTHROPIC_API_KEY": ""})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.bp_url = self.reverse("admin:questionbank_questionblueprint_change", self.bp.pk)

    @staticmethod
    def reverse(name, pk):
        from django.urls import reverse
        return reverse(name, args=[pk])

    def make_paper(self):
        with self.settings(AI_PROVIDER="anthropic", AI_MODEL="", AI_FINAL_MODEL=""), \
                mock.patch.object(ai, "_ask_claude", return_value=claude_reply(3)):
            return ai.generate_final_paper(self.bp, self.controller)

    def exam_controller(self):
        from django.contrib.auth import get_user_model
        from django.contrib.auth.models import Group
        user = get_user_model().objects.create_user("examctl", password="Pw-12345!", is_staff=True)
        user.groups.add(Group.objects.get(name="Exam Controller"))
        return user

    def assert_refused(self, response):
        from django.utils.html import escape
        self.assertContains(response, escape(llm.FINAL_NOT_ALLOWED))
        self.assertNotContains(response, "The AI is writing")

    def test_crafted_generate_final_post_is_refused(self):
        for user in (self.controller, self.exam_controller()):
            self.client.force_login(user)
            with self.subTest(user=user.username), mock.patch.object(ai.threading, "Thread") as thread, \
                    mock.patch.object(llm.genai, "Client") as client:
                self.assert_refused(self.client.post(self.bp_url, {"_generate_final": "1"}, follow=True))
            thread.assert_not_called()
            client.assert_not_called()
        self.assertFalse(FinalPaper.objects.exists())  # the get_or_create was rolled back with the refusal

    def test_crafted_regenerate_posts_are_refused(self):
        paper = self.make_paper()
        before = list(paper.questions.values_list("text", flat=True))
        url = self.reverse("admin:questionbank_finalpaper_change", paper.pk)
        self.client.force_login(self.controller)
        for data in ({"_regenerate": "1"}, {"_regenerate_one": "1", "question_order": "2"}):
            with self.subTest(data=data), mock.patch.object(ai.threading, "Thread") as thread, \
                    mock.patch.object(llm.genai, "Client") as client:
                self.assert_refused(self.client.post(url, data, follow=True))
            thread.assert_not_called()
            client.assert_not_called()
            paper.refresh_from_db()
            self.assertFalse(paper.is_generating)
            self.assertIsNone(paper.generation_started_at)
        self.assertEqual(list(paper.questions.values_list("text", flat=True)), before)

    def test_locking_still_works_on_the_free_tier(self):
        paper = self.make_paper()
        self.client.force_login(self.controller)
        self.client.post(self.reverse("admin:questionbank_finalpaper_change", paper.pk), {"_lock": "1"})
        paper.refresh_from_db()
        self.assertEqual(paper.status, FinalPaper.Status.LOCKED)

    def test_exam_controller_sees_provider_status(self):
        self.client.force_login(self.exam_controller())
        response = self.client.get(self.bp_url)
        self.assertContains(response, "AI connected: Gemini · model <code>gemini-bank-test</code>")
        self.assertContains(response, "final papers <code>gemini-final-test</code>")
        self.assertContains(response, 'id="ai-final-blocked"')
        self.assertNotContains(response, 'name="_generate_final"')
        with self.settings(GEMINI_PAID_TIER=True):
            response = self.client.get(self.bp_url)
        self.assertContains(response, 'name="_generate_final"')
        self.assertContains(response, "Final papers are processed by Google (Gemini).")

    def test_teacher_never_sees_final_paper_notes(self):
        from django.contrib.auth import get_user_model
        from django.contrib.auth.models import Group

        from .models import SubjectAssignment
        teacher = get_user_model().objects.create_user("teacher", password="Pw-12345!", is_staff=True)
        teacher.groups.add(Group.objects.get(name="Teacher / Subject Expert"))
        SubjectAssignment.objects.create(user=teacher, subject=self.bp.subject)
        self.bp.assigned_to = teacher  # teachers see the blueprints they write
        self.bp.save(update_fields=["assigned_to"])
        self.client.force_login(teacher)
        for paid in (False, True):
            with self.subTest(paid=paid), self.settings(GEMINI_PAID_TIER=paid):
                response = self.client.get(self.bp_url)
                self.assertEqual(response.status_code, 200, response.get("Location"))
                for text in ("ai-final-blocked", "processed by Google", "final papers <code>", "GEMINI_PAID_TIER",
                             "_generate_final", "gemini-final-test", "Final papers are switched off"):
                    self.assertNotContains(response, text)
                self.assertContains(response, "The exam controller will prepare the final paper")


class CheckAIEdgeTests(SimpleTestCase):
    """check_ai / check_claude with ping mocked, or with the real ping stopped before any client is built."""

    def run_command(self, name="check_ai"):
        out = StringIO()
        call_command(name, stdout=out)
        return out.getvalue()

    @override_settings(**GEMINI_PAID)
    def test_gemini_paid_checks_both_models(self):
        with mock.patch.object(llm, "ping", side_effect=[reply("connected", model="gemini-3.8-flash"),
                                                         reply("connected", model="gemini-4-argon")]) as ping:
            output = self.run_command("check_claude")
        self.assertEqual([c.kwargs["final"] for c in ping.call_args_list], [False, True])
        self.assertIn("Testing Gemini model gemini-4-argon (final papers)", output)
        self.assertNotIn("GEMINI_PAID_TIER", output)

    def test_missing_key_for_each_provider_sends_nothing(self):
        for settings_, env, expected in ((ANTHROPIC, {"ANTHROPIC_API_KEY": ""}, "ANTHROPIC_API_KEY"),
                                         (GEMINI, {"GEMINI_API_KEY": "", "GOOGLE_API_KEY": "x"}, "GEMINI_API_KEY")):
            for name in ("check_ai", "check_claude"):
                with self.subTest(provider=settings_["AI_PROVIDER"], command=name), self.settings(**settings_), \
                        mock.patch.dict("os.environ", env), \
                        mock.patch.object(llm.genai, "Client") as gclient, \
                        mock.patch.object(llm.anthropic, "Anthropic") as aclient:
                    with self.assertRaisesMessage(CommandError, expected):
                        self.run_command(name)
                    gclient.assert_not_called()
                    aclient.assert_not_called()

    @override_settings(**{**GEMINI, "AI_MODEL": "claude-opus-5-5"})
    def test_wrong_family_model_is_reported_without_a_request(self):
        with mock.patch.object(llm.genai, "Client") as client, mock.patch.dict("os.environ", GEMINI_KEY):
            with self.assertRaisesMessage(CommandError, "Claude model"):
                self.run_command()
        client.assert_not_called()

    @override_settings(**GEMINI)
    def test_gemini_api_error_becomes_command_error(self):
        error = genai_errors.ClientError(400, {"error": {"code": 400, "status": "INVALID_ARGUMENT",
                                                         "message": "API key not valid."}})
        with mock.patch.object(llm.genai, "Client") as client, mock.patch.dict("os.environ", GEMINI_KEY):
            client.return_value.models.generate_content_stream.side_effect = error
            with self.assertRaisesMessage(CommandError, "GEMINI_API_KEY"):
                self.run_command()
