---
name: dialnexa-voice-agent
description: Best practices, templates and tooling for building, tuning, testing and debugging Kraya's AI calling agents on Dialnexa (outbound qualification / follow-up voice agents driven by Kraya sequence steps). Use this skill whenever the user mentions Dialnexa, an AI calling agent, a voice agent, a call agent prompt, call instructions for an ai_call sequence step, a Dialnexa call ID (call_xxx) or agent ID (agent_xxx), Hindi/Hinglish pronunciation on calls, boosted keywords, post-call analysis fields, or asks to check, fix, roll back or set up a calling agent for a client — even if they only paste a call ID and say "check this".
---

# Dialnexa voice agents for Kraya clients

Everything here was learned building the Dr. Atul Sardana bariatric-clinic agent (Sep 2026, ~50 agent versions, ~20 test calls, two sessions with Dialnexa CS). The short version: the model and voice were never the real problem. Conflicting instructions, missing pronunciation guidance, wrong transcriber hints, and testing from the wrong place were.

## Access from this repo

- The Dialnexa key is not in the environment, because each client has its own Dialnexa workspace and key. Ask the rep to paste the client's workspace key in chat, keep it only in the current session, and pass it inline to the helper on each command: `DIALNEXA_KEY=<key> python3 scripts/dialnexa_agent.py show <agent_id>`. Never write it to a file, a commit, a log, or a reply. At the end of the session remind the rep to reset the key in the Dialnexa dashboard, since it has passed through chat.
- This agent has no Kraya database access. Where this skill says to read a transcript from `dialnexa_webhook_payloads`, ask a developer for it, or read the lead's call notes through the Kraya API (`lead_calls.call_notes` holds transcript plus summary for Kraya-initiated calls). Dialnexa's own `GET /calls/{id}` never returns a transcript.
- Kraya-side edits (the `ai_call` sequence step content) go through `POST /api/auto-responder/sequence` with the account token from `POST /auth/login`, same as the `kraya-account-setup` skill.

## 1. How the pieces fit

A Kraya AI call is three layers, and each has one job:

| Layer | Lives in | Owns |
|---|---|---|
| **Agent prompt** | Dialnexa agent version `prompt_text` | Persona, tone, language rules, universal behaviour rules, guardrails, reactive knowledge base, close |
| **Call instructions** | Kraya `auto_responder_sequence_messages.content` of the `ai_call` step, sent as `{{call_instructions}}` in outbound metadata | The flow: clinic/business context, numbered steps, eligibility logic, outcome mapping |
| **Lead data** | Built by `Lead::buildAiCallLeadData()` from lead fields + every custom attribute, sent as `{{lead_data}}` | What is already known; the agent must never re-ask it |

Dialnexa also prepends its own platform system prompt (4k chars: voice rules, an off-topic handler that says "I might be missing something", email-speakability rules) and, when the agent language is English or Hinglish, appends a per-turn **language output override** based on what the lead last said. Your prompt sits between those two. Anything you write that contradicts them makes the model pick at random.

State every fact exactly once across the two documents. The first version of the Sardana prompt had the clinic facts, the guardrails and the cost rule each written three to four times across prompt and instructions (42k chars per turn). GPT-5.4 Nano could not hold it and produced the tics the client complained about. The working version is 12k + 13k chars.

Read `references/agent-prompt-template.md` and `references/call-instructions-template.md` before writing either document. They are the final working versions, annotated with what is client-specific.

## 2. Best practices checklist

Apply all of these to every client agent. The sections that follow explain the ones that need it.

1. **Write the prompt as English and Hindi pairs.** Every line the agent may speak gets an `English:` and a `Hindi:` version. §3.
2. **Hindi goes in Devanagari, and so does every word you want pronounced as Hindi** — loanwords, acronyms, proper nouns, the lead's name. Latin-script Hindi mispronounces. §3.
3. **Spell short forms out in full.** Doctor not Dr, Mister not Mr, number not no., kilogram not kg, appointment not appt. The TTS pauses at the dot and reads the abbreviation as letters.
4. **Numbers that must be heard digit by digit are written as words**: "nine eight one one three four nine three nine five" / "नौ आठ एक एक तीन चार नौ तीन नौ पांच". Phone numbers, OTPs, account numbers, pin codes. Digit strings get read as one giant number and split at random points. §3.
5. **Use `"…."` to place a pause.** Written exactly like that, ellipsis then a dot inside double quotes, dropped mid-line where the delivery needs a breath: `Thank you"…."the team will review your details and be in touch.` Use it before a close, between a confirmation and the next instruction, and after a question the lead needs a moment to answer. Do not scatter it; one or two per long turn.
6. **No language section in the prompt.** One line about mirroring the lead is enough. Dialnexa appends its own per-turn language override for English and Hinglish agents; a language block of your own contradicts it and the model starts flipping languages mid-call. §3.
7. **Call instructions and lead data go in the prompt**, under the block in §2.1, so the flow is the authority on what to say and in what order and the lead data is a fact list the agent never re-asks.
8. **Swap the voice whenever you change the prompt or the call instructions.** Pick a different voice on the version, publish, then set the intended voice back and publish again. Prompt edits published on their own have gone live still sounding like the previous agent; the swap is what makes the change land. Verify with a test call, not with `show`. §8.

