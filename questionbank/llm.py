"""Talking to the AI provider. Everything else in the app is provider-neutral and goes through ask().

Two providers sit behind settings.AI_PROVIDER: "anthropic" (Claude, the default) and "gemini". Requests are built
once, in the Anthropic message format (document / text content blocks), by questionbank.ai; the Anthropic adapter
sends them as they are and the Gemini adapter translates them into google-genai parts. Both adapters return a Reply
with plain token counts, so usage recording, error handling and JSON parsing are shared.

Confidential final papers only go to Gemini once the owner confirms the paid tier (GEMINI_PAID_TIER=true): on the
free tier Google may use prompts for training and human review. Gemini requests always send store=False and send
files inline, never through the Files API, so no copy of a book or paper is kept in the Google project.
"""

import base64
import json
import logging
import os
from dataclasses import dataclass

import anthropic
import httpx
import httpx2
from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from google import genai
from google.genai import errors as genai_errors
from google.genai import types as genai_types

logger = logging.getLogger(__name__)

PROVIDERS = ("anthropic", "gemini")
LABELS = {"anthropic": "Claude", "gemini": "Gemini"}
KEY_NAMES = {"anthropic": "ANTHROPIC_API_KEY", "gemini": "GEMINI_API_KEY"}
GEMINI_DEFAULT_MODELS = {"bank": "gemini-3.8-flash", "final": "gemini-4-argon"}
# The model families, to catch a model id left over from the other provider (e.g. AI_MODEL=claude-... on Gemini).
MODEL_PREFIXES = {"anthropic": "claude-", "gemini": "gemini-"}
# Requests for confidential final papers. They use AI_FINAL_MODEL and are blocked on Gemini's free tier.
FINAL_PURPOSES = {"final_paper", "final_review", "final_question"}

ANTHROPIC_MAX_TOKENS = 64000
# Gemini 4 Argon and 3.8 Flash allow 65,536 output tokens; thinking shares this budget.
GEMINI_MAX_OUTPUT_TOKENS = 65536
# Final papers can take several minutes (FinalPaper.GENERATION_TIMEOUT is 15 minutes). The SDK takes milliseconds.
GEMINI_TIMEOUT_MS = 15 * 60 * 1000

FINAL_NOT_ALLOWED = (
    "Final papers can't be written with Gemini yet. They need Google's paid Gemini plan, which isn't turned on for "
    "this server. Practice questions still work. Ask the system administrator."
)


class GenerationError(Exception):
    """A problem the user can act on; the message is shown in the admin."""


@dataclass
class Reply:
    """One provider answer, reduced to what the app uses. error is a plain message when the provider stopped
    without a usable answer (refusal, cut off, blocked); the tokens were still billed, so usage is recorded first."""

    text: str
    model: str
    request_id: str
    input_tokens: int  # billed input that wasn't read from the cache
    output_tokens: int  # visible output plus thinking (both are billed as output)
    cache_read_tokens: int
    cache_write_tokens: int
    error: str = ""


# ---------- settings ----------

def provider():
    """The active provider. settings.py already rejects unknown values; this also covers override_settings."""
    name = (settings.AI_PROVIDER or "anthropic").strip().lower()
    if name not in PROVIDERS:
        raise ImproperlyConfigured(f"AI_PROVIDER must be one of {', '.join(PROVIDERS)}, not {name!r}.")
    return name


def model_for(final=False):
    """AI_FINAL_MODEL for final papers (and their review and single-question rewrites), AI_MODEL otherwise. Left
    empty, each falls back to the provider's default: QUESTION_GENERATION_MODEL on Anthropic, so existing setups
    keep their model. Resolved per request so a changed provider never inherits the other one's defaults."""
    explicit = settings.AI_FINAL_MODEL if final else settings.AI_MODEL
    if explicit:
        return explicit
    if provider() == "gemini":
        return GEMINI_DEFAULT_MODELS["final" if final else "bank"]
    return settings.QUESTION_GENERATION_MODEL


def final_papers_allowed():
    """Whether confidential final papers may be sent to the active provider. The API can't report Google's tier,
    so GEMINI_PAID_TIER is the owner's statement that billing is linked (and the side-by-side trial passed)."""
    return provider() != "gemini" or bool(settings.GEMINI_PAID_TIER)


