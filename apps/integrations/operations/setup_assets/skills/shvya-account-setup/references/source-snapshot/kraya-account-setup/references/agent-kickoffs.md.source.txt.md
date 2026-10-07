# Agent kickoff prompts (verbatim, from Agent Hub)

These are the first user turns Agent Hub fires at each of the five ops agents once the client context has been injected. They tell the agent to work from the context instead of re-asking, and they carry the formatting rules that the runtime's WhatsApp rendering needs. When you run the equivalent phase yourself, apply the same instructions, with one deliberate difference: the "NO emojis" lines below are Agent Hub's blanket product default. The cloud agent follows the emoji policy in `content-rules.md` §1 instead: none unless the client asked for them, and when they did, the emoji levels and placement rules already written into the Sequence Writer and AI Qualification Builder prompts apply. The same applies to `{{lead_first_name}}` in these prompts: Kraya substitutes single-brace `{lead_first_name}` and renders the double-brace form as `{Rahul}`, so write single braces when you push (see `content-rules.md` §1).

Source: `agent-hub/src/app/(dashboard)/ops/[id]/ops-client-detail.tsx` (`KICKOFF_PROMPTS`) and `src/lib/ops/qualification-kickoff.ts`.

## 1. client-profile-builder

```
Build the complete Client Profile for this client using the auto-injected client context above (call transcripts, sales notes, win details, ops notes). Output the full structured profile in your standard format. Do not ask me for info that's already in the context.
```

## 2. ai-qualification-builder

The base prompt below, followed by the `<client_preferences>` block from `content-rules.md` §6 when the client chose any non-default preference.

```
Using the Client Profile and other context auto-injected above, build the complete AI qualification flow: product / service list, qualification questions per product, relevant FAQs, formatted for Kraya. Do not ask me to re-provide website URL, company name, or industry, they're in the context above. Only ask if something critical is genuinely missing.

FORMATTING RULES for every message the AI bot will send to leads (welcome message, qualification question prompts, acknowledgement message). The bot's outputs are showing up as walls of text on WhatsApp; the rules below fix that.

1. **Single thought per paragraph**. Blank line between paragraphs. Never run a greeting, a description, and a question together as one paragraph.
2. **Welcome message structure** (3 short paragraphs):
   - Paragraph 1: short welcome line with the brand in *bold*, e.g. "Welcome to *Naulakha Naturals*."
   - Paragraph 2: one-line value description, e.g. "Natural and herbal personal care and home care products, crafted with over 127 years of legacy."
   - Paragraph 3: the first qualification question, asked clearly. Options on separate bulleted lines, not crammed inline.
3. **Question layout**. Do NOT decide the question FORMAT here — follow whatever the client's own inputs call for, per your main instructions, the brainstorming call and transcript, and the Question wording preference. These layout rules apply whichever format you land on:
   - Open with a short acknowledgement of the previous answer when applicable, e.g. "Great, personal care it is."
   - Then a blank line.
   - Then the actual question on its own line, ending in a question mark.
   - Then a blank line.
   - Then the answer options as a bulleted list with `-` markers OR `*bold options*`, one per line. Never cram options inline like "(a) Personal Care, (b) Home Care".
4. **Bold key terms** with WhatsApp `*asterisks*`: product names, brand names, category labels, prices. Don't use markdown headers, don't use underscores for emphasis.
5. **NO emojis** anywhere in the qualification messages. Not in greetings, not as headers, not in confirmations. The structure (paragraphs, bold, bullets) does the work emojis would have done.
6. **No em dashes (—) or en dashes (–)**. Use periods or commas, or split into two sentences. Regular hyphens (-) are fine in compound words and bullet markers.
7. **Acknowledgement message** (final message after qualification completes): 2-3 short paragraphs. Confirm what was captured, set expectation for next contact, sign-off.

EXAMPLE of correctly-formatted qualification messages. This is the visual target for LAYOUT ONLY: paragraph breaks, bolding, one option per line. It is not the literal copy, and it is not a format decision. The tags below happen to show one client's format; whether to wrap questions in <question_content> at all is decided by your main instructions and the Question wording preference. Copy the layout from this example, never the tag choice.

<welcome_message>
Welcome to *Naulakha Naturals*.

Natural and herbal personal care and home care products, crafted with over 127 years of legacy.

Quick question to get you to the right team. Are you looking to buy for:
- Personal use
- Business or bulk purchase

Reply with the option that fits.
</welcome_message>

<question_content>
Great, personal use.

Which category are you interested in?

- *Personal Care* (skin / hair / face)
- *Home Care* (laundry / floor cleaners / dishwash)
</question_content>

<question_content>
Got it, *Personal Care* it is.

Which product or concern are you looking for?

For example: shampoo or conditioner, face wash or cream, handwash, body lotion or soap.
</question_content>

<acknowledgement_message>
Thanks {{lead_first_name}}, all noted.

Our team will reach out shortly with the right products and pricing for *Personal Care*.

For any quick questions in the meantime, just reply here.
</acknowledgement_message>

Match this spacing and structure exactly for every message you generate. The same content WITHOUT line breaks renders on WhatsApp as one cluttered paragraph, which is the failure we're fixing.
```

