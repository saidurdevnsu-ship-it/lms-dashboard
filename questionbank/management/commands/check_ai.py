from django.core.management.base import BaseCommand, CommandError

from questionbank import llm


class Command(BaseCommand):
    help = ("Check that the AI provider's API key and models work (AI_PROVIDER: Claude or Gemini) by sending one "
            "tiny request per model. Costs a fraction of a cent.")

    def handle(self, *args, **options):
        status = llm.status()
        label = status["label"]
        checks = [(False, status["model"])]
        if status["final_model"] != status["model"]:
            if status["final_allowed"]:
                checks.append((True, status["final_model"]))
            else:
                # Same rule as generation: final-paper content isn't sent on Gemini's free tier, so don't test it.
                self.stdout.write(self.style.WARNING(
                    f"Final papers are off: GEMINI_PAID_TIER isn't true, so {status['final_model']} isn't checked."))
        for final, model in checks:
            self.stdout.write(f"Testing {label} model {model}{' (final papers)' if final else ''}…")
            try:
                reply = llm.ping(final=final)
            except llm.GenerationError as exc:
                raise CommandError(str(exc))
            if reply.error:
                raise CommandError(reply.error)
            self.stdout.write(self.style.SUCCESS(
                f"Connected. {reply.model} replied “{reply.text.strip()}” (request {reply.request_id or '-'}, "
                f"{reply.input_tokens} in / {reply.output_tokens} out tokens)."
            ))
