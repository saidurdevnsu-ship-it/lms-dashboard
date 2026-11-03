import os

import anthropic
from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from questionbank.ai import claude_client


class Command(BaseCommand):
    help = "Check that the Claude API key and model work by sending one tiny request (costs a fraction of a cent)."

    def handle(self, *args, **options):
        model = settings.QUESTION_GENERATION_MODEL
        if not os.environ.get("ANTHROPIC_API_KEY"):
            raise CommandError("ANTHROPIC_API_KEY isn't set. Add it to the .env file in the project folder.")
        self.stdout.write(f"Testing Claude model {model}…")
        try:
            response = claude_client().messages.create(
                model=model,
                max_tokens=256,
                output_config={"effort": "low"},
                messages=[{"role": "user", "content": "Reply with just the word: connected"}],
            )
        except anthropic.AuthenticationError:
            raise CommandError("The API key was rejected. Check that you copied the whole key from platform.claude.com.")
        except anthropic.PermissionDeniedError:
            raise CommandError(f"This key's workspace doesn't have access to {model}.")
        except anthropic.NotFoundError:
            raise CommandError(f"Model {model} wasn't found. Check QUESTION_GENERATION_MODEL.")
        except anthropic.RateLimitError:
            raise CommandError("Rate limited, or the workspace has no credit. Check billing on platform.claude.com.")
        except anthropic.APIConnectionError:
            raise CommandError("Couldn't reach api.anthropic.com. Check the internet connection or proxy.")
        except anthropic.APIStatusError as exc:
            raise CommandError(f"Claude API error {exc.status_code}: {exc.message}")

        reply = "".join(b.text for b in response.content if b.type == "text").strip()
        self.stdout.write(self.style.SUCCESS(
            f"Connected. {response.model} replied “{reply}” (request {response._request_id}, "
            f"{response.usage.input_tokens} in / {response.usage.output_tokens} out tokens)."
        ))
