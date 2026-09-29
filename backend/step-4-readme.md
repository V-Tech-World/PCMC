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
  vocabulary of 16 symptom groups (chest pain, breathlessness, bleeding,
  one-sided weakness, collapse/blackout, wound problems, vomiting, fever,
  confusion/drowsiness, urine/bowel, dizziness, swelling, headache, cough,
  fatigue, generic pain), each with synonyms for the wording patients actually
  use ("my chest hurts", "gasping for air", "blood in my urine", "passed out").
  Every match runs through a **windowed negation check** (looks 3 tokens back
  for "no / not / didn't / without / denies" etc.), so *"no chest pain"*
  produces no finding. Specific phrases are matched before generic ones and
  consume their tokens, so "chest pain" never double-counts as generic "pain",
  and a negated "no chest pain" can't leak its tokens into a phantom "pain"
  finding. Two extra passes keep it usable on real calls:
  - **benign phrases** that merely contain a trigger ("blood pressure", "blood
    test", "pain killers", "cough syrup") are masked before extraction, so they
    can't raise a false red flag;
  - **self-negated ability phrases** ("I can't move my left arm", "I couldn't
    catch my breath") are matched by a small regex layer, because the negation
    window would otherwise cancel them.
- **`extract_severity(text)`** -- pulls mild / moderate / severe, now including
  how patients really answer: scripted words, colloquial phrasing ("very bad",
  "a lot of pain", "not too bad", "can't bear it") and numeric scales
  ("8/10", "10 out of 10"). The **worst** level in the sentence wins, so "mild
  this morning but severe now" is severe.
- **`assess_risk(findings, medication_missed, ungraded_pain)`** -- transparent
  additive scorer with red-flag overrides:
  - Red flags (chest pain, breathlessness, bleeding, one-sided weakness /
    speech trouble, collapse/blackout/seizure) -> **immediate HIGH**.
  - Each finding adds base points + severity bonus (mild +0, moderate +1,
    severe +2).
  - Missed medication adds +2.
  - **Ungraded pain** (patient answered "yes" to pain but the severity could
    not be established) adds +2 -> MEDIUM, never LOW.
  - score >= 5 -> HIGH, >= 2 -> MEDIUM, else LOW.
  - Every decision returns human-readable `reasons` (e.g.
    `"RED FLAG: chest pain"`, `"pain (moderate): +2"`,
    `"pain reported but severity not established (+2)"`) for clinician review.
- **`assess_conversation(answers)`** -- one-call entry point that combines
  the structured yes/no answers (medication, category question) with
  negation-aware free-text extraction over all transcripts.

### 2. `backend/app/services/dialogue.py` (wired)

- The Step 3 placeholder `_YES_WORDS`/`_NO_WORDS` matching is replaced by the
  NLP engine's `interpret_yes_no` (re-exported so existing imports keep
  working).
- New **`CallDialogue.assess_risk()`** -- runs the full pipeline over the
  answers recorded so far (safe to call on a partially-completed dialogue).
- The pain-severity **choice** question ("Mild, moderate, or severe?") now
  falls back to `extract_severity` when the patient doesn't use the scripted
  word: "it is very bad" is recorded as `severe` instead of `None`.

### 3. `backend/app/services/call_flow.py` (wired)

- When a dialogue call finishes, the risk assessment is computed and logged
  (`Risk assessment: level=... score=...` plus one log line per reason) so
  every completed call produces an auditable risk decision in the logs.

### 4. `backend/tests/test_step4.py` (new) -- 86 tests

Covers all four Step 4 test cases plus the dialogue wiring and the
post-live-test hardening:

- **TC1** -- clear symptoms extracted from realistic free text (parametrised),
  including synonym coverage for the red flags ("my chest hurts", "gasping for
  air", "blood in my urine", "passed out", "I cannot move my left arm").
- **TC2** -- negated phrasing ("no chest pain", "the wound is not bleeding")
  is never flagged; negation stays local (a far-away "no" doesn't negate a
  later symptom); benign phrases ("blood pressure", "blood test", "pain
  killers") are not symptoms; self-negated ability phrases still are.
- **TC3** -- low / medium / high transcripts score correctly, including
  threshold boundaries (1.9 / 2.0 / 4.9 / 5.0), missed-medication-only ->
  medium, red flag -> immediate high, and a HIGH reached purely by score.
- **TC4** -- empty / ambiguous transcripts ("", "uhh hmm", "banana",
  "I don't know") produce no findings and no crash; an empty answers list
  scores LOW.
- **Severity** -- scripted words, lay phrasing ("very bad", "a lot of pain",
  "not too bad", "can't bear it"), numeric scales ("8/10"), worst-level-wins,
  and the dialogue fallback for a free-worded severity answer.
- **Regression (severe call scored LOW)** -- a lone severe pain is never LOW,
  pain with an unusable severity answer is MEDIUM, mild graded pain stays LOW,
  and severe symptoms described **only in the final open answer** are scored.
- Dialogue wiring: `CallDialogue.assess_risk()` agrees with the standalone
  engine, works on partial dialogues, and the pain-severity follow-up still
  fires with the NLP-based interpretation.

## Verification

```
python -m pytest backend/tests/test_step4.py -q     # 86 passed
python -m pytest backend/tests -q                   # 212 passed, 1 deselected
```
(`real_stt` tests are deselected by `backend/pytest.ini`.)

## Post-live-test hardening (29 Sep 2026): "severe call scored LOW"

A live call where the patient reported severe pain was stored as **LOW (1)**.
Diagnosis from the scorer's own log line (`Risk assessment: level=low score=1`)
and the additive rules:

- a score of exactly 1 means one finding worth 1 point and **no severity
  bonus**: the structured pain answer (base 1) with an unparsed severity, or a
  single 1-point free-text finding;
- the severity question is a **choice** question, so an answer like *"very
  bad"*, *"a lot of pain"* or *"10/10"* produced `interpretation=None` and the
  pain signal collapsed to 1 point -> LOW. Nothing was wired wrongly: the
  final open `anything_else` answer *is* included in the scoring, and every
  exit path logs the full assessment with reasons.

Fixes (all additive to the rules above, no threshold changes):

1. **Severity understanding** -- lay phrasing, per-symptom severity phrases and
   numeric scales; the worst level in an utterance wins; `dialogue.record_answer`
   falls back to `extract_severity` when the choice word isn't spoken.
2. **Ungraded pain = +2** -- if pain was answered "yes" but no severity could be
   established, the call lands on MEDIUM for nurse triage instead of LOW.
3. **Vocabulary/red-flag coverage** -- synonyms for chest pain, breathlessness,
   bleeding, stroke-type symptoms; new collapse/blackout/seizure red flag; new
   confusion and urine/bowel groups; benign-phrase masking so "blood pressure"
   or "pain killers" can't raise a false red flag; self-negated ability phrases
   ("I can't move my left arm") are matched instead of being cancelled by the
   negation window.

Manual re-test recipe for the severe case:

1. Place a call for a patient with `diagnosis_category` `surgical` (or
   `cardiac`) and answer: medication **"no, I forgot"**, pain **"yes"**,
   severity **"it is very bad"**, then describe wound bleeding or breathlessness
   in the final open answer.
2. In the backend log expect
   `Risk assessment: level=high score=...` plus reason lines such as
   `RED FLAG: bleeding`, `medication not taken as prescribed (+2)`.
3. Check the call row / dashboard call detail shows `HIGH` with those reasons,
   and that a WhatsApp alert is attempted (alerts are HIGH only, Step 7).

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
- **Safety over silence for ungraded pain** -- if the patient says "yes" to
  pain but the severity can't be established, the call is MEDIUM (nurse
  triage), not LOW. A failed grading must never read as "nothing wrong",
  while a *graded* mild pain still scores LOW to avoid alert fatigue.

## Notes / limitations

- **The full scoring walkthrough lives in `backend/severity-model.md`** -- read
  that for the severity vocabulary, the symptom weights, the thresholds and
  worked examples. This file stays the implementation diary.
- The symptom vocabulary and thresholds are a transparent placeholder for
  clinician review (README section 7) -- points and red flags live in one
  obvious table (`_SYMPTOMS`) at the top of `nlp.py`.
- Severity in free text is only picked up within 3 tokens of the symptom.
  Numeric scales are read by `extract_severity` (transcript-wide), not by the
  windowed per-symptom check, and only in digit form ("8/10") -- a spoken
  "seven out of ten" is not parsed yet.
- "a lot" near a symptom is read as MODERATE severity; if that turns out to be
  noise on real calls, it is one entry in `_SEVERITY_PHRASES` to remove.
- Benign-phrase masking is a fixed list (`_BENIGN_PHRASES`); new everyday
  phrases that contain a trigger word should be added there.
- Risk results are logged and persisted per call (Step 6); alerts stay
  HIGH-only (Step 7).

---

## Change log -- 29 Sep 2026 (patient types + answer-word rule)

Two more discharge types were added the same way the first three work, and one
interpretation rule was corrected because the new question needed it.

### New discharge types

| type | `CATEGORIES` question | `assess_conversation` rule | vocabulary added |
|---|---|---|---|
| `respiratory` | "…any new cough, wheezing, or trouble breathing at home?" | yes -> `respiratory_concern` +3 | `wheeze`, `wheezing`, `phlegm`, `sputum`, `chesty` -> `cough` |
| `diabetic` | "…any dizziness, blurred vision, or a sore on your foot that is not healing?" | yes -> `diabetic_concern` +3 | `foot sore`, `sore on my foot`, `wound on my foot`, `foot ulcer`, `not healing`, `foot is sore`, `my foot hurts`… -> `wound_problem` |

Measured end to end: a new cough alone = **MEDIUM (4)**; "I get breathless
walking to the bathroom" = **HIGH (9)** through the existing
`breathlessness` red flag; "yes my foot is sore and it is not healing" =
**HIGH (8)**; "no nothing like that" = **LOW (0)**.

### `interpret_yes_no`: the answer word comes first

The diabetic question ends in "…that is **not** healing?", so the patient
answers "yes, it is not healing". The old rule scanned the whole sentence for
a negation cue and read that as **NO** -- the structured rule never fired (and
the same would happen on any question phrased with a negation).

New order, in `nlp.interpret_yes_no`:

1. an answer made **only of answer words** (`"yes"`, `"no no"`, `"yes no"`) is
   still read **conservatively -- NO wins** (the old contradiction guard);
2. otherwise the **leading** token decides -- `yes, it is not healing` -> YES;
3. if the sentence starts with neither, the old negation-first scan runs
   (`I have no pain` -> NO, `I did take them` -> YES).

Tests: `test_step3.test_new_discharge_types_have_their_own_question`,
`test_step3.test_diabetic_flow_asks_the_category_question_and_grades_the_answer`,
`test_step4.test_respiratory_*`, `test_step4.test_diabetic_*`,
`test_step4.test_interpret_yes_no_answer_word_comes_first`.