### 2.1 The context block

Paste this verbatim into the prompt, after the role and before the behaviour rules. The two placeholders are filled by Kraya on every outbound call; on a dashboard test dial they arrive as `na`, which is why dashboard tests do not exercise the flow (§7).

```
###CONTEXT AND VARIABLES

#### Call instructions
the call flow and script for this call. This is the authority on what to say and in what order. Follow it exactly.
{{call_instructions}}

#### Lead Data

{{lead_data}}
```

## 3. Language and pronunciation

These rules came directly from Dialnexa CS and were confirmed by transcripts.

- **Opener in English, then mirror the lead.** The welcome message is fixed text the platform speaks. Keep it one English sentence with the proper noun in Devanagari. From the lead's first full reply, speak their language. Do not write a language section at all; Dialnexa's backend appends its own per-turn override for English/Hinglish agents and two rule sets for the same thing cause hallucination. One line telling the agent to mirror the lead is enough.
- **Every spoken line is a pair.** Write `English:` and `Hindi:` versions of every sentence the agent may say: questions, acknowledgments, deflections, confirmations, the close. A Hindi-only exemplar gets used in English calls and vice versa; the transcripts showed exactly that ("मैं पेमेंट लिंक भिजवा रही हूँ" in an English call).
- **Hindi lines are fully Devanagari,** including loanwords: कंसल्टेशन, पेमेंट लिंक, रिपोर्ट्स, सर्जरी, इंश्योरेंस. Acronyms spelt as spoken: एचबीए वन सी, सी पेप्टाइड, बीएमआई, टीपीए. Mixed-script Hindi lines make the TTS stumble mid-sentence.
- **Proper nouns in Devanagari in both languages**: डॉक्टर अतुल सरदाना, करोल बाग. "Karol Bagh" in Latin script becomes "Carol Bagh". The one exception is **WhatsApp**, always Roman, never transliterated (Dialnexa's own finding).
- **Never a short form.** "Dr." makes the TTS pause at the dot and say the name separately; "kg", "no.", "appt", "Mr." come out as spelt letters. Write Doctor / डॉक्टर, kilogram, number, appointment, Mister in full, in both languages.
- **Numbers.** Small numbers and fees as digits are fine (1500 rupees was spoken correctly). Large numbers as words (twenty five thousand / पच्चीस हज़ार). Phone numbers digit by digit as words: "nine eight one one three four…" / "नौ आठ एक एक…". The TTS reads "9811349395" as one giant number and splits digit strings at random points.
- **Rule: Hindi pronunciation for monetary amounts.** When speaking a monetary amount in Hindi or Hinglish, always say and write it in its natural Hindi/Devanagari wording, never as a digit-by-digit read of the numeral and never in English. A comma-grouped number on the Hindi side gets read by the TTS as one garbled digit string, not a proper amount. Use the Indian scale (हज़ार / लाख / करोड़):
  - ₹50,000 → पचास हज़ार
  - ₹1,00,000 → एक लाख
  - ₹2,00,000 → दो लाख
  - ₹50,000–₹1,00,000 → पचास हज़ार से एक लाख
  - Under ₹50,000 → पचास हज़ार से कम
  - Above ₹2,00,000 → दो लाख से अधिक
- **Lead names arrive in Latin script** from Kraya. Tell the model to say them in Devanagari (Satvik → सात्विक). Longer-term fix is transliterating in `buildAiCallLeadData()`.
- **Gender.** One line: "You are a woman; feminine verb forms for yourself (समझ गई, बता दूंगी)." Also tell it to match verb gender to the lead (रहते हैं / रहती हैं); models default to feminine for the lead too.
- **Give fixed shapes for the standard Hindi questions.** Left to improvise, Nano produced "आप अपनी उम्र और किस शहर में हैं?". With "आपकी उम्र कितनी है, और आप किस शहर में रहते हैं?" in the prompt it used it verbatim.

## 4. Behaviour rules that had to be learned the hard way

Each of these maps to a real failed call. Keep them in the prompt; they are cheap.

- **Acknowledgment cap.** The platform prompt says "use natural acknowledgements like okay, got it, understood", so every model starts every turn with one, plus the lead's name ("Got it, Satwik"). That was the client's number one complaint. Rule: at most one word, never followed by the name, never a standalone turn, not more than once every three turns, explicit ban list (thank you for sharing that, great, got it, understood, perfect).
- **Name at most twice per call.**
- **One question per turn.** A five-item comorbidity list plus a duration question in one 23-second turn made a lead hang up.
- **Accept short answers with an explicit mapping** (वजन, वेट, wazan, मोटापा → weight; शुगर, मधुमेह → blood sugar). Without it, GPT-4o mini asked "could you clarify weight, blood sugar or both?" three times to "हाँ, वजन के लिए" and the lead hung up.
- **A no is a no.** Never re-confirm, never follow a no with an "if yes" question, ask for a report value once.
- **Never ask for name or phone number.** Nano invented a "confirm your name and mobile so the payment link goes correctly" step and read back "John Doe" from the dashboard's placeholder lead data. Also: treat `na`, empty, or placeholder lead fields as unknown and never speak them.
- **Give the agent an `end_call` function.** Cascaded agents are created without one, so "end the call yourself" is impossible: the model re-reads the close into silence until the caller hangs up (five agent segments, 44 seconds, after one booking yes). PATCH `agent_functions: [{"display_name":"end_call","type":"end_call","description":"Judges the conversation and ends the call once its objective is met, or the moment the caller explicitly asks to hang up."}]` onto the version. Check with `show`; speech-to-speech agents get it by default.
- **After the booking yes: exactly two turns, then end the call yourself.** One confirmation turn, one close, and an explicit "say the payment-link line once". Without this the agent repeated the payment-link and scheduling-link sentences three times and waited for the lead to hang up.
- **Answer "when is my consultation" directly** (they pick the slot on the link after payment). Otherwise the agent repeats the payment-link question.
- **Never reverse a stated eligibility.** If a new fact (diabetes) surfaces after bariatric was stated, keep bariatric and add the extra questions.
- **Emergency means acute, at rest, right now.** "सांस लेने में थोड़ा दिक्कत" while walking is an obesity symptom; the first prompt sent that lead to emergency services. Give one disambiguating question.
- **Override the platform's off-topic line.** On an STT mishear the model says "I might be missing something, how does this relate…". Tell it to assume the line was unclear and ask once to repeat, in the lead's language.
- **Don't ban "in person" without giving the Hindi alternative** or you get "इन-पर्सन" and "fit-and-person" from the TTS.

## 5. Agent settings that matter

| Setting | Use | Why |
|---|---|---|
| Language | Hinglish (`lang_IPH82yvF7eN9oJ`) | Enables Dialnexa's per-turn language override; handles English and Hindi in one agent |
| LLM | GPT-5.4 Mini (`llm_739b4d91c2927e`) or GPT-4o Mini (`llm_meqtu4wm4xh2c6`) | Nano ignores rule-heavy prompts (tics, re-asks, invented steps). 5.4 Mini once paused 10s "thinking" on a BMI computation; watch turn gaps |
| Temperature | 0.4 | 0.9–1.0 is where language flips and phrasing tics came from |
| Voice speed | 1.05 | Client heard 0.9 as latency |
| Fallback LLM | Off, unless CS asks otherwise | Two models with different habits inside one call |
| max_call_duration_sec | ≥ 480 for an 11-step flow | Two early calls were cut at the 300s cap mid-booking |
| Timezone (agent level) | Asia/Kolkata | New agents default to America/Los_Angeles; the sales agent refused valid same-day slots for weeks because of this |
| Boosted keywords | ≤100 terms, ASCII only, comma separated | See §6 |
| Post-call analysis | Client-specific fields | New agents inherit template fields ("Department Assigned: Dermatology") that show up in the client's dashboard |

Turn-taking (`interruption_sensitivity`, `response_eagerness`, `responsiveness`, `pause_before_speaking_sec`) semantics are undocumented. Ask CS before changing; do not guess.

## 6. Boosted keywords

They hint the **transcriber** about what the lead might say; they do nothing for what the agent says. Devanagari is rejected (letters, digits, spaces only), 100 terms max. Prioritise by ambiguity, not frequency:

1. Proper nouns the STT mangles (Sardana, Karol Bagh, Apollo Spectra).
2. Romanised Hindi that gets heard as English, in every spelling you can think of (vajan, wazan, vazan → "weather income" was a real mishear).
3. The one-word replies the STT drops when under a second (haan, nahi, theek hai, bataiye, dono ke liye).
4. Medical acronyms and terms leads say (HbA1c, HOMA IR, C peptide, PCOD, TPA).
5. Units (kilo, feet, inch, centimeter).

Leave out plain English words the transcriber already gets (weight, height, cost, Monday) and generic city names.

## 7. Testing protocol

- **Test through Kraya, not the Dialnexa dashboard.** Dashboard dials send no metadata, so `{{call_instructions}}` and `{{lead_data}}` are "na" and the agent improvises the flow. Enable the sequence and add a fresh lead, or use `POST /api/auto-responder/sequence-message/preview-call`.
- **Clear custom attributes on reused test leads.** The post-call outcome analysis writes Height, Weight, BMI, Pathway back onto the lead; `buildAiCallLeadData()` sends every custom attribute as fact, so the next call skips straight to a disqualification. Better: never keep a "BMI" custom attribute at all.
- **Change one thing per test.** Model switch alone, then prompt. Otherwise you cannot tell which fixed or broke it.
- **Read the transcript from Kraya, not Dialnexa.** `GET /v1/calls/{id}` has no transcript; the completed-call webhook payload in `dialnexa_webhook_payloads` does (`$.payload.call.transcript`, `summary`, `recording_url`, `post_call_analysis`). `scripts/dialnexa_agent.py call <id>` prints it.
- **When the transcript shows the agent answering silence, split the recording.** Left channel is the caller, right is the agent. `ffmpeg silencedetect` on the caller channel shows whether the lead spoke (STT dropped it) or not (agent hallucinated). Sub-second Hindi replies get dropped by Soniox in `optimize_for_speed` mode; raise with CS, it is not a prompt bug.
- **Read turn gaps.** Caller end-time to agent start-time over ~3s is model latency, not TTS.
- **Assistant lines in the transcript are STT of the TTS audio**, not the model's text. "Dr." in an assistant line does not prove the model wrote "Dr.".
- **Swapping the voice is part of publishing a prompt change.** Publish the edited version with a different voice selected, then set the intended voice back and publish again. Prompt or call-instruction edits published without the swap have gone live still sounding like the previous agent. Confirm with a test call; `show` reports the new prompt either way.
- Log every version and what changed in a findings file next to the prompt copies. Rollback is "republish version N", so keep local copies of every prompt you publish.

## 8. Versioning and deployment mechanics

- Dialnexa dials the **latest published** version. `PATCH /v1/agents/{id}` with `version_number` edits that version in place; editing the draft does nothing live. Publish with `{"version_number": N, "is_published": true}`; Dialnexa then opens N+1 as the new draft. There is no `/publish` endpoint.
- Unknown fields in a PATCH are silently ignored, so verify by re-reading the agent after every write.
- `postcall_analysis` is an array on the version and is accepted by PATCH (see `references/dialnexa-api.md`).
- The workspace API key in creds.md only sees the Kraya-AI workspace. A client's key is KMS-encrypted in `ai_calling_agents.api_key`; decrypt with `KMSService->decrypt()` in `herd php artisan tinker`.
- Kraya call instructions are updated with `POST /api/auto-responder/sequence` (full sequence payload, message matched by id). The org's bearer token dies on re-login; expect 401 and ask for a fresh one.
- Use `scripts/dialnexa_agent.py` for show / patch / publish / calls / call. It needs the client key in an env var or file.

## 9. Speech-to-speech agents

A `Speech_To_Speech` pipeline agent (GPT-realtime) takes one self-contained prompt with the flow inlined (no `{{placeholders}}`, no Kraya connection). Inline the call instructions as a final "CALL FLOW" section, say explicitly that no lead data exists, fix the timezone, and pick a voice; the default is OpenAI "alloy". ~9.5 rupees a minute per CS.

## 10. What "working fine" looked like

The accepted version: English opener, mirrored language, one question per turn, fixed Hindi question shapes, correct eligibility stated once, comorbidities and reports asked once, fee stated, payment link accepted, one confirmation turn, one close, agent ends the call, post-call fields filled (Outcome, Pathway, Booking Agreed, Reports Available, Comorbidities), lead moved to Appointment Payment with attributes filled by the outcome analysis. Use that as the bar for any new client agent.
