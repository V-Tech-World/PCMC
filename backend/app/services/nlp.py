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
# Phrases that look like symptoms but are not
# ---------------------------------------------------------------------------

# Some trigger words also appear in harmless everyday phrases. "blood" as a
# bleeding red flag would otherwise fire on "I checked my blood pressure this
# morning" and page the care team. Benign phrases are masked (blanked) before
# extraction: same token count, same positions, so windows and negation checks
# stay aligned.
_BENIGN_PHRASES: tuple[str, ...] = (
    "blood pressure", "blood test", "blood tests", "blood sugar",
    "blood sample", "blood group", "blood count", "blood report",
    "blood result", "blood culture", "blood thinner", "blood thinners",
    "cough syrup", "cough medicine", "cough tablet", "cough tablets",
    "pain killer", "pain killers", "painkiller", "painkillers",
    "pain relief", "pain clinic", "chest x ray",
)


def _mask_benign(tokens: list[str]) -> list[str]:
    """Blank the tokens of every benign phrase, keeping token positions."""
    masked = list(tokens)
    for phrase in _BENIGN_PHRASES:
        ptoks = phrase.split()
        plen = len(ptoks)
        for start in range(len(tokens) - plen + 1):
            if masked[start:start + plen] == ptoks:
                for i in range(start, start + plen):
                    masked[i] = "_"
    return masked


# ---------------------------------------------------------------------------
# Self-negated phrases ("I can't move my left arm")
# ---------------------------------------------------------------------------

# A few symptom phrases contain their own negation -- "can't breathe", "can't
# move my arm". The windowed negation check above would cancel them (the
# "can't" sits inside the window before "move my arm"), and listing every
# filler ("left arm", "right leg", "fingers") as a trigger phrase does not
# scale. So these are matched by a small regex layer instead, which requires
# the ability verb and an explicit limb/sense word: "I can't move my left arm"
# is a red flag, "I can move my arm" is not.
_SELF_NEGATED_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(
        r"\b(?:can't|cannot|couldn't|could not|unable to|not able to)\s+"
        r"(?:move|feel|use|lift)\s+(?:my|the)\s+(?:\w+\s+)?"
        r"(?:arm|leg|hand|foot|feet|fingers|toes|face)\b"
    ), "neuro_deficit"),
    (re.compile(
        r"\b(?:can't|cannot|couldn't|could not|unable to|not able to)\s+"
        r"(?:catch|get)\s+(?:my|a)\s+(?:deep\s+)?breath\b"
    ), "breathlessness"),
    (re.compile(
        r"\b(?:can't|cannot|couldn't|could not|unable to)\s+"
        r"(?:breathe|breath|get enough air)\b"
    ), "breathlessness"),
    (re.compile(
        r"\b(?:can't|cannot|couldn't|could not|unable to)\s+"
        r"(?:pass|pee|urinate)\b"
    ), "urine_problem"),
)


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


_ANSWER_WORDS: frozenset[str] = _YES_WORDS | _NO_WORDS


def interpret_yes_no(transcript: str | None) -> bool | None:
    """Negation-aware yes/no interpretation.

    The answer word comes FIRST on a real call: "yes, it is not healing",
    "no, I feel fine". Scanning for a negation cue anywhere in the sentence (as
    this did before) read the first of those as NO, because the patient echoed
    the question's own wording ("... a sore on your foot that is not
    healing?"). So:

    1. a transcript made only of answer words ("yes", "no no", "yes no") is
       still read conservatively -- NO wins, so a contradictory answer can
       never be upgraded into a yes;
    2. otherwise the word the patient starts with decides;
    3. if they start with neither, the old negation-first scan runs
       ("I have no pain" -> False, "I did take them" -> True).

    Returns None when the transcript is empty or has no recognisable yes/no
    signal (Step 4 TC4: handled without crashing).
    """
    tokens = _tokens(transcript)
    if not tokens:
        return None
    if all(tok in _ANSWER_WORDS for tok in tokens):
        return not any(tok in _NO_WORDS for tok in tokens)
    if tokens[0] in _YES_WORDS:
        return True
    if tokens[0] in _NO_WORDS:
        return False
    if any(tok in _NO_WORDS for tok in tokens):
        return False
    if any(tok in _YES_WORDS for tok in tokens):
        return True
    return None


# ---------------------------------------------------------------------------
# Severity detection
# ---------------------------------------------------------------------------

_SEVERITY_LEVELS = ("mild", "moderate", "severe")

