# How "severity" and the risk level are calculated in VoiceCare LK

One-page, code-accurate walkthrough of the whole path a call takes, from audio
to the LOW / MEDIUM / HIGH badge on the dashboard. Everything below is
`backend/app/services/nlp.py` unless stated otherwise.

---

## 0. The 30-second version

| Layer | Question it answers | Where |
|---|---|---|
| STT | what did the patient *say*? | `stt.py` (faster-whisper) |
| Interpretation | did they mean **yes/no**, and how **severe**? | `nlp.interpret_yes_no`, `nlp.extract_severity` |
| Findings | which **symptoms** were mentioned (and how bad)? | `nlp.extract_symptoms` |
| Scoring | add up points + red flags -> level | `nlp.assess_risk` |
| Consequence | what the patient hears, what the row shows, whether an alert is prepared | `dialogue.py`, `call_flow.py`, `alerts.py` |

There are **no** per-symptom weights in a database and no model: the whole
thing is a transparent, auditable rule set (README section 7 calls it a
"placeholder model, to be refined with clinician input").

---

## 1. The three question kinds

`dialogue.build_script()` builds one fixed flow for every patient:

```
medication      yes_no   "Did you take your medicines today as prescribed?"
pain            yes_no   "Are you feeling any pain right now?"
  +- pain_severity choice "How strong is the pain? Mild, moderate, or severe?"  (only if pain = yes)
category_<type> yes_no   one question per discharge type (see section 6)
anything_else   open     "...tell me anything else concerning you..."
```

* **yes_no** -> `interpret_yes_no()` returns `True` / `False` / `None`.
  * A transcript made only of answer words (`"yes"`, `"no no"`, `"yes no"`) is
    read **conservatively -- NO wins**, so a contradictory answer can never be
    upgraded into a yes.
  * Otherwise the **word the patient starts with decides** (`"yes, it is not
    healing"` is YES). This rule was added on 29 Sep 2026: scanning the whole
    sentence for a negation had turned every echoed question word into a NO.
  * If they start with neither, the old negation-first scan runs
    (`"I have no pain"` -> NO, `"I did take them"` -> YES).
* **choice** -> `extract_choice()` looks for the scripted word; if the patient
  answers in their own words instead (`"it is very bad"`, `"10/10"`,
  `"can't bear it"`), `dialogue.record_answer()` falls back to
  `extract_severity()` -- this is exactly the bug that once scored a live
  "severe" call as LOW.
* **open** -> no interpretation, only the transcript (free text is mined for
  symptoms).

---

## 2. The severity extractor (`extract_severity`)

Returns `mild` | `moderate` | `severe` | `None`, in this order:

1. **Multi-word phrases first** (longest first), and the tokens they cover are
   *masked* so shorter phrases cannot re-read them:
   * `severe`: "a lot of pain", "lots of pain", "so much pain", "very bad",
     "really bad", "so bad", "very painful", "really painful", "can't bear",
     "cannot bear"
   * `mild`: "not too bad", "not that bad", "not a lot", "not that much",
     "a little bit", "just a bit", "not bad", "not too much", "a little",
     "a bit of"
   * `moderate`: "a lot", "lots"
   * (masking is what keeps *"not too bad"* from being re-read as the bare
     word *"bad"*, and *"not a lot"* from being read as *"a lot"*.)
2. **Single-word cues** on the remaining tokens: mild / slight / minor;
   moderate / medium / **bad**; severe / terrible / awful / unbearable /
   excruciating / intense / horrible / worst / agony...
3. **Numeric scales** (`8/10`, `8 out of 10`) straight off the raw string:
   **0-3 = mild, 4-6 = moderate, 7-10 = severe**.
4. **The worst level wins**, not the first one: *"it was mild this morning but
   it is severe now"* is SEVERE.

Measured (29 Sep 2026):

| said | reads as |
|---|---|
| "mild" / "a little bit" / "not too bad" | mild |
| "moderate" / "a lot" | moderate |
| "a lot of pain" / "very bad" / "can't bear it" / "10/10" / "8 out of 10" | severe |
| "seven out of ten" (spoken words) | *None* -- known limit, digits only |

---

## 3. Findings: which symptoms, and how severe

`extract_symptoms(transcript)` returns findings (id, label, **severity**,
red_flag, points, matched_text) using a fixed vocabulary (`_SYMPTOMS`, ordered
most specific first):

* **Red flags (points 5)** -> `chest_pain`, `breathlessness`, `bleeding`,
  `neuro_deficit`, `collapse`
* **3 points** -> `wound_problem`, `vomiting`, `fever`, `confusion`,
  `urine_problem`
* **2 points** -> `dizziness`, `swelling`, generic `pain`
* **1 point** -> `headache`, `cough`, `fatigue`

Three guards keep it honest on real speech:

1. **Benign-phrase masking** - "blood pressure", "pain killer", "cough
   syrup"... are blanked first, so *"I checked my blood pressure this
   morning"* never raises a bleeding red flag.
2. **Negation window** - a negation cue within 3 tokens before a phrase
   cancels it ("no chest pain" -> nothing).
3. **Self-negated ability regex layer** - "I **can't** move my left arm",
   "I **can't** catch my breath" are *symptoms*, not negations, so they are
   matched by regex instead of being cancelled by rule 2.

**Severity attaches to a finding from within 3 tokens of the symptom**
(`_severity_near`): *"the chest pain is very bad"* tags `chest_pain` as SEVERE;
a severity word in a different sentence never leaks onto a symptom.

---

## 4. The structured answers become findings too

`assess_conversation(answers)` merges three sources by symptom id:

| Answer | Effect |
|---|---|
| `medication` = **no** | `medication_missed = True` (not a finding: **+2** flat) |
| `pain` = **yes** | `pain` finding, 1 point, severity = the `pain_severity` answer |
| `category_general` = yes | `fever` (+3) |
| `category_surgical` = **no** (wound is not clean) | `wound_problem` (+3) |
| `category_cardiac` = yes | `cardiac_concern`, **red flag** (+5) |
| `category_respiratory` = yes | `respiratory_concern` (+3) |
| `category_diabetic` = yes | `diabetic_concern` (+3) |
| every transcript | free-text symptoms (section 3) |

If free text grades a symptom **worse** than the structured answer did, the
worse severity wins.

**Ungraded pain** (patient said yes to pain but the severity could not be
established) adds **+2** and is listed as *"pain reported but severity not
established"* - a failed grading must never read as "no problem". (This is the
29 Sep fix for the live call that scored LOW/1.)

---

## 5. The score and the level (`assess_risk`)

```
score = sum(finding.points + severity_bonus)   severity_bonus: mild +0, moderate +1, severe +2
      + 2  if medication was not taken
      + 2  if pain was reported but never graded
```

**Decision, in order:**

1. any red-flag finding -> **HIGH** (`Red flag present -> immediate escalation`)
2. `score >= 5` -> **HIGH**
3. `score >= 2` -> **MEDIUM**
4. otherwise -> **LOW**

Every call also returns human-readable `reasons[]` (one line per finding, the
flag override, the thresholds) which are stored on the row and rendered in the
dashboard's "Why this risk level" box - the scoring is meant to be auditable by
a clinician.

### Worked examples (measured, not theoretical)

| Call | Findings | Score | Level |
|---|---|---|---|
| cardiac, meds missed, pain *very bad*, final answer "the wound is bleeding and I feel breathless" | pain(severe) 1+2, breathlessness 5 RF, bleeding 5 RF, +2 meds | 15 | **HIGH** |
| general, meds taken, pain *mild*, nothing else | pain(mild) 1+0 | 1 | **LOW** |
| general, pain yes but the grading was unintelligible | pain 1, ungraded 2 | 3 | **MEDIUM** |
| respiratory, "yes I have a new cough" | respiratory_concern 3, cough 1 | 4 | **MEDIUM** |
| respiratory, "I get breathless walking to the bathroom" | +3, cough 1, breathlessness 5 RF | 9 | **HIGH** |
| diabetic, "yes my foot is sore and it is not healing" | diabetic_concern 3, wound 3, pain 2 | 8 | **HIGH** |
| diabetic, "no nothing like that" | - | 0 | **LOW** |

---

## 6. Discharge types (`diagnosis_category`)

`dialogue.CATEGORIES` is the single source of truth for the question wording
*and* for the API validation (`POST /records/patients`, `POST /calls`). Each
key produces a `category_<key>` question id, which section 4 keys off.

| type | question | "yes" means |
|---|---|---|
| `general` | fever, chills, or vomiting? | fever (+3) |
| `surgical` | wound clean and dry, no bleeding/discharge? (**no** = problem) | wound_problem (+3) |
| `cardiac` | breathlessness or chest pain? | **red flag** (+5) |
| `respiratory` | new cough, wheezing, or trouble breathing at home? | respiratory_concern (+3) |
| `diabetic` | dizziness, blurred vision, or a foot sore that is not healing? | diabetic_concern (+3) |

Free-text red flags still apply to every type: a respiratory patient who says
"breathless" escalates to HIGH exactly like a cardiac one, and a diabetic
patient whose foot sore is not healing reaches HIGH on the points alone.

---

## 7. What the level changes downstream

| Level | Patient hears | System does |
|---|---|---|
| any | - | re-scored after **every** transcribed turn (`call_flow`, sub-millisecond) |
| HIGH | one-shot "I have noted that as urgent..." acknowledgement + an urgent closing line | alert text **prepared** and stored on the call row (`alert_status = ready`, see `alerts.py`) |
| MEDIUM | neutral closing | `alert_status = skipped` |
| LOW | neutral closing | `alert_status = skipped` |

The dashboard shows all of it: the badge, the "why this risk level" reasons,
the symptoms with their severity, and the prepared alert message with a Copy
button.

---

## 8. Reproducing any of this yourself

```powershell
cd backend
python -m pytest tests/test_step4.py -q     # the engine, end to end, offline
```

```python
# one-off: print the real breakdown for a single call
from app.services.dialogue import CallDialogue
d = CallDialogue("cardiac"); d.start()
for answer in ["no I forgot my dose", "yes", "it is very bad",
               "no it is not chest pain",
               "the wound is bleeding and I feel breathless"]:
    if d.current_question:
        d.record_answer(answer)
a = d.assess_risk()
print(a.risk_level, a.score)
for r in a.reasons:
    print(" -", r)
```

---

## 9. Known limits (deliberate, documented)

* Spoken number words ("seven out of ten") are not parsed - digits only.
* Free-text severity only applies within 3 tokens of the symptom it grades.
* "a lot" next to a symptom reads MODERATE (not severe) unless it says
  "a lot **of pain**".
* The benign-phrase list is fixed, so a new everyday phrase containing a
  trigger word has to be added to `_BENIGN_PHRASES`.
* The weights (red flag = HIGH, +2 for missed medication, ungraded pain +2,
  >= 5 / >= 2 thresholds) are the documented placeholder model - they are meant
  to be re-tuned with clinician input, and every change is visible in the
  `reasons[]` list the nurse sees.
