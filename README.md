# VoiceCare LK

A phone-call-based monitoring system for discharged hospital patients in Sri Lanka.
Patients get a phone call, answer a short set of structured health questions by
speaking naturally (Sinhala, Tamil, or English), and the system detects concerning
symptoms and alerts clinical staff -- no smartphone, app, or internet needed on
the patient's end.

This is a demo/proof-of-concept project. It does not need to be permanently
hosted -- the goal is to show the full pipeline works correctly end to end.

See `SURVEY_INSIGHTS.md` for what the patient survey data actually feeds into
(call timing, script wording, language priority) -- it's separate from this
file because it's a different kind of input (design/behavior decisions, not
architecture).

---

## 1. What the system does

1. Automatically calls a discharged patient when their check-in is due
   (also triggerable manually from the dashboard for demo purposes).
2. Asks a short, fixed set of health check-in questions out loud.
3. Listens to the patient's spoken answers and converts them to text.
4. Extracts symptoms and severity from that text.
5. Scores risk as low / medium / high.
6. Logs everything to the database.
7. If risk is medium/high, alerts clinical staff (SMS/email + dashboard).
8. Staff review the case on the dashboard and call the patient back if needed.

---

## 2. Architecture overview

```
 Patient's phone
       |  (real PSTN call)
       v
 Telephony provider (Zernio)
       |  live call audio, both directions, over a WebSocket
       v
 Backend (FastAPI)
   +- Media stream handler -- receives/sends raw call audio
   +- Speech-to-Text (faster-whisper) -- audio -> text
   +- NLP engine (rule-based + spaCy) -- text -> symptoms/severity
   +- Risk scorer (decision tree) -- symptoms -> low/medium/high
   +- Dialogue manager -- fixed question flow, one category-specific slot
   +- Text-to-Speech -- response text -> audio, sent back over the call
   +- Scheduler -- decides which patients are due for a call, today
       |
       v
 Database (SQLite + SQLModel: call logs, transcripts, risk levels)
       |
       v
 Alert service (SMS/email to on-call staff, only for medium/high risk)
       |
       v
 Clinical dashboard (React + Tailwind) -- staff review calls, transcripts,
 symptoms, and the system's decision; manual "call now" button; shows
 next scheduled automatic call time
```

**Confirmed working today:** the telephony leg end to end -- placing a real
outbound call to a Sri Lankan mobile number, the patient answering, and the
live call audio streaming into our backend over a WebSocket in a known format
(PCMA, 8kHz, mono). Everything below the "Backend" box is what we're building
next, piece by piece.

---

## 3. Components

| Component | Role | Tech |
|---|---|---|
| Telephony | Places/receives real phone calls, streams audio to us | Zernio (built on Telnyx) |
| Local tunnel | Exposes local backend to Zernio during dev | ngrok |
| Media stream handler | Receives raw call audio, sends audio back | FastAPI WebSocket endpoint |
| Speech-to-Text | Turns patient's spoken words into text | faster-whisper (open source, ~4x faster than base Whisper on CPU) |
| NLP engine | Finds symptoms, severity, negation in the text | Rule-based keyword matching + spaCy (lemmatization, negation) |
| Risk scorer | Turns extracted symptoms into a risk level | Custom decision-tree function, rules set with clinician input |
| Dialogue manager | Runs the fixed question flow, tracks call state | Python state machine in the backend |
| Text-to-Speech | Turns the system's response into spoken audio | TBD -- candidates: gTTS (quick/free), AI4Bharat Indic-TTS (Tamil) |
| Scheduler | Decides which patients are due for a call today, triggers it | APScheduler (in-process) |
| Database | Stores patients, calls, transcripts, symptoms, risk, alerts | SQLite via SQLModel |
| Alert service | Notifies staff of medium/high risk cases | SMS/email, triggered from backend on risk threshold |
| Clinical dashboard | Staff view calls, transcripts, risk, system decision, manual dial | React + Tailwind CSS |
| Containerization | One-command local demo setup | Docker (added once core pipeline works; SQLite file mounted as a volume, not baked into the image) |

---

## 4. Live-call latency handling

Speech-to-text is the slow part of a live call, not the NLP/risk logic (which
is single-digit milliseconds). Two things keep the call from feeling broken:
- **faster-whisper** instead of plain Whisper -- same accuracy, much faster on CPU.
- **A short pre-recorded filler** ("mm-hmm, one moment") plays immediately after
  the patient finishes talking, while STT/risk-scoring runs in the background --
  same trick a human on a call naturally uses while writing something down.
- Each turn is kept short (one question, ~8-10 second answer cap) so audio
  clips stay quick to transcribe.

---

## 5. Question flow

One fixed flow for every patient, not a fully custom script per diagnosis --
avoids both "too generic to be useful" and "unmaintainable, one script per
illness":
- **Core questions (same for all patients):** medication adherence, pain
  (yes/no + severity), general wellbeing / anything concerning.
- **One category-specific question**, chosen by the patient's `diagnosis_category`
  in the database -- e.g. surgical discharge asks about the wound/incision,
  cardiac discharge asks about breathlessness/chest pain.