# Single-word cues. "bad" is deliberately only MODERATE -- patients say "very
# bad" / "so bad" for severe pain, and those are handled by _SEVERITY_PHRASES.
_SEVERITY_CUES: dict[str, str] = {
    "mild": "mild", "slight": "mild", "slightly": "mild", "minor": "mild",
    "moderate": "moderate", "medium": "moderate", "bad": "moderate",
    "severe": "severe", "severely": "severe", "terrible": "severe",
    "awful": "severe", "unbearable": "severe", "excruciating": "severe",
    "intense": "severe", "horrible": "severe", "worst": "severe",
    "agony": "severe", "agonising": "severe", "agonizing": "severe",
}

# Multi-word cues, checked BEFORE the single words and consuming (masking) the
# tokens they cover. Without that masking the "bad" in "not too bad" would read
# as MODERATE, and the "bad" in "very bad" would be capped at MODERATE.
# Longest phrase first.
_SEVERITY_PHRASES: tuple[tuple[str, str], ...] = (
    ("a lot of pain", "severe"), ("lots of pain", "severe"),
    ("so much pain", "severe"), ("not too bad", "mild"),
    ("not that bad", "mild"), ("not a lot", "mild"),
    ("not that much", "mild"), ("a little bit", "mild"), ("just a bit", "mild"),
    ("very bad", "severe"), ("really bad", "severe"), ("so bad", "severe"),
    ("very painful", "severe"), ("really painful", "severe"),
    ("can't bear", "severe"), ("cannot bear", "severe"),
    ("not bad", "mild"), ("not too much", "mild"),
    ("a little", "mild"), ("a bit of", "mild"),
    ("a lot", "moderate"), ("lots", "moderate"),
)

# Spoken/written pain scales: "10 out of 10", "8/10", "3 out of ten".
_NUMERIC_SEVERITY_RE = re.compile(r"\b(10|[1-9])\s*(?:/|out of)\s*(?:10|ten)\b")

_SEVERITY_BONUS: dict[str | None, int] = {
    None: 0, "mild": 0, "moderate": 1, "severe": 2,
}


def _severity_from_tokens(tokens: list[str], text: str | None = None) -> str | None:
    """Highest (worst) severity cue in `tokens`, or None.

    Phrase cues are evaluated first and mask the tokens they cover, so
    "not too bad" stays MILD instead of being read as the single word "bad".
    `text` is the raw string the tokens came from; it is only used to read
    numeric pain scales ("8/10"), which tokenisation drops.

    The WORST level wins, not the first one: "it was mild this morning but it
    is severe now" is a severe report (first-match-wins under-scored it).
    """
    levels: list[str] = []
    masked = list(tokens)

    for phrase, level in _SEVERITY_PHRASES:
        ptoks = phrase.split()
        plen = len(ptoks)
        for start in range(len(tokens) - plen + 1):
            # Match against the masked list so a phrase can't re-consume tokens
            # already claimed by a longer one ("not a lot" before "a lot").
            if masked[start:start + plen] == ptoks:
                levels.append(level)
                for i in range(start, start + plen):
                    masked[i] = "_"

    for tok in masked:
        level = _SEVERITY_CUES.get(tok)
        if level:
            levels.append(level)

    if text:
        for match in _NUMERIC_SEVERITY_RE.finditer(text.lower()):
            rating = int(match.group(1))
            levels.append(
                "severe" if rating >= 7 else "moderate" if rating >= 4 else "mild"
            )

    if not levels:
        return None
    return max(levels, key=_SEVERITY_LEVELS.index)


def extract_severity(transcript: str | None) -> str | None:
    """Pull mild / moderate / severe from a transcript.

    Used for the pain-severity follow-up ("How strong is the pain? Mild,
    moderate, or severe?") and to qualify symptoms found in free text.
    Understands the scripted words, common lay phrasing ("very bad", "a lot of
    pain", "can't bear it", "not too bad") and numeric scales ("8/10").
    Returns None when no severity signal is present.
    """
    text = transcript or ""
    tokens = _tokens(text)
    if not tokens and not _NUMERIC_SEVERITY_RE.search(text.lower()):
        return None
    return _severity_from_tokens(tokens, text)


