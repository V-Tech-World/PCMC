# Step 4 -- NLP + Risk Scoring (Offline) -- COMPLETE

Step 4 of the VoiceCare LK backend: an **offline, rule-based NLP engine** that
extracts symptoms from call transcripts and scores each conversation as
**low / medium / high risk**. No cloud APIs, no heavy dependencies, no model
downloads -- pure deterministic rules that a clinician can read and audit.

## What was built

### 1. `backend/app/services/nlp.py` (new) -- the engine

- **`interpret_yes_no(text)`** -- negation-aware yes/no parsing. No-words are
  checked before yes-words, so "no pain" / "I didn't take it" is never read
  as yes. Returns `True` / `False` / `None` (unrecognised).
- **`extract_symptoms(text)`** -- finds symptom mentions in free text using a
  vocabulary of 13 symptom groups (chest pain, breathlessness, bleeding,
  one-sided weakness, wound problems, vomiting, fever, dizziness, swelling,
  headache, cough, fatigue, generic pain). Every match runs through a
  **windowed negation check** (looks 3 tokens back for "no / not / didn't /
  without / denies" etc.), so *"no chest pain"* produces no finding. Specific
  phrases are matched before generic ones and consume their tokens, so
  "chest pain" never double-counts as generic "pain", and a negated
  "no chest pain" can't leak its tokens into a phantom "pain" finding.
- **`extract_severity(text)`** -- pulls mild / moderate / severe (plus
  colloquial cues like "unbearable", "terrible") from a transcript.
- **`assess_risk(findings, medication_missed)`** -- transparent additive
  scorer with red-flag overrides:
  - Red flags (chest pain, breathlessness, bleeding, one-sided weakness /
    speech trouble) -> **immediate HIGH**.
  - Each finding adds base points + severity bonus (mild +0, moderate +1,
    severe +2).
  - Missed medication adds +2.
  - score >= 5 -> HIGH, >= 2 -> MEDIUM, else LOW.
  - Every decision returns human-readable `reasons` (e.g.
    `"RED FLAG: chest pain"`, `"pain (moderate): +2"`) for clinician review.
- **`assess_conversation(answers)`** -- one-call entry point that combines
  the structured yes/no answers (medication, category question) with
  negation-aware free-text extraction over all transcripts.

### 2. `backend/app/services/dialogue.py` (wired)

- The Step 3 placeholder `_YES_WORDS`/`_NO_WORDS` matching is replaced by the
  NLP engine's `interpret_yes_no` (re-exported so existing imports keep
  working).
- New **`CallDialogue.assess_risk()`** -- runs the full pipeline over the
  answers recorded so far (safe to call on a partially-completed dialogue).

### 3. `backend/app/services/call_flow.py` (wired)

- When a dialogue call finishes, the risk assessment is computed and logged
  (`Risk assessment: level=... score=...` plus one log line per reason) so
  every completed call produces an auditable risk decision in the logs.

### 4. `backend/tests/test_step4.py` (new) -- 49 tests

Covers all four Step 4 test cases plus the dialogue wiring:

- **TC1** -- clear symptoms extracted from realistic free text (parametrised).
- **TC2** -- negated phrasing ("no chest pain", "the wound is not bleeding")
  is never flagged; negation stays local (a far-away "no" doesn't negate a
  later symptom).
- **TC3** -- low / medium / high transcripts score correctly, including
  threshold boundaries (1.9 / 2.0 / 4.9 / 5.0), missed-medication-only ->
  medium, red flag -> immediate high, and a HIGH reached purely by score.
- **TC4** -- empty / ambiguous transcripts ("", "uhh hmm", "banana",
  "I don't know") produce no findings and no crash; an empty answers list
  scores LOW.
- Dialogue wiring: `CallDialogue.assess_risk()` agrees with the standalone
  engine, works on partial dialogues, and the pain-severity follow-up still
  fires with the NLP-based interpretation.

## Verification

```
& backend/.venv/Scripts/python.exe -m pytest backend/tests -q
108 passed, 1 deselected  (59 pre-existing + 49 new)
```

## Deliberate design decisions

- **No spaCy / no ML model** -- the vocabulary is small and the questions are
  closed; rules are faster, fully offline, and auditable. The README lists
  spaCy as a future upgrade path; the `extract_symptoms` signature is
  designed so an ML backend could later implement the same contract.
- **Conservative by default** -- contradictory answers ("yes no") are read as
  NO rather than guessing; unrecognised answers return `None` and never raise.
- **Negation consumes tokens** -- a negated specific phrase can't resurface as
  a generic symptom (no phantom "pain" from "no chest pain").
- **Explainable output** -- every risk level carries `reasons: tuple[str, ...]`
  and `as_dict()` for storage/API use in later steps.

## Notes / limitations

- The symptom vocabulary and thresholds are a transparent placeholder for
  clinician review (README section 7) -- points and red flags live in one
  obvious table (`_SYMPTOMS`) at the top of `nlp.py`.
- Severity in free text is only picked up within 3 tokens of the symptom.
- Risk results are currently logged; persistence (per-call rows) is Step 6.

