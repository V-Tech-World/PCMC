"""
Offline tests for Step 4 (NLP engine + risk scorer, README section 7).

Pure logic -- no TTS, no STT, no model loading. Synthetic transcripts
(self-generated realistic responses, as the README prescribes) exercise:
- TC1: clear symptoms are extracted correctly from free text
- TC2: negated phrasing ("no chest pain") is NOT flagged as a symptom
- TC3: low / medium / high risk transcripts score correctly
- TC4: empty / ambiguous transcripts are handled without crashing

The dialogue wiring is tested too: CallDialogue.assess_risk() must agree
with the standalone engine on the same answers.
"""

from __future__ import annotations

import pytest

from app.services import nlp
from app.services.dialogue import CallDialogue, extract_choice
from app.services.nlp import (
    RiskAssessment,
    SymptomFinding,
    assess_conversation,
    extract_severity,
    extract_symptoms,
    interpret_yes_no,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _finding_ids(findings) -> set[str]:
    return {f.symptom_id for f in findings}


def _summary(overrides: dict[str, tuple] | None = None) -> list[dict]:
    """Build a dialogue-summary-like structure with sensible defaults."""
    base = {
        "medication": (True, "yes I took all my tablets"),
        "pain": (False, "no pain at all"),
        "category_general": (False, "no fever no chills no vomiting"),
        "category_surgical": (None, ""),
        "category_cardiac": (None, ""),
        "pain_severity": (None, ""),
        "anything_else": (None, "no I feel fine"),
    }
    base.update(overrides or {})
    return [
        {"question_id": qid, "kind": "yes_no", "interpretation": v[0],
         "transcript": v[1]}
        for qid, v in base.items()
    ]


# ---------------------------------------------------------------------------
# TC1 -- clear symptoms extracted correctly
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "transcript, expected",
    [
        ("I have a bad headache", {"headache"}),
        ("I've been vomiting since morning", {"vomiting"}),
        ("I feel dizzy when I stand up", {"dizziness"}),
        ("I have chest pain and I am short of breath",
         {"chest_pain", "breathlessness"}),
        ("there is some discharge from the wound", {"wound_problem"}),
        ("I have a fever and chills", {"fever"}),
        ("the wound is swollen and there is some bleeding",
         {"wound_problem", "bleeding"}),
    ],
)
def test_clear_symptoms_extracted(transcript, expected):
    findings = extract_symptoms(transcript)
    assert expected <= _finding_ids(findings), transcript


def test_clear_symptoms_full_sentence():
    """TC1: a realistic answer with several symptoms at once."""
    findings = extract_symptoms(
        "Doctor, I have a severe headache, I have been vomiting, "
        "and I feel a bit dizzy."
    )
    assert {"headache", "vomiting", "dizziness"} <= _finding_ids(findings)
    # No phantom findings from negation / filler words.
    assert "chest_pain" not in _finding_ids(findings)
    assert "fever" not in _finding_ids(findings)


def test_severity_attached_to_symptom():
    findings = extract_symptoms("the chest pain is severe")
    assert len(findings) == 1
    assert findings[0].symptom_id == "chest_pain"
    assert findings[0].severity == "severe"
    assert findings[0].red_flag is True


def test_generic_pain_does_not_shadow_chest_pain():
    """Specific sites are matched before the generic 'pain' trigger."""
    findings = extract_symptoms("I have chest pain")
    assert "chest_pain" in _finding_ids(findings)
    assert "pain" not in _finding_ids(findings)  # tokens already consumed


# ---------------------------------------------------------------------------
# TC2 -- negated phrasing is NOT flagged
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "transcript",
    [
        "no chest pain",
        "I have no fever",
        "I am not vomiting",
        "the wound is not bleeding",
        "no pain, no fever, no chills",
        "I don't have any headache",
        "I didn't feel dizzy",
        "there is no discharge from the wound",
        "I have not had any chest pain",
    ],
)
def test_negated_phrasing_not_flagged(transcript):
    findings = extract_symptoms(transcript)
    assert not findings, f"negated phrase flagged: {transcript!r} -> {findings}"


def test_negation_does_not_leak_across_symptoms():
    """'no X but I do have Y' keeps Y."""
    findings = extract_symptoms("no fever but I do have pain in my leg")
    ids = _finding_ids(findings)
    assert "fever" not in ids
    assert "pain" in ids


def test_negation_window_is_local():
    """A 'no' far from the symptom must not negate it (window = 3 tokens)."""
    # 'no' sits 4 tokens before 'pain' -> outside the window -> NOT negated.
    findings = extract_symptoms("no I said the pain is severe")
    assert "pain" in _finding_ids(findings)


# ---------------------------------------------------------------------------
# Severity extraction
# ---------------------------------------------------------------------------