def _severity_near(tokens: list[str], start: int, end: int) -> str | None:
    """Severity cue within 3 tokens before or after a symptom mention.

    Windowed on purpose: a severity word from another sentence must not be
    attached to this symptom. Numeric scales are not read here (tokenisation
    drops the digits) -- those are transcript-wide and reach the score through
    the structured severity answer instead.
    """
    lo = max(0, start - 3)
    hi = min(len(tokens), end + 3)
    return _severity_from_tokens(tokens[lo:hi])


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
                "pressure on my chest", "pressure in my chest",
                "chest hurts", "chest is hurting", "my chest hurts",
                "chest feels heavy", "heavy in my chest", "chest feels tight",
                "tight chest", "chest is tight", "crushing chest",
                "squeezing in my chest"),
               red_flag=True, points=5),
    SymptomDef("breathlessness", "breathlessness",
               ("breathless", "breathlessness", "short of breath",
                "shortness of breath", "hard to breathe", "hard to breath",
                "can't breathe", "cannot breathe", "struggling to breathe",
                "difficulty breathing", "trouble breathing", "gasping",
                "gasping for air", "suffocating", "can't catch my breath",
                "cannot catch my breath", "couldn't catch my breath",
                "can't get my breath", "cannot get my breath",
                "can't take a deep breath", "breathing is hard"),
               red_flag=True, points=5),
    SymptomDef("bleeding", "bleeding",
               ("bleeding", "bleed", "bleeds", "oozing", "oozes", "blood",
                "bloody", "bleeding a lot", "blood clot", "vomiting blood",
                "blood in my vomit", "coughing up blood", "blood in my urine",
                "blood in my stool", "nosebleed", "nose bleed"),
               red_flag=True, points=5),
    SymptomDef("neuro_deficit", "one-sided weakness / speech trouble",
               ("slurred speech", "slurring my words", "trouble speaking",
                "can't speak", "face drooping", "facial droop",
                "face is drooping", "drooping", "one side of my body",
                "numb on one side", "weakness on one side", "one sided weakness",
                "one sided numbness", "numbness in my arm", "numbness in my leg",
                "numbness in my hand", "numb in my arm", "numb in my hand",
                "numb in my leg", "can't move my arm", "cannot move my arm",
                "can't move my leg", "cannot move my leg", "can't move my hand",
                "dragging my leg"),
               red_flag=True, points=5),
    SymptomDef("collapse", "fainting / blackout / seizure",
               ("passed out", "passing out", "fainted", "fainting",
                "blacked out", "lost consciousness", "unconscious",
                "collapsed", "seizure", "seizures", "convulsion",
                "convulsions"),
               red_flag=True, points=5),
    SymptomDef("wound_problem", "wound not clean / discharge",
               ("wound is not clean", "wound is not dry", "wound is dirty",
                "wound is wet", "wound is open", "wound is red",
                "wound is swollen", "wound is warm", "discharge from",
                "pus", "oozing from the wound", "wound is leaking",
                "wound smells", "smelly discharge", "foul smell",
                "smells foul",
                # Diabetic foot care: the phrase patients actually use.
                "foot sore", "sore on my foot", "sore on the foot",
                "wound on my foot", "foot wound", "foot ulcer",
                "ulcer on my foot", "sore is not healing",
                "wound is not healing", "not healing", "not healed",
                "foot is sore", "my foot is sore", "foot hurts",
                "my foot hurts", "sore foot", "foot is swollen"),
               points=3),
    SymptomDef("vomiting", "vomiting / nausea",
               ("vomiting", "vomit", "vomited", "throwing up", "threw up",
                "nausea", "nauseous", "queasy", "sick to my stomach"),
               points=3),
    SymptomDef("fever", "fever / chills",
               ("fever", "feverish", "running a temperature", "high temperature",
                "chills", "shivering", "feeling hot and cold", "burning up"),
               points=3),
    SymptomDef("confusion", "confusion / drowsiness",
               ("confused", "confusion", "disoriented", "disorientated",
                "not making sense", "talking nonsense", "drowsy",
                "very sleepy", "hard to wake", "difficult to wake",
                "not waking up"),
               points=3),
    SymptomDef("urine_problem", "urine / bowel problem",
               ("not passing urine", "cannot pass urine", "can't pass urine",
                "burning when i pee", "burning when i urinate",
                "no bowel movement", "haven't opened my bowels",
                "constipated", "constipation", "diarrhoea", "diarrhea"),
               points=3),
    SymptomDef("dizziness", "dizziness",
               ("dizzy", "dizziness", "lightheaded", "light headed", "faint",
                "feeling faint", "woozy", "unsteady", "blurred vision",
                "blurry vision"),
               points=2),
    SymptomDef("swelling", "swelling",
               ("swelling", "swollen", "puffy", "puffiness"),
               points=2),
    SymptomDef("headache", "headache",
               ("headache", "head ache", "migraine"),
               points=1),
    SymptomDef("cough", "cough",
               ("cough", "coughing", "coughed",
                # Respiratory discharge wording.
                "wheeze", "wheezing", "phlegm", "sputum", "chesty"),
               points=1),
    SymptomDef("fatigue", "weakness / fatigue / poor appetite",
               ("weak", "weakness", "no energy", "fatigue", "fatigued",
                "exhausted", "worn out", "tired", "tiredness", "lethargic",
                "no appetite", "poor appetite", "not eating",
                "lost my appetite"),
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

    Two extra passes keep the result usable on real calls:
    - benign phrases that merely contain a trigger ("blood pressure") are
      masked out, so they can't raise a red flag;
    - self-negated ability phrases ("I can't move my left arm") are matched by
      _SELF_NEGATED_PATTERNS, because the negation window would cancel them.
    """
    raw = text or ""
    tokens = _mask_benign(_tokens(raw))
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

    # Self-negated ability phrases ("I can't move my left arm"): regex layer,
    # skipped when a trigger phrase already produced this symptom.
    found_ids = {f.symptom_id for f in findings}
    for pattern, symptom_id in _SELF_NEGATED_PATTERNS:
        if symptom_id in found_ids:
            continue
        match = pattern.search(raw.lower())
        if match is None:
            continue
        sdef = _SYMPTOM_BY_ID[symptom_id]
        start = len(_tokens(raw[:match.start()]))
        plen = max(1, len(_tokens(match.group(0))))
        findings.append(SymptomFinding(
            symptom_id=sdef.symptom_id,
            label=sdef.label,
            severity=_severity_near(tokens, start, start + plen),
            matched_text=match.group(0),
            red_flag=sdef.red_flag,
            points=sdef.points,
            source="text",
        ))
        found_ids.add(symptom_id)

    return tuple(findings)


# ---------------------------------------------------------------------------
# Risk assessment (transparent additive score + red-flag overrides)
# ---------------------------------------------------------------------------

_MEDIUM_THRESHOLD = 2.0
_HIGH_THRESHOLD = 5.0
_MEDS_MISSED_SCORE = 2.0
_UNGRADED_PAIN_SCORE = 2.0


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
    ungraded_pain: bool = False,
    unanswered: int = 0,
) -> RiskAssessment:
    """Transparent additive risk score with red-flag overrides.

    Rules (documented for clinician review -- README section 7):
    - Any red-flag symptom -> immediate HIGH.
    - Each finding adds base points + severity bonus (mild +0, moderate +1,
      severe +2).
    - Missed medication adds +2 (adherence is a known post-discharge risk).
    - Ungraded pain (patient said yes, but the severity could not be
      established) adds +2 -- a failed grading must not read as "no problem".
    - score >= 5 -> HIGH; score >= 2 -> MEDIUM; else LOW.

    `unanswered` counts questions whose audio never arrived. It adds NO
    points (we must not invent risk from a transport failure) but it is stated
    in the reasons, because silence on a call is ambiguous: it reads as "no
    symptoms" to anyone skimming the record, which is how a real call with a
    lost answer ends up triaged as reassuring.
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

    if ungraded_pain:
        score += _UNGRADED_PAIN_SCORE
        reasons.append(
            "pain reported but severity not established "
            f"(+{_UNGRADED_PAIN_SCORE:.0f})"
        )

    if unanswered:
        reasons.append(
            f"{unanswered} question(s) answered with no usable audio -- risk "
            "may be understated; check the transcript before closing this call"
        )

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

    # Respiratory + diabetic discharge types (same shape as the general /
    # surgical rules above: one structured finding worth 3 points).
    if by_qid.get("category_respiratory", (None, ""))[0] is True:
        _add("respiratory_concern", "new cough / wheezing / breathing trouble",
             None, red_flag=False, points=3, source="structured")

    if by_qid.get("category_diabetic", (None, ""))[0] is True:
        _add("diabetic_concern", "dizziness / blurred vision / foot sore",
             None, red_flag=False, points=3, source="structured")

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

    # Pain answered "yes" but never graded (unusable follow-up answer, hangup,
    # unclear audio, or a wording the severity vocabulary still misses): the
    # score must not read that as "nothing wrong". A live severe call scored
    # LOW (1) exactly this way.
    pain_finding = findings_by_id.get("pain")
    ungraded_pain = (
        by_qid.get("pain", (None, ""))[0] is True
        and pain_finding is not None
        and pain_finding.severity is None
    )

    # Questions the patient never answered in a way we could use. Silence is
    # ambiguous on a phone call, so it is reported rather than silently read
    # as "no symptoms" (live 4 Oct 2026: 3 of 5 answers lost to the media leg
    # and the record still looked like a calm call).
    unanswered = sum(
        1 for interp, transcript in by_qid.values()
        if interp is None and not str(transcript or "").strip()
    )

    return assess_risk(
        tuple(findings_by_id.values()),
        medication_missed,
        ungraded_pain=ungraded_pain,
        unanswered=unanswered,
    )