## 3. client-followup-sequence-outline-generator

```
Design the WhatsApp follow-up sequence strategy for this client.

Before you write a single sequence, do the BUSINESS AUDIT and put it at the top of your response, in this exact format:

## Business audit
- Brand: <name from CLIENT INFO>
- Business type: <B2B | B2C | both, pick one based on the Client Profile's audience description>
- Actual products / services: <2-5 concrete items from the profile, not categories>
- Target customer: <one sentence, who actually buys, where, what they care about>
- Sales cycle: <what real leads do from first message to purchase, pulled from call transcripts / win details / ops notes>
- Drop-off points: <where leads typically go cold, also from transcripts / notes>
- Conversation language: <English | Hinglish | Hindi | Punjabi | Tamil | other. Infer from the call transcripts and sales notes. If the client's own sales calls happen in Punjabi or Hinglish, the sequences must be in the same language. Do not default to English.>
- Reply convention: <3 to 5 single-word reply keywords the sequences will use as CTAs, e.g. INFO, BOOK, SHOW, START, STOP. Each keyword should map to a single concrete next step in the sales cycle.>

If any of the audit fields are missing from the context, write `(missing, needs ops to fill)` rather than inventing. Don't make up a B2C consumer flow for a B2B brand. Don't assume the playbook's default audience.

THEN design the sequences. Pick the count that matches this business's actual flow, typically 4-6:
- Hot-lead sequence for high-intent leads from the post-discovery / post-trial stage. Typically 5-6 messages, daily cadence, faster close-oriented.
- Nurture sequence for warm-but-not-decided leads. Typically 5-7 messages, 5-7 day cadence.
- DNP / re-engagement sequence for cold or unresponsive leads. Typically 4-5 messages, 5-7 day cadence.
- Post-purchase / onboarding sequence for the won stage. Optional but valuable for referral / retention.
- Win-back or seasonal sequence. Optional, depends on the business.

For each sequence, output:
- **Sequence name**: 3-5 words, names the actual moment in the customer journey for THIS business.
- **When it fires**: which stage or trigger from the lead's behaviour.
- **Who it talks to**: one specific customer type from the audit.
- **Number of messages + cadence**: e.g. "5 messages over 7 days: 5m, day 1, day 3, day 5, day 7".
- **Per-message plan** as a numbered list. For each message slot give all four of:
  1. **Theme title**: 3-7 words describing what this message does.
  2. **Purpose label** from this exact set: `welcome` / `value` / `proof` / `objection` / `urgency` / `last-chance` / `reactivate` / `referral` / `identity` / `visualization` / `doubt-removal` / `fomo`. NO TWO MESSAGES IN THE SAME SEQUENCE MAY SHARE A LABEL.
  3. **CTA keyword**: the single uppercase word the lead types back if interested. Must match one from the reply convention.
  4. **Attachment**: a PDF / image / video / voice note / link this message should include, or "none".

Strategy and structure only, no message copy yet. The Sequence Writer drafts the copy.

The INDUSTRY REFERENCE in the context is descriptive shape only. Use it to sanity-check that you have roughly the right sequence count and cadence for this industry. Do NOT copy its scenarios if they don't match the audit above.
```

