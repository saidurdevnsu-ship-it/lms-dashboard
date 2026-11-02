"""What a final paper must look like: one slot per question the blueprint asks for, in blueprint order.

Generation, single-question regeneration and the lock check all read the blueprint through here, so they can't
disagree about sections, types or marks."""

from dataclasses import dataclass

from .models import Question


@dataclass(frozen=True)
class Slot:
    section: str
    question_type: str
    marks: int
    difficulty: str
    topic: str
    sample: str

    @property
    def type_label(self):
        return Question.QuestionType(self.question_type).label


def blueprint_slots(items):
    """items: the blueprint's BlueprintItems in order. Section names are cut to FinalQuestion.section's 50 chars."""
    return [
        Slot(section=(item.section or f"Section {n}")[:50], question_type=item.question_type,
             marks=item.marks_each, difficulty=item.difficulty, topic=item.topic,
             sample=item.sample_question.strip())
        for n, item in enumerate(items, 1)
        for _ in range(item.count)
    ]