def test_extract_severity_levels():
    assert extract_severity("it is mild doctor") == "mild"
    assert extract_severity("moderate I think") == "moderate"
    assert extract_severity("it is severe, unbearable") == "severe"
    assert extract_severity("uhh") is None
    assert extract_severity("") is None
    assert extract_severity(None) is None


# ---------------------------------------------------------------------------
# Yes / no interpretation (now from the NLP engine)
# ---------------------------------------------------------------------------

def test_interpret_yes_no_basic():
    assert interpret_yes_no("yes") is True
    assert interpret_yes_no("yeah I did") is True
    assert interpret_yes_no("no") is False
    assert interpret_yes_no("nope") is False
    assert interpret_yes_no("") is None
    assert interpret_yes_no(None) is None


def test_interpret_yes_no_negation_wins_over_yes():
    """NO-words are checked first so 'no pain' / 'not really' are not yes."""
    assert interpret_yes_no("no pain") is False
    assert interpret_yes_no("not really") is False
    assert interpret_yes_no("I didn't take it") is False
    assert interpret_yes_no("yes no") is False  # contradictory -> conservative NO


def test_interpret_yes_no_unrecognised():
    assert interpret_yes_no("banana") is None
    assert interpret_yes_no("!!! ???") is None


# ---------------------------------------------------------------------------
# TC3 -- low / medium / high risk transcripts
# ---------------------------------------------------------------------------

def test_low_risk_transcript():
    """TC3 low: everything negative, medication taken, no symptoms."""
    assessment = assess_conversation(_summary())
    assert assessment.risk_level == "low"
    assert assessment.score == 0.0
    assert not assessment.findings


def test_medium_risk_transcript():
    """TC3 medium: moderate pain + headache + weakness, no red flags."""
    assessment = assess_conversation(_summary({
        "pain": (True, "yes"),
        "pain_severity": ("moderate", "moderate"),
        "anything_else": (None, "I have a headache and I feel weak"),
    }))
    assert assessment.risk_level == "medium"
    ids = _finding_ids(assessment.findings)
    assert "pain" in ids and "headache" in ids and "fatigue" in ids
    assert not any(f.red_flag for f in assessment.findings)


def test_medium_risk_missed_medication():
    """Missed medication alone (+2) reaches the MEDIUM threshold."""
    assessment = assess_conversation(_summary({
        "medication": (False, "no I forgot to take it yesterday"),
    }))
    assert assessment.risk_level == "medium"
    assert assessment.score >= 2.0


def test_high_risk_transcript_red_flag():
    """TC3 high: chest pain reported -> immediate HIGH regardless of score."""
    assessment = assess_conversation(_summary({
        "category_cardiac": (True, "yes I have chest pain"),
        "anything_else": (None,
                          "the chest pain is severe and I am short of breath"),
    }))
    assert assessment.risk_level == "high"
    assert any(f.red_flag for f in assessment.findings)


def test_high_risk_by_score_without_red_flag():
    """Score >= 5 without a single red flag also reaches HIGH."""
    findings = (
        SymptomFinding("vomiting", "vomiting / nausea", "severe",
                       "vomiting", False, 3, "text"),
        SymptomFinding("fever", "fever / chills", "severe",
                       "fever", False, 3, "text"),
    )
    assessment = nlp.assess_risk(findings)  # 5 + 5 = 10, no red flag
    assert assessment.risk_level == "high"
    assert assessment.score == 10.0
    assert not any(f.red_flag for f in assessment.findings)


def test_risk_thresholds_documented():
    """Boundary check: 1.9 -> LOW, 2.0 -> MEDIUM, 4.9 -> MEDIUM, 5.0 -> HIGH."""
    def _one(points: float) -> str:
        f = SymptomFinding("x", "x", None, "x", False, points, "text")
        return nlp.assess_risk([f]).risk_level

    assert _one(1.9) == "low"
    assert _one(2.0) == "medium"
    assert _one(4.9) == "medium"
    assert _one(5.0) == "high"


def test_risk_reasons_are_human_readable():
    assessment = assess_conversation(_summary({
        "medication": (False, "I did not take it"),
        "category_general": (True, "yes fever"),
    }))
    assert assessment.reasons  # not empty
    assert all(isinstance(r, str) for r in assessment.reasons)
    assert any("medication" in r for r in assessment.reasons)


# ---------------------------------------------------------------------------
# TC4 -- empty / ambiguous transcripts handled
# ---------------------------------------------------------------------------

def test_empty_transcript_handled():
    """TC4: no answers at all -> LOW risk, no crash."""
    assessment = assess_conversation([])
    assert isinstance(assessment, RiskAssessment)
    assert assessment.risk_level == "low"
    assert not assessment.findings