Questions are closed/structured (yes-no, short expected answers) rather than
open-ended, so the NLP engine isn't fighting free-form rambling -- only the
final "anything else concerning you?" question tolerates open speech.

The opening line of every call addresses privacy/trust directly, per the
survey findings -- see `SURVEY_INSIGHTS.md`.

---

## 6. Multilingual plan

- **English** -- primary language for building and proving the pipeline first.
- **Tamil** -- reasonably supported by Whisper / AI4Bharat models; add once English pipeline is solid.
- **Sinhala** -- the weakest-supported language in available free/open STT and TTS tools,
  despite being the *most preferred* language in our own survey data (36/46 responses).
  This is a known, real research gap (confirmed in the project's own literature review),
  not an implementation mistake -- treat it as something to benchmark and report on,
  not assume will "just work" at the same accuracy as English.
- Patient picks their language at the start of the call; the dialogue manager
  branches into the matching script, STT language setting, and TTS voice from there.

---

## 7. NLP / decision-making data sources

- **For building the initial pipeline:** self-generated, realistic synthetic
  patient responses -- not dependent on interviews to get moving.
- **The patient survey** (`SURVEY_INSIGHTS.md`) informs call timing, frequency,
  script wording, and language priority -- not the symptom vocabulary itself.
- **Future interviews** (per the original research proposal) will supply the
  real symptom vocabulary, validate the synthetic-data pipeline, and calibrate
  risk thresholds with clinician input.
- **Initial risk scoring** uses simple, transparent rule-based logic to get
  the pipeline working end to end; thresholds get refined once clinician
  input is available.

---

## 8. Database

SQLite, via SQLModel (pairs naturally with FastAPI -- one model class gives
both the DB table and the request/response validation). No need for Postgres
at this scale, and it keeps the demo dependency-free.

```
patients: id, patient_code (anonymized), phone_number, language_pref,
          diagnosis_category, discharge_date

calls: id, patient_id, timestamp, transcript, extracted_symptoms (json),
       risk_level, reviewed (bool)

alerts: id, call_id, sent_at, channel (sms/email), acknowledged (bool)
```

---

## 9. User roles (kept simple, no extra complexity)

Three roles, all logging in through the same screen (see design system below).

| Role | Can do |
|---|---|
| **Nurse** | View assigned/all patient calls, listen to/read transcripts, mark alerts reviewed, trigger a manual call, add a short note to a case |
| **Doctor** | Everything a nurse can do, plus: review and close out escalated high-risk cases, view full patient history across calls |
| **Admin** | Manage patient records (add/edit/discharge), manage staff accounts, view system-wide stats -- no clinical review actions |

No granular permissions system beyond this -- one shared dashboard, role
just changes which actions are visible/enabled.

---

## 10. Frontend (clinical dashboard)

React + Tailwind CSS, talks to the backend over a normal REST API -- no
real-time/voice complexity on this side, it's a standard CRUD-style dashboard.

**Design system (from initial mockups):**
- Primary color: green (brand color, matches "VoiceCare LK" identity), white cards, soft rounded corners
- Login screen: centered white card on a green gradient background, role tabs (Doctor/Nurse/Admin), hospital selector, employee ID + password
- Dashboard shell: dark sidebar for navigation (with logout pinned at the bottom), light content area, profile icon top-right
- Must be responsive -- sidebar collapses on mobile, card grid reflows to single column

**Screens:**
- List of patient calls, sorted by risk level, with the time each call was placed.
- Next scheduled automatic call time shown per patient.
- Manual "call now" button (for demo purposes, alongside the automatic scheduler).
- Transcript + extracted symptoms + the system's resulting decision per call
  (e.g. "continue monitoring" vs. "escalated to nurse").
- Alerts list, filterable by status (open/reviewed).
- Simple JWT-based staff login (no patient-facing login needed).

---

## 11. Environment / config handling

- **One `.env` file per component**, not per-provider -- e.g. a single `.env`
  in the backend, not separate files like `.env.zernio`. Keeps setup simple
  and avoids forgetting to load the right one.
- `.env.example` is committed with placeholder values; real `.env` is not.
- Local dev exposes the backend to Zernio via **ngrok** (confirmed choice --
  the resulting `https://...ngrok-free.app` URL, converted to `wss://` with
  `/media-stream` appended, goes in `.env` as `PUBLIC_WSS_URL`).

---

## 12. Backend structure (illustrative -- will change as we build)

The file layout below is a rough map of responsibilities, not a fixed contract.
Names and boundaries will shift as pieces get built and merged/split.

```
backend/
|-- main.py              # FastAPI app, REST endpoints for the dashboard
|-- stream_server.py      # WebSocket endpoint, handles live call audio
|-- stt.py                 # faster-whisper wrapper
|-- nlp.py                  # keyword/symptom extraction, spaCy negation handling
|-- risk.py                  # decision-tree risk scoring
|-- dialogue.py                # question flow / conversation state machine
|-- tts.py                      # text -> speech audio
|-- db.py                        # SQLModel models and queries
|-- alerts.py                     # WhatsApp trigger to staff (Zernio sandbox)
|-- scheduler.py                    # decides which patients are due today
`-- outbound_call.py                  # places calls via Zernio API
```

---

## 13. Development plan (build in chunks, each tested before moving on)

Each step must pass its manual test cases before the next step starts.
Status is tracked here so it's always clear where we actually are.

### Step 1 -- Telephony connectivity -- **Done**
- [x] TC1: Outbound call script connects to a real Sri Lankan number within ~10s
- [x] TC2: Phone rings and greeting audio is clearly audible
- [x] TC3: `stream_server.py` logs `event=start` with correct `media_format` (PCMA, 8000Hz, mono)
- [x] TC4: On hangup, `event=stop` is received and the WebSocket disconnects cleanly

### Step 2 -- Batch transcription proof -- In progress
- [ ] TC1: A `.wav` file is created in `recordings/` after each call
- [ ] TC2: The file plays back clearly in a normal media player (no corruption)
- [ ] TC3: `transcribe_call.py` produces a transcript that matches what was actually said
- [ ] TC4: A full sentence is captured without being cut off mid-word

### Step 3 -- Turn-based structured dialogue -- Not started
- [ ] TC1: Only one question is asked at a time; the system waits for the answer
- [ ] TC2: Each turn's audio is captured separately, not the whole call at once
- [ ] TC3: The system correctly moves to the next question after each answer
- [ ] TC4: A full call completes all fixed questions in order, no repeats/skips

### Step 4 -- NLP + risk engine, offline first -- **Done**
- [x] TC1: Test transcripts with clear symptoms extract the correct symptoms
- [x] TC2: Negated phrasing ("no pain") is correctly not flagged as a symptom
- [x] TC3: Sample transcripts covering low/medium/high risk each score correctly
- [x] TC4: An empty or ambiguous transcript is handled without crashing

### Step 5 -- Wire NLP + risk into the live call -- COMPLETE
- [x] TC1: A real answer is transcribed and scored within a few seconds, without dead air
- [x] TC2: The risk level correctly changes the next spoken response
- [x] TC3: A full live call completes without the backend crashing
- [x] Final open question captured in a fixed 60 s window (patient may hang up; call closes either way)
- [x] Cost rails: max call duration per call + internal rate limiting (hourly/daily/concurrent) on POST /calls

See backend/step-5-readme.md for details.

### Step 6 -- Database (SQLModel + SQLite) -- COMPLETE
- [x] TC1: Each call produces a new row with correct transcript/risk/timestamp
- [x] TC2: The call is correctly linked to the right patient record
- [x] TC3: Data survives a backend restart

See backend/step-6-7-readme.md.

### Step 7 -- Alerts -- COMPLETE (WhatsApp via Zernio sandbox instead of SMS/email)
- [x] TC1: A high-risk call triggers a WhatsApp alert within a reasonable time (right after the call)
- [x] TC2: A low-risk call does not trigger an alert
- [x] TC3: The alert includes patient code, risk level, and key symptoms (plus full answers/transcripts)

See backend/step-6-7-readme.md. Live verification: backend/live-test-plan.md.

### Step 8 -- Scheduler -- COMPLETE (ships DORMANT: SCHEDULE_CALLS_ENABLED=false)
- [x] TC1: A patient due today is called automatically, with no manual trigger (proven with the dial mocked in tests/test_step8.py; live auto-dial only if you flip the switch)
- [x] TC2: A patient not yet due is not called (plus: slots older than the grace window are never dialed)
- [x] TC3: The next scheduled call time is correctly computed and shown (GET /schedule + the dashboard Schedule tab)

See backend/step-8-readme.md.

### Step 9 -- Frontend dashboard (Vite + React + TypeScript + Tailwind) -- COMPLETE
- [x] TC1: Login works for each of the 3 roles, showing the correct allowed actions (backend: tests/test_step9.py; UI gated by the GET /auth/roles permission matrix)
- [x] TC2: Call list displays with correct risk-level color coding (red/amber/green badges + filter chips)
- [ ] TC3: The manual "call now" button places a real call -- UI + confirm dialog built; ticks when you run live-test-plan.md section 7 (it replaces Call B's curl trigger, no extra call)
- [x] TC4: Layout is usable and responsive at mobile width (sidebar collapses to a drawer under 1024px)

See backend/step-9-readme.md. Verify: 169 backend tests + `cd frontend && npm run build`.

### Step 10 -- Multilingual (Tamil, then Sinhala) -- Not started
- [ ] TC1: Patient can select a language at the start of the call
- [ ] TC2: A Tamil test call produces a reasonable transcript
- [ ] TC3: TTS output in the selected language is understandable


---

## 14. Open decisions (to settle as we build)

- [ ] TTS engine choice for Sinhala/Tamil
- [ ] Real-time turn-detection (knowing when the patient stopped talking, mid-call)
- [ ] Final risk-scoring rules, once clinician input is available
- [ ] Docker Compose setup (after core pipeline works)