def status():
    """For display: which provider and models are used, whether its key is set, and whether final papers may be
    written. Makes no API call."""
    name = provider()
    return {
        "provider": name,
        "label": LABELS[name],
        "configured": bool(os.environ.get(KEY_NAMES[name], "").strip()),
        "model": model_for(),
        "final_model": model_for(final=True),
        "final_allowed": final_papers_allowed(),
    }


def model_setting(final=False):
    """The .env setting the model for this request comes from, for error messages: AI_FINAL_MODEL / AI_MODEL, or
    QUESTION_GENERATION_MODEL when that is left empty on Anthropic (see model_for)."""
    if not (settings.AI_FINAL_MODEL if final else settings.AI_MODEL) and provider() == "anthropic":
        return "QUESTION_GENERATION_MODEL"
    return "AI_FINAL_MODEL" if final else "AI_MODEL"


def _check_model(name, model, final):
    other = next(p for p in PROVIDERS if p != name)
    if model.startswith(MODEL_PREFIXES[other]):
        setting = model_setting(final)
        raise GenerationError(
            f"The AI model is set to {model}, which is a {LABELS[other]} model, but the server uses {LABELS[name]}. "
            f"Change {setting} (or AI_PROVIDER) in the .env file and restart."
        )


# ---------- the one entry point ----------

def ask(system, messages, schema, *, effort="medium", purpose, user=None, final=False):
    """Send one structured-output request to the active provider and return the parsed JSON object.

    messages use the Anthropic content-block format (see questionbank.ai). purpose (an AIUsage.Purpose value)
    and user label the token counts recorded for cost tracking. final picks AI_FINAL_MODEL; final-paper purposes
    count as final even without it, so the paid-tier guard can't be skipped by a caller forgetting the flag.
    """
    final = final or purpose in FINAL_PURPOSES
    name = provider()
    if final and not final_papers_allowed():
        raise GenerationError(FINAL_NOT_ALLOWED)
    model = model_for(final)
    _check_model(name, model, final)

    adapter = _ask_gemini if name == "gemini" else _ask_anthropic
    reply = adapter(system, messages, schema, effort=effort, model=model, final=final)
    logger.info("AI request %s (%s): %s in / %s out tokens, %s cached", reply.request_id, reply.model,
                reply.input_tokens, reply.output_tokens, reply.cache_read_tokens)
    record_usage(reply, purpose, user)
    if reply.error:
        raise GenerationError(reply.error)
    return parse_json(reply, schema, LABELS[name], purpose=purpose)


def parse_json(reply, schema, label="The AI", purpose=""):
    """The reply as a dict with the schema's first required key, or a plain error. Shared by both providers, so
    a reply is accepted or rejected the same way whichever wrote it. The log names the request but never quotes
    the reply: for final papers it is confidential paper content (SR-03)."""
    try:
        data = json.loads(reply.text)
        data[schema["required"][0]]
    except (ValueError, KeyError, TypeError, IndexError):
        logger.warning("Unparseable AI output for %s (request %s): %s characters", purpose or "request",
                       reply.request_id, len(reply.text or ""))
        raise GenerationError(f"{label}'s reply couldn't be read. Please try again.")
    return data


def record_usage(reply, purpose, user):
    """Save the request's token counts (never content). Never raises: measuring must not break generation."""
    from .models import AIUsage

    try:
        AIUsage.objects.create(
            user=user if getattr(user, "pk", None) else None, purpose=purpose, model=(reply.model or "")[:50],
            input_tokens=reply.input_tokens or 0, output_tokens=reply.output_tokens or 0,
            cache_read_tokens=reply.cache_read_tokens or 0, cache_write_tokens=reply.cache_write_tokens or 0,
            request_id=(reply.request_id or "")[:100],
        )
    except Exception:
        logger.exception("Couldn't record AI usage")


def ping(final=False):
    """One tiny plain-text request to the active provider (for check_ai). Costs a fraction of a cent; not recorded
    in AIUsage, which tracks question generation."""
    name = provider()
    model = model_for(final)
    _check_model(name, model, final)
    prompt = "Reply with just the word: connected"
    if name == "gemini":
        config = genai_types.GenerateContentConfig(
            max_output_tokens=2048, store=False,
            thinking_config=genai_types.ThinkingConfig(thinking_level=genai_types.ThinkingLevel.LOW))
        return _gemini_stream(model, [genai_types.Content(role="user", parts=[genai_types.Part(text=prompt)])], config,
                              final=final)
    with _anthropic_errors(final):
        response = claude_client().messages.create(
            model=model, max_tokens=256, output_config={"effort": "low"},
            messages=[{"role": "user", "content": prompt}],
        )
    return _anthropic_reply(response, model)


