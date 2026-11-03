"""The old name of check_ai, kept so existing notes and habits still work. It checks whichever provider
AI_PROVIDER selects."""

from .check_ai import Command as CheckAICommand


class Command(CheckAICommand):
    pass
