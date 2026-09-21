"""
NLP engine + risk scorer (Step 4, offline first).

Rule-based, transparent, and fully offline -- no spaCy dependency yet (the
symptom vocabulary is small and the question flow is closed; spaCy can be
layered on later for lemmatisation if the vocabulary outgrows plain rules).

What it does (README Step 4):
- extract_symptoms(text): finds symptom mentions in free text with a windowed
  negation check, so "no pain" / "no chest pain" is never counted as a
  symptom (Step 4 TC2).
- interpret_yes_no(text): negation-aware yes/no interpretation for the fixed
  questions (authoritative replacement for the Step 3 placeholder).
- extract_severity(text): pulls mild / moderate / severe from a transcript
  (for the pain-severity follow-up).
- assess_risk(findings, medication_missed): transparent additive score with
  red-flag overrides -> low / medium / high with human-readable reasons.
- assess_conversation(dialogue_summary): one-call entry point that combines
  the structured answers + free-text extraction into a RiskAssessment.

The risk model is a transparent placeholder (README section 7: "simple,
transparent rule-based logic ... refined once clinician input is available").
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, replace
from typing import Any, Mapping, Sequence

logger = logging.getLogger("voicecare.nlp")

# ---------------------------------------------------------------------------
# Tokenisation
# ---------------------------------------------------------------------------

_WORD_RE = re.compile(r"[a-z']+")


def _tokens(text: str | None) -> list[str]:
    """Lowercase word tokens, keeping apostrophes ('don't' stays one token)."""
    return _WORD_RE.findall((text or "").lower())


# ---------------------------------------------------------------------------
# Negation detection
# ---------------------------------------------------------------------------

NEGATION_CUES: frozenset[str] = frozenset({
    "no", "not", "none", "nothing", "never", "nor", "without",
    "don't", "dont", "didn't", "didnt", "doesn't", "doesnt",
    "wasn't", "wasnt", "isn't", "isnt", "aren't", "arent",
    "can't", "cant", "cannot", "couldn't", "couldnt",
    "haven't", "havent", "hadn't", "hadnt",
    "denies", "deny", "denied", "negative",
})

# How many tokens BEFORE a symptom phrase to look for a negation cue.
# Covers the common patterns: "no pain", "I don't have any pain",
# "I have not had any chest pain", "there is no bleeding".
NEGATION_WINDOW = 3


def _is_negated(tokens: list[str], start: int) -> bool:
    """True if a negation cue appears within the window before `start`."""
    lo = max(0, start - NEGATION_WINDOW)
    return any(tok in NEGATION_CUES for tok in tokens[lo:start])


# ---------------------------------------------------------------------------
# Yes / no interpretation
# ---------------------------------------------------------------------------

_YES_WORDS: frozenset[str] = frozenset({
    "yes", "yeah", "yep", "yah", "yup", "sure", "correct", "true",
    "ok", "okay", "right", "affirmative",
})

_NO_WORDS: frozenset[str] = frozenset({
    "no", "nope", "not", "nah", "never", "nothing", "none", "nor",
    "don't", "dont", "didn't", "didnt", "doesn't", "doesnt",
    "wasn't", "wasnt", "isn't", "isnt", "haven't", "havent",
    "hadn't", "hadnt", "can't", "cant", "cannot", "without",
})


def interpret_yes_no(transcript: str | None) -> bool | None:
    """Negation-aware yes/no interpretation.

    NO-words are checked first so "no pain" / "not really" / "didn't take it"
    are not read as yes. Returns None when the transcript is empty or has no
    recognisable yes/no signal (Step 4 TC4: handled without crashing).
    """
    tokens = _tokens(transcript)
    if not tokens:
        return None
    if any(tok in _NO_WORDS for tok in tokens):
        return False
    if any(tok in _YES_WORDS for tok in tokens):
        return True
    return None


# ---------------------------------------------------------------------------
# Severity detection
# ---------------------------------------------------------------------------

_SEVERITY_LEVELS = ("mild", "moderate", "severe")

_SEVERITY_CUES: dict[str, str] = {
    "mild": "mild", "slight": "mild", "slightly": "mild",
    "moderate": "moderate", "medium": "moderate",
    "severe": "severe", "terrible": "severe", "awful": "severe",
    "unbearable": "severe", "excruciating": "severe", "intense": "severe",
    "horrible": "severe", "worst": "severe",
}

_SEVERITY_BONUS: dict[str | None, int] = {
    None: 0, "mild": 0, "moderate": 1, "severe": 2,
}


def extract_severity(transcript: str | None) -> str | None:
    """Pull mild / moderate / severe from a transcript.

    Used for the pain-severity follow-up ("How strong is the pain? Mild,
    moderate, or severe?") and to qualify symptoms found in free text.
    Returns None when no severity word is present.
    """
    tokens = _tokens(transcript)
    for tok in tokens:
        level = _SEVERITY_CUES.get(tok)
        if level:
            return level
    return None


def _severity_near(tokens: list[str], start: int, end: int) -> str | None:
    """Severity word within 3 tokens before or after a symptom mention."""
    lo = max(0, start - 3)
    hi = min(len(tokens), end + 3)
    for tok in tokens[lo:hi]:
        level = _SEVERITY_CUES.get(tok)
        if level:
            return level
    return None


# ---------------------------------------------------------------------------
# Symptom vocabulary
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class SymptomDef:
    """One entry in the symptom vocabulary."""

    symptom_id: str
    label: str
    triggers: tuple[str, ...]   # lowercase phrases; matched as whole tokens
    red_flag: bool = False      # any match -> immediate HIGH risk
    points: int = 1             # base contribution to the additive risk score


# Ordered most-specific / red-flag first so longer phrases consume tokens
# before generic ones (e.g. "chest pain" eats both tokens before generic
# "pain" can match).
_SYMPTOMS: tuple[SymptomDef, ...] = (
    SymptomDef("chest_pain", "chest pain",
               ("chest pain", "pain in my chest", "pain in the chest",
                "chest discomfort", "chest tightness", "tightness in my chest",
                "pressure on my chest", "pressure in my chest"),
               red_flag=True, points=5),
    SymptomDef("breathlessness", "breathlessness",
               ("breathless", "breathlessness", "short of breath",
                "shortness of breath", "hard to breathe", "hard to breath",
                "can't breathe", "cannot breathe", "struggling to breathe",
                "difficulty breathing", "trouble breathing"),
               red_flag=True, points=5),
    SymptomDef("bleeding", "bleeding",
               ("bleeding", "bleed", "bleeds", "oozing", "oozes"),
               red_flag=True, points=5),
    SymptomDef("neuro_deficit", "one-sided weakness / speech trouble",
               ("slurred speech", "trouble speaking", "can't speak",
                "face drooping", "facial droop", "one side of my body",
                "numb on one side", "weakness on one side"),
               red_flag=True, points=5),
    SymptomDef("wound_problem", "wound not clean / discharge",
               ("wound is not clean", "wound is not dry", "wound is dirty",
                "wound is wet", "wound is open", "wound is red",
                "wound is swollen", "wound is warm", "discharge from",
                "pus", "oozing from the wound", "wound is leaking"),
               points=3),
    SymptomDef("vomiting", "vomiting / nausea",
               ("vomiting", "vomit", "vomited", "throwing up", "threw up",
                "nausea", "nauseous", "queasy", "sick to my stomach"),
               points=3),
    SymptomDef("fever", "fever / chills",
               ("fever", "feverish", "running a temperature", "high temperature",
                "chills", "shivering", "feeling hot and cold"),
               points=3),
    SymptomDef("dizziness", "dizziness",
               ("dizzy", "dizziness", "lightheaded", "light headed", "faint",
                "feeling faint", "woozy", "unsteady"),
               points=2),
    SymptomDef("swelling", "swelling",
               ("swelling", "swollen", "puffy", "puffiness"),
               points=2),
    SymptomDef("headache", "headache",
               ("headache", "head ache", "migraine"),
               points=1),
    SymptomDef("cough", "cough",
               ("cough", "coughing", "coughed"),
               points=1),
    SymptomDef("fatigue", "weakness / fatigue",
               ("weak", "weakness", "no energy", "fatigue", "fatigued",
                "exhausted", "worn out"),
               points=1),
    # Generic "pain" LAST so specific sites (chest, head...) consume first.
    SymptomDef("pain", "pain",
               ("pain", "ache", "aching", "hurts", "sore", "discomfort",
                "throbbing", "burning", "stinging"),
               points=2),
)

_SYMPTOM_BY_ID: dict[str, SymptomDef] = {s.symptom_id: s for s in _SYMPTOMS}


# ---------------------------------------------------------------------------
# Symptom extraction (negation-aware)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class SymptomFinding:
    """One symptom detected in a transcript."""

    symptom_id: str
    label: str
    severity: str | None    # "mild" | "moderate" | "severe" | None (unknown)
    matched_text: str
    red_flag: bool
    points: int
    source: str             # "text" | "structured"


def extract_symptoms(text: str | None) -> tuple[SymptomFinding, ...]:
    """Negation-aware symptom extraction from free text (Step 4 TC1 + TC2).

    Returns only NON-negated findings -- "no chest pain" produces nothing.
    Overlapping matches are resolved in favour of the more specific symptom
    that is matched first (red flags before generic "pain").
    """
    tokens = _tokens(text)
    if not tokens:
        return ()

    used: set[int] = set()   # token indices consumed by a longer phrase
    findings: list[SymptomFinding] = []

    for sdef in _SYMPTOMS:   # specific / red-flag first
        finding = None
        for phrase in sdef.triggers:
            ptoks = tuple(_tokens(phrase))
            plen = len(ptoks)
            if plen == 0 or plen > len(tokens):
                continue
            for start in range(len(tokens) - plen + 1):
                if tuple(tokens[start:start + plen]) != ptoks:
                    continue
                span = set(range(start, start + plen))
                if span & used:
                    continue  # already covered by a more specific match
                # Consume the span either way: a negated specific phrase
                # ("no chest pain") must not leak its tokens to a generic
                # trigger ("pain"), which would create a phantom finding.
                used |= span
                if _is_negated(tokens, start):
                    continue  # negated -> not a finding (Step 4 TC2)
                finding = SymptomFinding(
                    symptom_id=sdef.symptom_id,
                    label=sdef.label,
                    severity=_severity_near(tokens, start, start + plen),
                    matched_text=" ".join(tokens[start:start + plen]),
                    red_flag=sdef.red_flag,
                    points=sdef.points,
                    source="text",
                )
                break
            if finding is not None:
                break
        if finding is not None:
            findings.append(finding)

    return tuple(findings)


# ---------------------------------------------------------------------------
# Risk assessment (transparent additive score + red-flag overrides)
# ---------------------------------------------------------------------------

_MEDIUM_THRESHOLD = 2.0
_HIGH_THRESHOLD = 5.0
_MEDS_MISSED_SCORE = 2.0


@dataclass(frozen=True)
class RiskAssessment:
    """The system's risk decision for one completed call."""

    risk_level: str                 # "low" | "medium" | "high"
    score: float
    findings: tuple[SymptomFinding, ...]
    reasons: tuple[str, ...]

    def as_dict(self) -> dict:
        return {
            "risk_level": self.risk_level,
            "risk_score": round(self.score, 1),
            "findings": [
                {"id": f.symptom_id, "label": f.label,
                 "severity": f.severity, "red_flag": f.red_flag}
                for f in self.findings
            ],
            "reasons": list(self.reasons),
        }


def assess_risk(
    findings: Sequence[SymptomFinding],
    medication_missed: bool = False,
) -> RiskAssessment:
    """Transparent additive risk score with red-flag overrides.

    Rules (documented for clinician review -- README section 7):
    - Any red-flag symptom -> immediate HIGH.
    - Each finding adds base points + severity bonus (mild +0, moderate +1,
      severe +2).
    - Missed medication adds +2 (adherence is a known post-discharge risk).
    - score >= 5 -> HIGH; score >= 2 -> MEDIUM; else LOW.
    """
    reasons: list[str] = []
    score = 0.0
    red_flag_labels: list[str] = []

    for f in findings:
        bonus = _SEVERITY_BONUS.get(f.severity, 0)
        contribution = f.points + bonus
        score += contribution
        label = f"{f.label} ({f.severity})" if f.severity else f.label
        if f.red_flag:
            red_flag_labels.append(f.label)
            reasons.append(f"RED FLAG: {label}")
        else:
            reasons.append(f"{label}: +{contribution:.0f}")

    if medication_missed:
        score += _MEDS_MISSED_SCORE
        reasons.append(f"medication not taken as prescribed (+{_MEDS_MISSED_SCORE:.0f})")

    if red_flag_labels:
        level = "high"
        reasons.insert(0, f"Red flag present ({', '.join(red_flag_labels)}) -> immediate escalation")
    elif score >= _HIGH_THRESHOLD:
        level = "high"
        reasons.append(f"risk score {score:.0f} >= {_HIGH_THRESHOLD:.0f}")
    elif score >= _MEDIUM_THRESHOLD:
        level = "medium"
        reasons.append(f"risk score {score:.0f} >= {_MEDIUM_THRESHOLD:.0f}")
    else:
        level = "low"
        if not findings and not medication_missed:
            reasons.append("no concerning symptoms or risk factors reported")
        else:
            reasons.append(f"risk score {score:.0f} below medium threshold")

    return RiskAssessment(
        risk_level=level, score=score,
        findings=tuple(findings), reasons=tuple(reasons),
    )






def assess_conversation(
    answers: Sequence[Mapping[str, Any]],
) -> RiskAssessment:
    """One-call entry: run the full NLP pipeline over a completed dialogue.

    `answers` is CallDialogue.summary() -- a list of dicts with
    question_id / kind / interpretation / transcript.

    Combines:
    - structured signals from the fixed yes/no answers (medication, category)
    - free-text symptom extraction from ALL transcripts (negation-aware)
    then runs the additive risk scorer.
    """
    by_qid: dict[str, tuple[Any, str]] = {}
    transcripts: list[str] = []

    for item in answers:
        qid = str(item.get("question_id", ""))
        interp = item.get("interpretation")
        transcript = str(item.get("transcript") or "")
        by_qid[qid] = (interp, transcript)
        transcripts.append(transcript)

    medication_missed = False
    pain_severity: str | None = None
    findings_by_id: dict[str, SymptomFinding] = {}

    def _add(fid: str, label: str, severity: str | None,
             red_flag: bool, points: int, source: str) -> None:
        if fid not in findings_by_id:
            findings_by_id[fid] = SymptomFinding(
                symptom_id=fid, label=label, severity=severity,
                matched_text="", red_flag=red_flag, points=points,
                source=source,
            )

    # -- structured signals from the fixed questions --------------------------

    med_interp = by_qid.get("medication", (None, ""))[0]
    if med_interp is False:
        medication_missed = True

    if by_qid.get("pain", (None, ""))[0] is True:
        pain_severity = by_qid.get("pain_severity", (None, ""))[0]
        if not isinstance(pain_severity, str):
            pain_severity = None
        _add("pain", "pain", pain_severity, red_flag=False, points=1,
             source="structured")

    if by_qid.get("category_general", (None, ""))[0] is True:
        _add("fever", "fever / chills / vomiting", None,
             red_flag=False, points=3, source="structured")

    if by_qid.get("category_surgical", (None, ""))[0] is False:
        _add("wound_problem", "wound not clean / discharge", None,
             red_flag=False, points=3, source="structured")

    if by_qid.get("category_cardiac", (None, ""))[0] is True:
        _add("cardiac_concern", "breathlessness or chest pain reported", None,
             red_flag=True, points=5, source="structured")

    # -- free-text extraction over every transcript (negation-aware) ----------

    # Extract per transcript, never on a join: negation and severity windows
    # must not cross utterance boundaries (a severity word from one answer
    # landing next to a symptom from another must not bleed across).
    for transcript in transcripts:
        for f in extract_symptoms(transcript):
            existing = findings_by_id.get(f.symptom_id)
            if existing is None:
                findings_by_id[f.symptom_id] = f
            elif f.severity and (existing.severity is None or
                                 _SEVERITY_LEVELS.index(f.severity) >
                                 _SEVERITY_LEVELS.index(existing.severity)):
                # Free text found a worse severity than the structured answer.
                findings_by_id[f.symptom_id] = replace(existing, severity=f.severity)

    return assess_risk(tuple(findings_by_id.values()), medication_missed)