# ---------- Anthropic ----------

def claude_client():
    """The Anthropic client. Keys that aren't scoped to a workspace must name one on every request, so
    ANTHROPIC_WORKSPACE_ID (if set) is sent as the anthropic-workspace-id header."""
    if not os.environ.get("ANTHROPIC_API_KEY", "").strip():
        raise GenerationError("The Claude API key isn't set. Add ANTHROPIC_API_KEY to the .env file and restart.")
    if settings.ANTHROPIC_WORKSPACE_ID:
        return anthropic.Anthropic(default_headers={"anthropic-workspace-id": settings.ANTHROPIC_WORKSPACE_ID})
    return anthropic.Anthropic()


class _anthropic_errors:
    """Turns Anthropic SDK exceptions into plain GenerationErrors. final names the model setting in use."""

    def __init__(self, final=False):
        self.final = final

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        if exc is None or not isinstance(exc, anthropic.AnthropicError):
            return False
        if isinstance(exc, anthropic.AuthenticationError):
            raise GenerationError("The Claude API key is missing or invalid. Set ANTHROPIC_API_KEY on the server.")
        if isinstance(exc, anthropic.PermissionDeniedError):
            raise GenerationError("The Claude API key doesn't have access to this model.")
        if isinstance(exc, anthropic.NotFoundError):
            raise GenerationError(f"The Claude model wasn't found. Check {model_setting(self.final)} in the .env file.")
        if isinstance(exc, anthropic.RateLimitError):
            raise GenerationError("Claude is busy right now (rate limit). Please try again in a minute.")
        if isinstance(exc, anthropic.BadRequestError):
            logger.warning("Claude rejected the request: %s", exc)
            raise GenerationError(f"Claude couldn't process this request: {exc.message}")
        if isinstance(exc, anthropic.APIStatusError):
            logger.warning("Claude API error %s: %s", exc.status_code, exc)
            raise GenerationError("Claude returned a server error. Please try again shortly.")
        if isinstance(exc, anthropic.APIConnectionError):
            raise GenerationError("Couldn't reach the Claude API. Check the server's internet connection.")
        logger.warning("Claude client error: %s", exc)
        raise GenerationError("The Claude API isn't configured on this server. Set ANTHROPIC_API_KEY.")


def _anthropic_reply(response, model):
    usage = response.usage
    error = ""
    if response.stop_reason == "refusal":
        error = "Claude declined to generate questions for this material."
    elif response.stop_reason == "max_tokens":
        error = "The answer was cut off. Try asking for fewer questions at a time."
    return Reply(
        text="".join(block.text for block in response.content if block.type == "text"),
        model=getattr(response, "model", "") or model,
        request_id=getattr(response, "_request_id", "") or "",
        input_tokens=usage.input_tokens or 0, output_tokens=usage.output_tokens or 0,
        cache_read_tokens=getattr(usage, "cache_read_input_tokens", 0) or 0,
        cache_write_tokens=getattr(usage, "cache_creation_input_tokens", 0) or 0,
        error=error,
    )


