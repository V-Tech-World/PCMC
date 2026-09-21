# Survey Data -- What It Actually Feeds Into

Source: 46 responses, "Survey on Post-Discharge Patient Care and Monitoring."

**Important distinction, stated upfront:** this survey is about patient
*experience and preferences* (timing, trust, language). It contains no
free-text symptom descriptions, so it does **not** give us a symptom
vocabulary for the NLP engine. That still has to come from targeted
interviews where patients describe symptoms in their own words (as the
original research proposal planned). What the survey *does* give us is
real data for how the call itself should behave.

---

## What the survey confirms, with numbers

| Finding | Data | What it changes in our system |
|---|---|---|
| Sinhala is the dominant preferred language, not English | 36 of 46 preferred Sinhala, 10 preferred English (0 Tamil in this sample) | Confirms Sinhala can't stay an afterthought after English -- though Tamil should still be built, since this sample simply didn't include Tamil-preferring respondents |
| Best call time | Morning 22, Afternoon 13, Evening 11 | Default scheduled call window: morning, with afternoon as fallback |
| Preferred call frequency | Weekly 15, "Only when a problem is reported" 15, Every 2-3 days 11, Daily 5 | Default check-in schedule: weekly, not daily -- daily would likely feel intrusive to most patients |
| Real concerns about automated calls exist and are specific | Inconvenient timing (8+), privacy of health info (10+ combined), not trusting an AI system, language/communication difficulty, shared household phone | These need to be addressed directly in the call's opening script (see below), not assumed away |
| Hearing difficulty is non-trivial | 8 "mild difficulty," 5 "yes" out of 46 (~28% combined) | TTS should support a "repeat that" / slower-speech option -- not a nice-to-have |
| Most patients get no follow-up today | 25 of 46 said no one from the hospital followed up after discharge at all | Reinforces the actual problem this project addresses -- useful evidence for the report, not a system requirement |
| Common response to complications | 18 went back to the hospital, 9 visited a nearby clinic, 2 called the hospital, 2 waited it out, 2 didn't know what to do | The "no concerning symptoms" / low-risk response script should reinforce clear guidance, since "didn't know what to do" is a real, non-trivial outcome today |
| Missed follow-up appointment reasons | Cost (8), forgot (8), felt recovery was fine (7), transport difficulty (2) | Useful context for why phone-based monitoring specifically helps (removes cost/transport barriers) -- supports the *research case*, not the NLP engine |

---

## Direct wording to use in the call's opening script

Since privacy and AI-trust concerns showed up repeatedly, the first thing the
patient hears should briefly address them, not just launch into questions.
Something like:

> "This is a short automated call from [hospital] to check how you're
> recovering. Your answers are kept private and only seen by your care team.
> This will take about two minutes."

This isn't a nice-to-have -- it directly responds to the two most common
stated concerns (privacy, trust in an AI system) before the patient can
even form the objection.

---

## What still needs real interviews, not this survey

- Actual symptom vocabulary (how patients describe pain, wound issues, fever
  in their own words, in Sinhala/Tamil/English)
- Clinician-validated risk thresholds
- The exact wording of the category-specific question per diagnosis type

These remain open and tracked in the main README's open decisions.