## 4. sequence-writer

```
Write every WhatsApp message for all sequences in the approved Sequence Outline above. These conventions are pulled from our best-performing client sequences (NUWAVE, Mister Hair, Really Agritech). Follow them exactly.

STEP 1: Repeat the business audit at the top of your response. Pull it verbatim from the Sequence Outline's audit block. One line. If a message you write doesn't fit this audit, rewrite it.

STEP 2: For each sequence in the outline, draft every message using the EXACT format below.

MESSAGE FORMAT (mandatory):
[purpose-label] Theme title in 3-7 words

Hi {{lead_first_name}}, opening line.   <-- first message of the sequence only

Body paragraph one. One or two short sentences. Use *bold with asterisks* on product names, prices, brand names, and key terms.

Body paragraph two if needed. Single thought per paragraph. Never more than 3 lines together.

(Optional) Bulleted list with concrete items from the Client Profile:
- One specific product, service, benefit, or fact
- Another specific item

(Optional, when purpose is proof) Real customer quote, attributed by name and city:
"Quote text exactly as the customer would have said it." Customer Name, City.

(Optional, for B2B / high-ticket) Phone-number CTA paragraph:
Call us at +91 80000 00000.

Reply *KEYWORD* to <one specific outcome>.

DETAILED RULES:
1. Purpose label: every message starts with the bracketed label assigned by the outline, on the first line, followed by the theme title.
2. Theme title: 3-7 words after the purpose label.
3. NO emojis anywhere in the message. If you feel like adding an emoji, instead add a more specific word.
4. WhatsApp markdown: *asterisks* for bold. No markdown headers, no underscores. Bullet lists use - markers.
5. Bulleted lists: 3-5 concrete items pulled from the Client Profile, short noun phrases.
6. Real customer quotes: when the purpose is proof, attribute by NAME + CITY (or NAME + ROLE for B2B). If no real customer is in the context, do not invent one; drop the proof slot and note it.
7. CTA convention: every message ends with `Reply *KEYWORD* to <outcome>.` on its own paragraph. One uppercase keyword from the outline's reply convention.
8. Single thought per paragraph, blank line between paragraphs.
9. Client-specific grounding: every message references at least one specific fact about this client.
10. Compliance footer: marketing nurture / re-engagement sequences add `Reply *STOP* to unsubscribe.` as a separate last paragraph. Hot-lead, post-purchase and onboarding sequences don't need it.
11. Sequence length and cadence: follow the outline. Don't pad.
12. Phone-number CTA: B2B, agritech, high-ticket, dealer / distributor sequences ALSO include `Call us at <number>.` above the reply CTA, with the exact number from the Client Profile.
13. Greeting: only the first message of a sequence opens with `Hi {{lead_first_name}},`.

STYLE RULES (strict):
1. No em dashes or en dashes. Use periods or commas, or split into two sentences.
2. Always {{lead_first_name}}. Never {{lead_name}}.
3. Sound like a real person typing on a phone. No "we are excited", "discover", "transform", "unlock", "elevate", "embark".
4. Language match: write in the language the audit says, in Roman script the way Indians text on WhatsApp. Match register ("Tusi" for Punjabi, "Aap" for professional Hindi).
5. Message length: 60-150 words. Never under 30, never over 200.

The INDUSTRY REFERENCE in your context is descriptive shape only. Do NOT copy phrases from it.
```

Note: Agent Hub's build step then re-normalises the writer's output before it reaches Kraya: the bracketed purpose label is stripped from `content`, emoji are removed, dashes become commas, and any message that still contains a placeholder is dropped (see `content-rules.md` §1).

## 5. account-setup-builder

```
Using all prior agent outputs auto-injected above (Client Profile, AI Qualification, Sequence Outline, Sequence Writer), generate the complete Kraya platform configuration: pipeline stages, smart triggers, automation rules, and settings — ready to paste into the Kraya admin to deploy this account.
```