@pytest.mark.parametrize(
    "transcript",
    ["", "   ", "uhh hmm", "I don't know", "maybe", "!!! ???",
     "banana banana", "??? ..", "um the the the"],
)
def test_ambiguous_transcript_handled(transcript):
    """TC4: gibberish / fillers / ambiguity -> no findings, no crash."""
    assert extract_symptoms(transcript) == ()
    assert interpret_yes_no(transcript) in (True, False, None)
    summary = _summary({"anything_else": (None, transcript)})
    assessment = assess_conversation(summary)
    assert assessment.risk_level in {"low", "medium", "high"}


def test_empty_transcript_does_not_crash_dialogue():
    """Empty recordings flow through the dialogue without errors."""
    dialogue = CallDialogue("general")
    dialogue.start()
    qid = dialogue.current_question.id
    dialogue.record_answer("")
    assert qid in {a["question_id"] for a in dialogue.summary()}
    assert dialogue.assess_risk().risk_level in {"low", "medium", "high"}


# ---------------------------------------------------------------------------
# Dialogue wiring (CallDialogue.assess_risk)
# ---------------------------------------------------------------------------

def _run_full_dialogue(category: str, answers: dict[str, str]) -> CallDialogue:
    """Drive a dialogue to completion (handles dynamic follow-ups)."""
    dialogue = CallDialogue(category)
    dialogue.start()
    while dialogue.current_question is not None:
        dialogue.record_answer(answers.get(dialogue.current_question.id, ""))
    return dialogue


def test_dialogue_assess_risk_agrees_with_engine():
    """CallDialogue.assess_risk() == assess_conversation(dialogue.summary())."""
    dialogue = _run_full_dialogue("cardiac", {
        "medication": "yes I took them",
        "pain": "no",
        "category_cardiac": "yes I have chest pain",
        "anything_else": "the chest pain is severe",
    })
    expected = assess_conversation(dialogue.summary())
    actual = dialogue.assess_risk()
    assert actual.risk_level == expected.risk_level
    assert actual.score == expected.score
    assert actual.risk_level == "high"


def test_dialogue_assess_risk_low_flow():
    dialogue = _run_full_dialogue("general", {
        "medication": "yes",
        "pain": "no pain at all",
        "category_general": "no fever",
        "anything_else": "no nothing to worry about",
    })
    assert dialogue.assess_risk().risk_level == "low"


def test_partial_dialogue_assess_risk_works():
    """assess_risk() is safe before the flow completes."""
    dialogue = CallDialogue("general")
    dialogue.start()
    first = dialogue.current_question.id
    dialogue.record_answer("yes I took them")
    assessment = dialogue.assess_risk()
    assert first in {a["question_id"] for a in dialogue.summary()}
    assert assessment.risk_level in {"low", "medium", "high"}


def test_extract_choice_unchanged_by_step4():
    """The choice parser is Step 3 behaviour -- must still work identically."""
    assert extract_choice("moderate", ("mild", "moderate", "severe")) == "moderate"
    assert extract_choice("I said severe", ("mild", "moderate", "severe")) == "severe"
    assert extract_choice("", ("mild", "moderate", "severe")) is None


def test_pain_severity_uses_nlp_interpretation():
    """The severity follow-up fires only when pain = True (Step 3 behaviour)."""
    dialogue = CallDialogue("general")
    dialogue.start()
    dialogue.record_answer("yes I took them")          # medication
    dialogue.record_answer("yes a lot of pain")        # -> True -> follow-up
    assert dialogue.current_question.id == "pain_severity"
    dialogue.record_answer("severe")
    assert dialogue.current_question.id != "pain_severity"
    assessment = dialogue.assess_risk()
    pain = next(f for f in assessment.findings if f.symptom_id == "pain")
    assert pain.severity == "severe"

















def test_free_text_severity_upgrades_structured_answer():
    """Severity from free text upgrades a structured answer, never downgrades.

    Also guards the per-transcript extraction: the severity word must come
    from the patient's own follow-up sentence, not bleed from another
    answer's transcript (regression for the joined-transcript bug).
    """
    assessment = assess_conversation(_summary({
        "pain": (True, "yes"),
        "pain_severity": ("mild", "mild"),
        "anything_else": (None, "the pain is severe now"),
    }))
    pain = next(f for f in assessment.findings if f.symptom_id == "pain")
    assert pain.severity == "severe"

    # And the reverse: free text never downgrades a structured severity.
    assessment = assess_conversation(_summary({
        "pain": (True, "yes"),
        "pain_severity": ("severe", "severe"),
        "anything_else": (None, "just a mild ache today"),
    }))
    pain = next(f for f in assessment.findings if f.symptom_id == "pain")
    assert pain.severity == "severe"
