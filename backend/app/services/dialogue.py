"""
Dialogue manager: the fixed check-in question flow (README section 5).

Design rules from the README:
- One fixed flow for every patient, not a per-diagnosis script.
- Core questions (all patients): medication adherence, pain (yes/no +
  severity only if yes), general wellbeing / anything concerning.
- Exactly ONE category-specific question, chosen by diagnosis_category.
- Questions are closed/structured except the final open one, so the NLP
  engine (Step 4) doesn't fight free-form rambling.
- One question is asked at a time; the caller waits for the answer.

The yes/no interpretation is provided by the offline NLP engine
(app/services/nlp.py, Step 4) -- this module wires it into the fixed flow
and exposes CallDialogue.assess_risk() to score the completed conversation.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from app.services.nlp import (  # noqa: F401
    assess_conversation,
    extract_severity,
    interpret_yes_no,
)

__all__ = [
    "CATEGORIES",
    "DEFAULT_CATEGORY",
    "FINAL_QUESTION_ID",
    "URGENT_ACK_TEXT",
    "CallDialogue",
    "Answer",
    "Question",
    "build_script",
    "extract_choice",
    "interpret_yes_no",
]

logger = logging.getLogger("voicecare.dialogue")

# Wording rule (learned in live testing): the yes/no instruction comes FIRST.
# When it trailed the question ("Are you in pain? Please say yes or no"), the
# patient started answering the moment they heard the question and then heard
# "please say yes or no" playing over their answer -- confusing, and it made
# them think they had answered too early. Leading with the instruction gives
# them a beat to wait, then the question they must actually answer.
_YES_NO_LEAD = "Please answer yes or no. "

# One question per discharge type. Adding a type here is all the dialogue and
# the /calls + /records/patients validation need -- the risk scorer adds the
# matching `category_<name>` rule in nlp.assess_conversation, and the dashboard
# dropdown reads the same keys from the API.
CATEGORIES: dict[str, str] = {
    "general": _YES_NO_LEAD + "Do you have any fever, chills, or vomiting?",
    "surgical": (
        _YES_NO_LEAD + "Is your wound clean and dry, with no bleeding or discharge?"
    ),
    "cardiac": (
        _YES_NO_LEAD
        + "Have you had any breathlessness or chest pain since leaving the hospital?"
    ),
    # Respiratory discharge: the follow-up that matters is a NEW or worsening
    # cough / wheeze / breathing trouble at home, not the chronic baseline.
    "respiratory": (
        _YES_NO_LEAD
        + "Have you had any new cough, wheezing, or trouble breathing at home?"
    ),
    # Diabetic discharge: foot care and hypoglycaemia are the two things that
    # send these patients back to hospital, so they share one closed question.
    "diabetic": (
        _YES_NO_LEAD
        + "Have you had any dizziness, blurred vision, or a sore on your foot "
        "that is not healing?"
    ),
}

DEFAULT_CATEGORY = "general"

# The last, free-form question. call_flow treats it specially: instead of the
# 1.2 s silence detector (which cuts rambling answers short), it records for
# a fixed FINAL_ANSWER_SEC window and closes the call either way.
FINAL_QUESTION_ID = "anything_else"

# Spoken once, right before the next question, when the running risk reaches
# high (Step 5 TC2: the risk level must change the next spoken response).
URGENT_ACK_TEXT = (
    "Thank you. I have noted that as urgent, and your care team will be "
    "informed as soon as possible."
)


@dataclass(frozen=True)
class Question:
    """One turn in the call script."""

    id: str
    text: str
    kind: str = "yes_no"            # yes_no | choice | open
    choices: tuple[str, ...] = ()
    follow_up_id: str | None = None  # asked only if answer is yes


@dataclass
class Answer:
    question_id: str
    transcript: str
    kind: str
    interpretation: bool | str | None = None


def _follow_up_questions() -> dict[str, Question]:
    """Conditional questions, inserted only when triggered (e.g. pain = yes)."""
    return {
        "pain_severity": Question(
            id="pain_severity",
            # Options are part of the question, not a trailing command, so the
            # patient can't be talked over while answering.
            text="How strong is the pain? Mild, moderate, or severe?",
            kind="choice",
            choices=("mild", "moderate", "severe"),
        ),
    }


def build_script(diagnosis_category: str = DEFAULT_CATEGORY) -> list[Question]:
    """The main call flow: core questions + the one category question.
    Conditional follow-ups (e.g. pain severity) are NOT in the main flow --
    they are inserted by record_answer when triggered."""
    category = diagnosis_category.strip().lower()
    category_question_text = CATEGORIES.get(category)
    if category_question_text is None:
        raise ValueError(
            f"Unknown diagnosis_category {diagnosis_category!r}. "
            f"Valid: {', '.join(sorted(CATEGORIES))}"
        )

    return [
        Question(
            id="medication",
            text=_YES_NO_LEAD + "Did you take your medicines today as prescribed?",
            kind="yes_no",
        ),
        Question(
            id="pain",
            text=_YES_NO_LEAD + "Are you feeling any pain right now?",
            kind="yes_no",
            follow_up_id="pain_severity",
        ),
        Question(
            id=f"category_{category}",
            text=category_question_text,
            kind="yes_no",
        ),
        Question(
            id=FINAL_QUESTION_ID,
            # Open question: an invitation, not a yes/no, so it is not confused
            # with the previous questions and the patient can speak freely.
            # The wording sets expectations for the fixed capture window: say
            # everything in one go, then hang up -- we close the call after
            # FINAL_ANSWER_SEC no matter what, so "we will get back to you"
            # replaces a conversational goodbye.
            #
            # "after the beep" (2 Oct 2026, from live testing): the question is
            # long, and without a cue the patient either starts talking over the
            # last words or waits in silence for a prompt that never comes. The
            # question therefore names the BEEP, and call_flow plays two short
            # 1 kHz beeps right before the capture window opens -- the promise
            # in this sentence is only true because the beep exists.
            text=(
                "Final question. Please tell me anything else concerning you "
                "about your recovery. Please speak clearly after the beep, "
                "and when you are done, hang up. We will get back to you soon."
            ),
            kind="open",
        ),
    ]


def all_questions(diagnosis_category: str = DEFAULT_CATEGORY) -> dict[str, Question]:
    """Main flow + follow-ups, keyed by id (used to resolve follow-ups)."""
    by_id = {q.id: q for q in build_script(diagnosis_category)}
    by_id.update(_follow_up_questions())
    return by_id


def extract_choice(transcript: str, choices: tuple[str, ...]) -> str | None:
    text = (transcript or "").lower()
    for choice in choices:
        if choice in text:
            return choice
    return None


class CallDialogue:
    """Per-call state machine: pops the next question as answers come in."""

    def __init__(self, diagnosis_category: str = DEFAULT_CATEGORY) -> None:
        self.diagnosis_category = diagnosis_category.strip().lower()
        build_script(self.diagnosis_category)  # validate category early
        self.answers: dict[str, Answer] = {}
        self._queue: list[Question] = []
        self._started = False
        self._urgent_announced = False

    # -- lifecycle -----------------------------------------------------------

    def start(self) -> Question:
        """(Re)start the flow; returns the first question."""
        self._queue = build_script(self.diagnosis_category)
        self._started = True
        logger.info(
            "Dialogue started (category=%s, %d questions, first=%s)",
            self.diagnosis_category, len(self._queue), self._queue[0].id,
        )
        return self._queue[0]

    @property
    def current_question(self) -> Question | None:
        return self._queue[0] if self._queue else None

    @property
    def is_complete(self) -> bool:
        return self._started and not self._queue

    @property
    def closing_text(self) -> str:
        """Risk-aware goodbye (Step 5 TC2: risk changes what we say next).

        Low/medium risk keeps the neutral Step 3 wording; a HIGH assessment
        gets an urgent closing so the patient knows the follow-up will come
        fast. The level is recomputed from the answers recorded so far, so it
        is correct even if the flow was cut short by a hangup.

        Neither ending tells the patient to phone the hospital (4 Oct 2026):
        they are already on a post-discharge follow-up call, the care team owns
        the escalation, and repeating "call the hospital" on every unanswered
        read as though nothing was going to happen. We say what we did, then
        the patient hangs up in their own time -- see call_flow.wait_for_hangup.
        """
        if self.assess_risk().risk_level == "high":
            return (
                "Thank you. I have noted your symptoms as urgent, and your "
                "care team will contact you as soon as possible. Goodbye."
            )
        return (
            "Thank you. Your answers have been recorded and your care team "
            "will review them. Goodbye."
        )

    # -- advancing ------------------------------------------------------------

    def record_answer(self, transcript: str) -> Question | None:
        """Store the answer and return the next question (None = call done)."""
        if not self._queue:
            raise RuntimeError("record_answer called on a completed dialogue")
        question = self._queue.pop(0)

        answer = Answer(
            question_id=question.id, transcript=transcript, kind=question.kind
        )
        if question.kind == "yes_no":
            answer.interpretation = interpret_yes_no(transcript)
        elif question.kind == "choice":
            answer.interpretation = extract_choice(transcript, question.choices)
            if answer.interpretation is None:
                # The patient does not have to use the scripted word: "very
                # bad", "a lot of pain", "10/10", "can't bear it" all answer
                # the severity question. Fall back to the NLP severity
                # extractor so an unparsed grading is not silently dropped --
                # a live severe call scored LOW (1) exactly that way.
                level = extract_severity(transcript)
                if level is not None and level in question.choices:
                    answer.interpretation = level
                    logger.info(
                        "Choice answer %r read as %r by the NLP severity fallback",
                        transcript, level,
                    )
        self.answers[question.id] = answer
        logger.info(
            "Answer recorded: question=%s kind=%s interpretation=%r transcript=%r",
            question.id, question.kind, answer.interpretation, transcript,
        )

        # Conditional follow-up (pain severity), only when pain = yes.
        if question.follow_up_id and answer.interpretation is True:
            follow_up = all_questions(self.diagnosis_category).get(question.follow_up_id)
            if follow_up is not None:
                self._queue.insert(0, follow_up)
                logger.info("Inserted follow-up question: %s", follow_up.id)

        return self._queue[0] if self._queue else None

    # -- reporting ---------------------------------------------------------------

    def summary(self) -> list[dict]:
        return [
            {
                "question_id": a.question_id,
                "kind": a.kind,
                "interpretation": a.interpretation,
                "transcript": a.transcript,
            }
            for a in self.answers.values()
        ]

    def assess_risk(self):
        """Score the completed conversation with the Step 4 NLP engine.

        Combines the structured yes/no answers with negation-aware
        free-text symptom extraction and returns a RiskAssessment
        (low / medium / high + human-readable reasons).
        Safe to call before the flow is complete -- partial answers only.
        """
        return assess_conversation(self.summary())

    def pop_urgent_acknowledgment(self) -> str:
        """One-shot acknowledgment for a red-flag answer (Step 5 TC2).

        Returns URGENT_ACK_TEXT the first time the running risk reaches high,
        and "" on every later call, so the call driver can speak it before the
        next question without ever nagging the patient twice.
        """
        if self._urgent_announced:
            return ""
        if self.assess_risk().risk_level != "high":
            return ""
        self._urgent_announced = True
        logger.info("Red flag detected -- urgent acknowledgment armed")
        return URGENT_ACK_TEXT


