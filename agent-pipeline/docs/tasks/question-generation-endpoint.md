# Task: question-generation-endpoint

## Goal
Add a backend endpoint that generates exam questions with the Claude API, so teachers
can create question drafts from a topic instead of writing them by hand.

## Acceptance criteria
- [ ] An authenticated POST endpoint accepts: subject, topic, grade, difficulty
      (easy / medium / hard), question_type (mcq for now), count (1 to 20).
- [ ] Invalid input returns a 400 response with a clear error message.
- [ ] The AI call lives in its own service module and reads `ANTHROPIC_API_KEY`
      from the environment.
- [ ] The model is asked for JSON only; the response is validated (4 options per
      question, answer matches one option, no empty fields). One retry on invalid output.
- [ ] Valid questions are saved with status "draft" and returned in the response.
- [ ] API errors (low credit, rate limit, timeout) return a friendly 503 error and
      are logged, without exposing the API key or raw error details to the user.

## Out of scope
- Frontend changes.
- Question types other than MCQ.
- Deployment.

## Notes
- Model: claude-sonnet-5-5 (check https://docs.claude.com for current model names).
- Follow `docs/architecture.md` for where views, models and services go, and
  `docs/business-requirements.md` for who may generate questions.
- Saving drafts needs a model or field change: include the migration and update
  `docs/database-design.md`.