def _ask_anthropic(system, messages, schema, *, effort, model, final=False):
    """Claude with structured output and Anthropic's server-side fallback, streamed so long papers don't time out."""
    client = claude_client()
    with _anthropic_errors(final):
        with client.beta.messages.stream(
            model=model,
            max_tokens=ANTHROPIC_MAX_TOKENS,
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
    return _anthropic_reply(response, model)


# ---------- Gemini ----------

# finish_reason / block_reason values meaning Gemini refused on policy grounds.
_GEMINI_BLOCKED = {"SAFETY", "PROHIBITED_CONTENT", "BLOCKLIST", "SPII", "IMAGE_SAFETY", "IMAGE_PROHIBITED_CONTENT",
                   "MODEL_ARMOR", "JAILBREAK", "OTHER_BLOCKED"}
_GEMINI_DECLINED = ("Gemini declined to write questions for this material. Try rewording the request or choosing "
                    "fewer chapters.")
_TRANSPORT_ERRORS = (httpx.TransportError, httpx2.TransportError)


def gemini_client():
    """The Gemini client. The key is passed explicitly: the SDK would otherwise also read GOOGLE_API_KEY, which
    wins over GEMINI_API_KEY and could send requests (and bills) to another project. vertexai=False likewise stops
    GOOGLE_GENAI_USE_VERTEXAI / GOOGLE_GENAI_USE_ENTERPRISE from switching to Google Cloud's endpoint and auth.
    Use it as a context manager so its HTTP connections are closed after the request."""
    key = os.environ.get("GEMINI_API_KEY", "").strip()
    if not key:
        raise GenerationError("The Gemini API key isn't set. Add GEMINI_API_KEY to the .env file and restart.")
    return genai.Client(api_key=key, vertexai=False, http_options=genai_types.HttpOptions(
        timeout=GEMINI_TIMEOUT_MS,
        # A couple of retries when Google is briefly overloaded; nothing else is retried.
        retry_options=genai_types.HttpRetryOptions(attempts=3, http_status_codes=[429, 503]),
    ))


def _gemini_parts(content):
    """Anthropic-style content blocks as google-genai parts. PDFs go inline as bytes (Gemini reads pages and
    figures); text documents as text with their title. cache_control is dropped: Gemini caches repeated prefixes
    by itself, and the books already come first."""
    if isinstance(content, str):
        return [genai_types.Part(text=content)]
    parts = []
    for block in content:
        if block["type"] == "text":
            parts.append(genai_types.Part(text=block["text"]))
        elif block["type"] == "document" and block["source"]["type"] == "base64":
            parts.append(genai_types.Part(text=f"Document: {block.get('title', '')}"))
            parts.append(genai_types.Part.from_bytes(data=base64.standard_b64decode(block["source"]["data"]),
                                                     mime_type=block["source"]["media_type"]))
        elif block["type"] == "document" and block["source"]["type"] == "text":
            parts.append(genai_types.Part(
                text=f"<document title=\"{block.get('title', '')}\">\n{block['source']['data']}\n</document>"))
        else:
            raise ValueError(f"Unsupported content block for Gemini: {block['type']}")
    return parts


_GEMINI_UNSENDABLE = ("This request couldn't be sent to Gemini (it has content Gemini can't take). Please try again, "
                      "and ask the system administrator if it keeps happening.")


def _gemini_contents(messages):
    """The messages as google-genai contents. Malformed or unsupported content (a bug in the request builder, not
    something staff can fix) becomes a plain GenerationError; the details are logged without the content."""
    contents = []
    for m in messages:
        try:
            parts = _gemini_parts(m["content"])
        except (ValueError, KeyError, TypeError) as exc:
            logger.warning("Couldn't convert a message for Gemini: %s", type(exc).__name__)
            raise GenerationError(_GEMINI_UNSENDABLE)
        if not parts or not any(part.text or part.inline_data for part in parts):
            logger.warning("Empty message content for Gemini")
            raise GenerationError(_GEMINI_UNSENDABLE)
        contents.append(genai_types.Content(role="model" if m["role"] == "assistant" else "user", parts=parts))
    return contents


def _gemini_error(exc, final=False):
    """A plain GenerationError for a google-genai API error (codes from Google's API error reference)."""
    code, status, message = exc.code, (exc.status or ""), (exc.message or "")
    lowered = message.lower()
    if code == 401 or "api key not valid" in lowered or status == "UNAUTHENTICATED":
        return GenerationError("The Gemini API key is missing or invalid. Check GEMINI_API_KEY in the .env file.")
    if code == 402 or "prepay" in lowered:
        return GenerationError("The Gemini prepaid credit is used up. Top it up in Google AI Studio → Billing "
                               "(or turn on auto-reload), then try again.")
    if status == "FAILED_PRECONDITION" or "billing" in lowered:
        return GenerationError("Gemini needs billing set up for this request. Check the Google project's billing "
                               "in Google AI Studio.")
    if code == 403:
        return GenerationError("The Gemini API key doesn't have access to this model.")
    if code == 404:
        return GenerationError(f"The Gemini model wasn't found. Check {model_setting(final)} in the .env file.")
    if code == 429:
        return GenerationError("Gemini is busy or the usage limit was reached (rate limit). Please try again in a "
                               "minute.")
    if isinstance(exc, genai_errors.ServerError) or (code or 0) >= 500:
        logger.warning("Gemini API error %s: %s", code, exc)
        return GenerationError("Gemini returned a server error. Please try again shortly.")
    logger.warning("Gemini rejected the request: %s", exc)
    return GenerationError(f"Gemini couldn't process this request: {message or status or code}")


def _reason(value):
    return getattr(value, "value", value) or ""


def _gemini_stream(model, contents, config, *, final=False):
    """Stream one request and fold the chunks into a Reply. Streaming keeps long final papers from hitting HTTP
    read timeouts; the text arrives in pieces and the usage and finish reason on the last chunk. The client is
    created per request and closed when it ends (the SDK's context manager)."""
    client = gemini_client()
    text, usage, finish, blocked, model_version, response_id = [], None, "", "", "", ""
    with client:
        try:
            for chunk in client.models.generate_content_stream(model=model, contents=contents, config=config):
                model_version = chunk.model_version or model_version
                response_id = chunk.response_id or response_id
                usage = chunk.usage_metadata or usage
                if chunk.prompt_feedback and chunk.prompt_feedback.block_reason:
                    blocked = _reason(chunk.prompt_feedback.block_reason)
                for candidate in chunk.candidates or []:
                    finish = _reason(candidate.finish_reason) or finish
                    for part in (candidate.content.parts or []) if candidate.content else []:
                        if part.text and not part.thought:
                            text.append(part.text)
        except genai_errors.APIError as exc:
            raise _gemini_error(exc, final)
        except _TRANSPORT_ERRORS as exc:
            logger.warning("Couldn't reach Gemini: %s", exc)
            raise GenerationError("Couldn't reach the Gemini API. Check the server's internet connection.")
        except Exception:
            # An SDK bug or an unexpected response shape. The traceback goes to the log; the message shown in the
            # admin carries no exception text.
            logger.exception("Gemini client error")
            raise GenerationError("Gemini's reply couldn't be completed. Please try again.")

    error = ""
    if blocked or finish in _GEMINI_BLOCKED:
        error = _GEMINI_DECLINED
    elif finish == "RECITATION":
        error = "Gemini stopped because the answer quoted the book too closely. Try again or narrow the topic."
    elif finish == "MAX_TOKENS":
        error = "The answer was cut off. Try asking for fewer questions at a time."
    elif finish not in ("", "STOP", "FINISH_REASON_UNSPECIFIED"):
        error = f"Gemini stopped early ({finish}). Please try again."

    prompt = getattr(usage, "prompt_token_count", 0) or 0
    cached = getattr(usage, "cached_content_token_count", 0) or 0
    return Reply(
        text="".join(text), model=model_version or model, request_id=response_id,
        input_tokens=max(prompt - cached, 0),
        output_tokens=(getattr(usage, "candidates_token_count", 0) or 0)
        + (getattr(usage, "thoughts_token_count", 0) or 0),
        cache_read_tokens=cached, cache_write_tokens=0, error=error,
    )


def _thinking_level(effort):
    """Anthropic's effort as Gemini's thinking level. The app sends "medium" and "high"; anything Gemini has no
    level for (Anthropic's "xhigh" / "max") becomes HIGH rather than failing the request. Checked by name: the
    SDK's enum accepts unknown strings (for forward compatibility), so ThinkingLevel("MAX") wouldn't fail here but
    would be rejected by the API."""
    name = (effort or "medium").upper()
    return genai_types.ThinkingLevel[name] if name in ("MINIMAL", "LOW", "MEDIUM", "HIGH") \
        else genai_types.ThinkingLevel.HIGH


def _ask_gemini(system, messages, schema, *, effort, model, final=False):
    """Gemini via the stateless Generate Content API, with JSON-schema output and store=False."""
    config = genai_types.GenerateContentConfig(
        system_instruction=system,
        response_mime_type="application/json",
        response_json_schema=schema,
        max_output_tokens=GEMINI_MAX_OUTPUT_TOKENS,
        thinking_config=genai_types.ThinkingConfig(thinking_level=_thinking_level(effort)),
        store=False,  # never keep the request in the project's AI Studio logs
    )
    return _gemini_stream(model, _gemini_contents(messages), config, final=final)
