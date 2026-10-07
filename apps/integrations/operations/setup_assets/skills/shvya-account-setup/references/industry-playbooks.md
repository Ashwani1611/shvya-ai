# Reference contents

- Complete industry authoring playbooks
- healthcare
- stages
- attributes
- sequences
- orgInfo
- faqs
- quickReplies
- rules
- realestate
- stages
- attributes
- sequences
- orgInfo
- faqs
- quickReplies
- rules
- fitness
- stages
- attributes
- sequences
- orgInfo
- faqs
- quickReplies
- rules
- travel
- stages
- attributes
- sequences
- orgInfo
- faqs
- quickReplies
- rules
- b2b
- stages
- attributes
- sequences
- orgInfo
- faqs
- quickReplies
- rules
- agencies
- stages
- attributes
- sequences
- orgInfo
- faqs
- quickReplies
- rules
- education
- stages
- attributes
- sequences
- orgInfo
- faqs
- quickReplies
- rules
- retail
- stages
- attributes
- sequences
- orgInfo
- faqs
- quickReplies
- rules
- manufacturing
- stages
- attributes
- sequences
- orgInfo
- faqs
- quickReplies
- rules

# Complete industry authoring playbooks

> SHVYA ADAPTATION: Read runtime-contract.md before using this full reference. Preserve this method's detail, but compile its output into native SHVYA schemas. Kraya field names, API routes, database queries, token scripts and past performance claims are historical context, not live capabilities. Sample businesses, prices and policies remain examples. Human handoff/opt-out takes precedence over continued qualification; use verified double-brace CRM tokens and provider bindings. This reference does not authorize sending, enrollment or activation.

Choose healthcare, realestate, fitness, travel, b2b, agencies, education, retail or manufacturing from verified business activity; preserve digits in b2b. For a mixed business record the dominant funnel and adapt relevant secondary chunks. No classifier API or code is required. Each industry contains all seven original guidance dimensions: stages, attributes, sequences, orgInfo, faqs, quickReplies and rules. Narrative source labels must be translated through runtime-contract.md; sample assumptions require client evidence.

## healthcare

### stages

Pipeline: "Appointments", type: Leads
Stages (in order):
1. New Lead. AI starts here. Set as default stage.
2. Qualified. Auto-shifted once concern, appointment timeline, and name/contact are captured.
3. Appointment Booked. Staff moves manually after reception confirms the slot.
4. Visited. Staff moves manually after the patient attends.
5. Active Patient. Mark as Won stage. Treatment started / paid.
6. No Show. Staff moves here when a booked patient does not attend. Drives the No-Show Recovery sequence.
7. No Response. Lead went cold with no reply.
Note: Multispecialty hospitals route concern to a per-department stage. If the clinic has 3+ doctors, create per-doctor pipelines matching the doctor's name for Round Robin.

### attributes

1. Treatment / Department. Dropdown. Values = the client's real menu (dental: Pain, Cleaning, Braces, Whitening; derma: Skin, Hair, Laser, Botox, PRP; multispecialty: GP, Ortho, Neuro, Gastro, Gynae, Paeds). Description: "concern or department the patient is seeking".
2. Visit Status. Dropdown. Values: New Patient, Follow-Up, Recurring. AI reads this to personalise the follow-up.
3. Preferred Slot. Short text. The appointment date/time the patient wants. Description: "patient's preferred day and time; reception confirms it".
4. City. Dropdown. Values: the client's branch cities plus Online. Description: "routes online vs in-person and picks the branch".
5. Doctor Preference. Dropdown. Values: the client's doctor names plus No Preference. Description: "used for Round Robin and per-doctor pipelines".
Adapt: aesthetics/hair clinics add Hair Loss Grade, Recommended Grafts, Booking Amount, EMI Amount. Meta-ad clinics add wa_ref_* / meta_lead_form tracking attributes.
Important: Add a description to every attribute. AI uses descriptions for extraction accuracy.

### sequences

SHAPE REFERENCE ONLY. These are typical sequence shapes for a clinic. The wording below is illustrative; the writer must rebuild content from the actual Client Profile (clinic name, treatments, location, consult fee). Copy stays benefits + testimonials. Never state treatment cost, never use urgency or scarcity.

Sequence 1: DNP Recovery (4 messages over 5 days)
  Trigger: rep called but the lead did not pick up, or New Lead with no reply for 4+ hours.
  Day 1: short, no-pressure re-attempt about the enquiry.
  Day 3: why patients trust the clinic (credentials, experience). Attach: clinic brochure.
  Day 3: patient testimonials. Attach: testimonials / awareness video.
  Day 5: common questions answered (timings, online vs in-person, what happens after enquiry).
  Stop: lead replies, or moves to Qualified / Appointment Booked.

Sequence 2: 7-Day Nurturing (4 messages)
  Trigger: lead moved to New Lead but not yet qualified.
  Day 1: warm welcome + one-line service summary tailored to the concern.
  Day 2: patient testimonials. Attach: testimonials.
  Day 3: what makes the clinic different (tech, personalised plans, expert team).
  Day 4: booking the consultation is the first step. Attach: booking (Calendly / Google Form) link.
  Stop: lead replies, books, or reception confirms a slot.

Sequence 3: No-Show Recovery (3 messages over 3 days)
  Trigger: lead moved to No Show stage (booked but did not attend).
  Day 1: warm "we missed you, shall we reschedule?" with no blame.
  Day 1: offer the next available dates. Attach: reschedule / booking link.
  Day 3: gentle final reschedule nudge + testimonials.
  Stop: lead replies or moves back to Appointment Booked.

Sequence 4: Slot Reminder (2 messages)
  Trigger: lead in Appointment Booked stage, appointment approaching.
  Day 0: reminder of the confirmed date/time + branch. Attach: Maps pin.
  Day 0 (day before): quick prep note + reception contact.
  Stop: patient attends (Visited) or reschedules.

Sequence 5: Call-Me (1 message)
  Trigger: lead asked for a callback or to speak to the doctor.
  Day 0: acknowledge, "our reception will call you to confirm your appointment".
  Stop: reception calls or lead moves to Qualified.

### orgInfo

Bot persona: "Meera", a warm clinic care coordinator (illustrative name). Cosmetic/derma clinics may speak in the doctor's voice ("Dr {name}'s team would like to understand your concern").
Opener: "Hi, welcome to {clinic}" (no emoji) + one-line service summary + concern question.
Qualification questions (in order):
  1. (optional) preferred language.
  2. Concern / treatment / department. Offer the menu; a condition mentioned upfront auto-maps and skips this.
  3. Concern sub-detail: how long / severity / for whom.
  4. Prior treatment or reports.
  5. Location, or online vs in-person.
  6. Name (add age/gender only when clinically needed: fertility, hair, dental).
  7. Contact number.
  8. Preferred appointment date and time.
Handoff: once concern + timeline + name/contact are captured, mark Qualified, send one acknowledgement ("our team will call to confirm your appointment"), then route to human reception. Escalate immediately if the lead asks to book, speak to the doctor, or requests a call. Multispecialty hospitals forward every conversation to a human.
Bump-up count: 2 (min 60 min gap between bump-ups).
Business hours: match the clinic's OPD timings (e.g. 9 AM to 8 PM IST).
Hard rules:
  - Never diagnose, interpret symptoms, give medical advice, or suggest prescriptions ("only a qualified doctor can...").
  - Never quote treatment costs. A fixed consultation fee is the only price the bot may state.
  - Never guarantee outcomes ("permanent cure", "100% results").
  - On red-flag or emergency symptoms, stop the flow and direct the patient to emergency numbers.
  - The AI never confirms a slot itself. Reception calls to confirm.

### faqs

Manual FAQs to add beyond CSV import:
- How to book an appointment, OPD timings, and walk-in policy.
- Online vs in-person consultation.
- Consultation fee, EMI options, "why is treatment more expensive", refunds. Never state treatment cost via the bot.
- Billing, insurance, TPA, and cashless (hospitals).
- Doctor and clinic credentials, location, branches, and directions.
- Prep and aftercare, "what happens after I enquire", and emergency / 24x7 numbers (hospitals).

### quickReplies

Group by clinic workflow: Treatments (per-treatment info replies), Pre-Counselling (what to bring, how the consult works), After-Counselling (prescription/next-step recap, payment details), Support (delayed report, reschedule, directions with Maps pin), plus the standard Follow-Up, First Response, and Reminder groups. Include a 'Call Not Picked' reply (reception tried calling, ask for a good time) and a consultation-fee reply (the only price the bot may state).

### rules

Trigger 1: New Lead Nurturing.
  When: lead moved to New Lead stage.
  Then: initiate the 7-Day Nurturing sequence.
  Delay: immediate.
Trigger 2: No-Show Recovery.
  When: lead moved to No Show stage.
  Then: initiate the No-Show Recovery sequence.
  Delay: +2 hours.
Trigger 3: Appointment Confirmation.
  When: lead moved to Appointment Booked stage.
  Then: set a call reminder for reception to confirm + send the reminder template.
  Delay: immediate; reminder 1 day before the slot.
Trigger 4: Reply Stops Sequence.
  When: keyword_detected (any inbound reply or STOP).
  Then: stop_sequence.
  Delay: immediate.
Trigger 5: Emergency Escalation (hospitals / mental health).
  When: keyword_detected (red-flag or emergency terms).
  Then: stop_sequence, assign to a human, and share emergency numbers.
  Delay: immediate.
Lead-source note: wire Meta Lead Ads (click-to-WhatsApp) by default; also Google Sheets and JustDial. Meta ads carry wa_ref_* tracking attributes.

## realestate

### stages

Pipeline: "Property Leads", type: Leads
Stages (in order):
1. New Lead. AI starts here. Set as default stage.
2. Qualified. Auto-shifted once the 5 capture fields (intent, property type, location, budget, timeline) are captured.
3. Site Visit Scheduled. AI or staff moves after a visit date and time are booked.
4. Visit Done. Staff moves manually after the lead attends the site visit.
5. Negotiation. Staff moves during price and booking discussion.
6. Lead Won (Booked). Mark as Won stage. Booking confirmed or token paid.
7. No Response. Sequence finished with no reply.
Note: Site Visit Scheduled to Visit Done is the RE conversion spine, keep it. Add a locked "Ineligible / Low Budget" stage if a budget floor is enforced. Add In Conversation, Nurturing, and Lead Lost as needed. Round Robin by locality or rep only if 3+ consultants; developers route to per-project buckets.

### attributes

1. Intent. Dropdown. Values: Buy, Rent, Sell, Invest. Description: "what the lead wants to do" (routes the whole flow).
2. Property Type. Dropdown. Values: 1BHK, 2BHK, 3BHK, 4BHK, Plot, Commercial. Description: "configuration the lead is looking for". Set values to the client's real inventory.
3. Budget Range. Dropdown. Values: Under 50L, 50L-1Cr, 1Cr-2Cr, 2Cr+. Description: "budget band, never a final price". Scale bands to their ticket (luxury 2Cr+/5Cr+, rental in Rs/month).
4. Decision Timeline. Dropdown. Values: Immediate, 1-3 months, 3-6 months, 6+ months. Description: "how soon they want to close". AI reads this to tag urgency.
5. Buyer Type. Dropdown. Values: End User, Investor, NRI. Description: "buyer profile". Personalises the nurture.
Also add Location/Area (dropdown of the client's real localities) and Site Visit Date (date type, "preferred date and time for a site visit").
Important: Add a description to every attribute. AI uses descriptions for extraction accuracy.

### sequences

SHAPE REFERENCE ONLY. These are typical sequence shapes for a real estate team. The wording below is illustrative; the writer must rebuild content from the actual Client Profile (brand name, years in market, projects, localities, ticket size). Cadence 0, +1d, +2d; about 4 messages each; mode extension. Nurture leans on credibility and local-market education, not discounts.

Sequence 1: DNP Follow-up (4 messages over 3 days)
  Trigger: Lead sits in "New Lead" with no reply, or a rep called and the lead did not pick up.
  Day 0: short re-attempt referencing their enquiry (config + location).
  Day 1: value nudge, offer to share matching options.
  Day 2: light "shall I keep this open or close it" nudge.
  Attach: property cards for matching inventory.
  Stop: lead replies or books a visit.

Sequence 2: Warm-Lead Visit Nurture (4 messages over 3 days)
  Trigger: qualified lead who has not booked a site visit.
  Day 0: acknowledge the requirement, lead with credibility (years in market, verified inventory count).
  Day 1: local-market education (why the locality or project is a strong bet).
  Day 2: address the "let me think" objection with a practical reason to visit now.
  Attach: project brochure and a Maps link.
  Stop: visit booked or lead opts out.

Sequence 3: FOMO Reminder (3 messages over 3 days)
  Trigger: warm lead near a real project launch or price-revision date.
  Day 0: heads-up on the upcoming launch or price change.
  Day 1: limited-inventory nudge tied to that date.
  Day 2: last call before the date.
  Attach: launch brochure or floor plan.
  Stop: books a visit or the date passes.

Sequence 4: Call-Me (2 messages)
  Trigger: lead asks to be called or shares a preferred slot.
  Day 0: confirm the callback slot and set expectations.
  Day 1: short reminder before the scheduled call.
  Attach: booking link.
  Stop: call connects or lead reschedules.

### orgInfo

Bot persona: "Vaani", a property advisor. Position her explicitly as an advisor, not a broker and not a telecaller.
Opener: "Welcome to {org_name}" + one line on what they do + the first question.
Qualification questions (in order):
  1. Intent (buy / rent / sell / invest).
  2. Property type or configuration.
  3. Location or project of interest.
  4. Budget range (capture the band, never a final price).
  5. Purpose (self-use or investment).
  6. Decision timeline.
  7. Finance (loan or self-fund), if relevant.
  8. Site-visit availability (preferred date and time).
  9. Name and contact, only if not already in CRM.
Handoff: once the 5-tuple (intent, type, location, budget, timeline) is captured, mark Qualified, send an acknowledgement ("our expert will review and connect shortly"), route to a human consultant, and book a site visit (booking link or capture date+time to Site Visit Scheduled). AI stops after human handover. Auto-tag Hot (clear need + budget + under 6 months + visit intent) / Warm / Cold. Below the budget floor, move to Ineligible with a polite exit message. Route by segment or location to the right rep (residential vs commercial; area rep).
Bump-up count: 2 (min 60 min gap between bump-ups).
Business hours: 9 AM to 8 PM IST.
Hard rules:
  Never quote an exact or final price and never negotiate; capture the budget range, the team confirms pricing.
  Never promise ROI, appreciation, rental yield, possession dates, or loan approval.
  For a portal or meta ad lead, acknowledge the listing they enquired about. For rentals, swap budget to Rs/month and capture move-in date.

### faqs

Manual FAQs to add beyond CSV import:
- Available properties or projects for the enquired config and location.
- How to schedule a site visit or virtual tour (by appointment, weekdays and weekends).
- Booking and purchase steps (KYC, agreement, token) plus pricing structure and payment schedule (construction-linked / installments).
- Legal and approvals (RERA/JDA, title documents, whether an NRI can buy).
- Loan assistance, possession updates, and post-sale support.
- Credibility (who runs it, years in market, Google rating, why pricing is higher).
Sendable files: project brochures/PDFs per project (the dominant sendable), floor plans/layouts, a Maps link, property cards for matching inventory, and the booking link.

### quickReplies

Seed: a pricing-deferral reply (budget band captured, team confirms pricing), Inventory/Availability per project, Location/Directions with the Maps link, Site-visit slot booking + confirmation, a 'when the client says no' objection reply (park warmly, door open), and a post-visit follow-up. Titles are situation labels the rep can find in two seconds.

### rules

Trigger 1: DNP kickoff.
  When: lead moved to "New Lead".
  Then: initiate the DNP Follow-up sequence.
  Delay: immediate.
Trigger 2: Confirm site visit.
  When: lead moved to "Site Visit Scheduled".
  Then: set a call reminder for the rep to confirm the visit (RE uses call-reminders heavily).
  Delay: same day, before the slot.
Trigger 3: Stop on reply.
  When: an inbound keyword is detected (a reply, "call me", or "booked").
  Then: stop the active sequence.
  Delay: immediate.
Trigger 4: Mark unresponsive.
  When: a sequence completes with no reply.
  Then: move the lead to "No Response".
  Delay: immediate.
Trigger 5: Budget-floor exit.
  When: captured budget is below the client's floor.
  Then: move the lead to "Ineligible / Low Budget" and send a polite exit message.
  Delay: immediate.
Lead sources to wire: Meta Lead Ads by default; add Housing.com or 99acres portal connectors if they list there, and Neodove or MyOperator telephony for call-heavy teams; Google Sheets for manual imports.

## fitness

### stages

Pipeline: "Trials", type: Leads
Stages (in order):
1. New Lead. AI starts here. Set as default stage.
2. Qualified. Auto-shifted once a goal + a booked conversion are captured.
3. In Conversation. Lead is actively replying but not yet booked.
4. Trial Booked. Free trial / demo class / gym visit scheduled.
5. Trial Done. Staff moves after the lead attends the trial.
6. Converted (Member). Mark as Won stage. Membership paid / enrolled.
7. No Response. Ghosted leads routed here for DNP nurture.
Also add: Nurturing, Lead Lost.
Note: 3+ trainers, create per-trainer pipelines matching the trainer's name for Round Robin. Online-coaching sub, rename the spine to Discovery Call Booked -> Call Done -> Enrolled. Studio (yoga / zumba), rename to Demo Class Booked -> Attended -> Joined.

### attributes

1. Fitness Goal. Dropdown. Values: Weight Loss, Muscle Gain, General Fitness, Weight Gain, Lifestyle-Disease Management. Description: "the primary result the lead trains for".
2. Program / Service Interest. Dropdown. Values: Gym Membership, Personal Training, Group Classes, Online Coaching, Trial / Pay-per-session. Description: "which offering the lead wants".
3. Membership Interest. Dropdown. Values: Trial, Monthly, Quarterly, Annual, PT. Description: "the plan tier the lead leans toward".
4. Fitness Level. Dropdown. Values: Beginner, Intermediate, Advanced. Description: "current training experience; AI uses it to pitch beginner-friendliness".
5. Preferred Slot. Text. Description: "day + time the lead can come in for the free trial, e.g. Weekday evening / Weekend morning".
Important: Add a description to every attribute. Multi-branch gyms add City / Branch (multi-location); Meta-ad accounts add wa_ref_* / meta_lead_form tracking. AI uses descriptions for extraction accuracy.

### sequences

SHAPE REFERENCE ONLY. These are typical sequence shapes for a fitness brand. The wording below is illustrative; the writer must rebuild content from the actual Client Profile (brand name, programs, branches, pricing) and imitate a warm, motivational voice.

Sequence 1: Nurturing (transformation + motivation) (3 messages over 3 days)
  Trigger: Lead moved to "New Lead".
  Day 1: warm re-connect, "taking the first step is the hardest" social proof.
  Day 2: check in on the options discussed, "many members start their transformation this month".
  Day 3: ready-to-start nudge, benefit-led (better energy, confidence, results within weeks).
  Attach: transformation photos / testimonials.
  Stop: lead replies or books a trial.

Sequence 2: DNP Follow-up (3 messages over 3 days)
  Trigger: Lead in "New Lead" 4+ hours with no reply, or moved to "No Response".
  Day 1: short re-attempt about the enquiry.
  Day 2: offer to answer questions on plans / timings.
  Day 3: final nudge with the booking link.
  Attach: booking / calendar link.
  Stop: any reply.

Sequence 3: Discount / Offer (membership) (3 messages over 3 days)
  Trigger: Lead in "Qualified" but not converted.
  Day 1: limited-time joining offer / fee waiver.
  Day 2: FOMO, slots or batch filling up.
  Day 3: final-day reminder.
  Attach: membership / price list.
  Stop: lead converts or opts out.

Sequence 4: FOMO Reminder (2 messages over 2 days)
  Trigger: Lead in "Trial Booked" but not attended.
  Day 1: confirm slot + what to wear / bring.
  Day 2: gentle reminder + reschedule option.
  Attach: Google Maps location pin.
  Stop: lead attends or reschedules.
Cadence note: mode extension; keep the tone warm, benefit + social-proof led, never spammy. No emoji.

### orgInfo

Bot persona: "Ria", a warm, motivating fitness / membership advisor with personal-trainer energy, never pushy (gyms often say "automated assistant" then hand to a Fitness Consultant).
Qualification questions (in order):
  1. Fitness goal / which service they want.
  2. Location / branch (multi-branch gyms only).
  3. Fitness level / prior training experience.
  4. Health / injury screen (coaching + studios).
  5. Membership / plan interest.
  6. Book a free trial (day + slot).
Bump-up count: 2 (min 60 min gap between bump-ups).
Business hours: 6 AM to 10 PM IST.
Handoff: mark Qualified only once goal + a booked conversion (free trial / demo / visit / call, or a callback time) are captured. Canonical ack: "Thanks for sharing these details. I'll forward them to the team so they can connect with you to take things forward." Then hand to a named Fitness Consultant. Online-coaching sub: share a calendar booking link as the terminal action.
Emoji: never use emoji, there is no fitness exception; carry the motivational energy in the wording alone. Still one question at a time.
Hard rules: never guarantee results or transformation timelines. No medical advice, defer to a professional. For a free-trial date, only accept a date within the next 30 days. Mirror Hinglish / English. Route events, out-of-scope, and complex requests straight to a human. Apply eligibility gates (age gate for kids programs; health screen for coaching).

### faqs

Manual FAQs to add beyond CSV import:
- Membership pricing / plans / EMI / joining fee
- Timings & schedule, and online-vs-in-person options
- Programs & classes offered, and how to choose the right one
- Enrollment / onboarding: what happens after I enquire, how the free trial works
- Eligibility & prerequisites: age, prior experience, branch coverage
- Trainer / founder credibility (certifications, experience, results), beginner-friendliness, what to wear or bring
Data hygiene: several gyms carry a mis-seeded generic or edtech ("students / courses / EMI") FAQ block; delete it and replace with real fitness FAQs. Sendable files: booking / calendar link (dominant terminal artifact), Google Maps pin, price list, class schedule, transformation photos.

### quickReplies

Seed: Membership pricing (real plan prices), Trial-booking confirmation (slot + what to wear/bring), Class timings, Location/directions with the Maps pin, a Discount opener (only if a real offer exists), plus the standard Follow-Up / Initial Response / Reminder groups and a 'Call Not Picked' reply.

### rules

Trigger 1: New Lead nurture.
  When: lead_moved_to_stage("New Lead").
  Then: initiate_sequence(Nurturing).
  Delay: immediate.
Trigger 2: DNP on ghost.
  When: lead_moved_to_stage("No Response"), or no_response_from_lead for 24h.
  Then: initiate_sequence(DNP Follow-up).
  Delay: on entry / after 24h.
Trigger 3: Confirm the trial.
  When: lead_moved_to_stage("Trial Booked").
  Then: set_call_reminder to confirm the trial + send the lead a reminder.
  Delay: immediate, remind 1 day before the slot.
Trigger 4: Stop on reply.
  When: keyword_detected (or lead replies).
  Then: stop_sequence.
  Delay: immediate.
Trigger 5: Win handoff.
  When: lead_moved_to_stage("Converted (Member)").
  Then: stop all sequences + notify the Fitness Consultant.
  Delay: immediate.
Lead-source note: wire Meta Lead Ads (click-to-WhatsApp, hence wa_ref_* attributes) + JustDial by default; Google Sheets import is also common.

## travel

### stages

Pipeline: "Bookings", type: Leads (type mix skews sales).
Stages (in order):
1. New Lead. AI starts here. Set as default stage.
2. Qualified. Auto-shifted after AI captures the core requirement set.
3. Quotation Sent. Staff moves manually after sending the itinerary + quote. This is the Travel spine.
4. Booking Confirmed. Mark as Won stage. Advance/deposit paid.
5. Human Intervention. Staff-only holding stage; AI stops qualifying once a lead lands here.
Plus holding stages: In Conversation, Nurturing, No Response, Lead Lost.
Note: Travel teams usually run 3+ agents, so turn on Round Robin (highest adoption of any ICP) and assign per travel agent. If they sell both, split into Domestic and International pipelines.

### attributes

1. Trip Type. Dropdown. Values: Honeymoon, Family, Corporate, Solo / Friends, Pilgrimage. Description: "purpose or format of the trip the lead wants".
2. Trip Duration. Dropdown. Values: 2-3 Nights, 4-6 Nights, 7-9 Nights, 10+ Nights. Description: "how many nights/days the trip runs". Stays/resorts: swap for check-in/check-out dates.
3. Budget Tier. Dropdown. Values: Economy, Standard, Premium, Luxury. Description: "per-person budget band the AI reads to pitch the right package".
4. Hotel Category. Dropdown. Values: 3 Star, 4 Star, 5 Star, Villa. Description: "accommodation level the traveller expects".
5. Passport Status. Dropdown. Values: Ready, Renewing, Not Yet. Description: "visa/passport readiness". International operators only; drop it for domestic-only brands.
Also add as text: Destination, Travel Dates/Timeline, Traveller Count (adults+children), Departure City, campaign_name (ad-campaign attribution). Fixed-departure brands replace free Destination with their trip/batch list.
Important: Add a description to every attribute. AI uses descriptions for extraction accuracy.

### sequences

SHAPE REFERENCE ONLY. These are typical sequence shapes for a travel operator. The wording below is illustrative; the writer must rebuild content from the actual Client Profile (brand name, destinations, trip types, pricing).

Sequence 1: DNP Follow-up (3 messages over 3 days)
  Trigger: Lead in "New Lead" for 4+ hours with no reply, or agent called but lead didn't pick up.
  Day 0: short re-attempt referencing their specific enquiry.
  Day +1d: re-share value, offer to draft a quick itinerary.
  Day +2d: last light nudge; invite them to reply with a good call time.
  Attach: none (keep it light).
  Stop condition: any reply, or move to Human Intervention.

Sequence 2: Per-Destination Follow-up (the Travel signature, 3 messages over 3 days)
  Trigger: Destination captured; lead moved to "Nurturing".
  Day 0: one evocative line picturing the destination (morning, afternoon, evening beats).
  Day +2d: "a trip here should feel like a movie, not a rushed checklist"; name 3-4 real spots.
  Day +3d: "which vibe are you after?": romantic honeymoon / luxury villa / adventure / peaceful family.
  Attach: the itinerary/package PDF for that destination.
  Stop condition: reply, booking, or opt-out. Build one of these per popular destination.

Sequence 3: FOMO + Discount Offer (2 messages over 2 days)
  Trigger: Lead engaged but stalled after quote (in "Quotation Sent").
  Day 0: seasonal FOMO (slots/season filling, price sensitivity).
  Day +2d: limited-time offer framed as "starting from" only.
  Attach: rate card / tariff image.
  Stop condition: reply, booking, or opt-out.

Sequence 4: Post-Booking "Booked" (2 messages)
  Trigger: Lead moved to "Booking Confirmed".
  Day 0: warm confirmation + what happens next.
  Day +2d: pre-trip checklist and 24x7 on-trip support contact.
  Attach: confirmation voucher / company profile.
  Stop condition: opt-out.

### orgInfo

Bot persona: "Riya", a warm travel expert / well-travelled friend. Use a concierge voice for stays/resorts.
Opener: "Welcome to {org_name}", one line, no emoji, then Q1 (service/trip-type or destination).
Qualification questions (in order):
  1. Service or trip format, if they sell more than one.
  2. Destination.
  3. Trip type / purpose.
  4. Travel dates / month.
  5. Number of travellers (plus child ages).
  6. Duration (nights/days).
  7. Accommodation level.
  8. Budget.
  9. Name + contact + preferred call-back time.
Handoff: capture the core set, mark Qualified, acknowledge ("our travel expert will review and share a customized itinerary + quote shortly"), then route to a human. Grade intent New to Engaged to Warm to Hot by number of core fields answered.
Escalate to the Human Intervention stage (stop qualifying) when the lead asks for a call/price/discount, is ready to book/pay, or is a large group / wedding / corporate-MICE.
Bump-up count: 2 (min 60 min gap between bump-ups).
Business hours: 9 AM to 8 PM IST.
Hard rules: never quote exact prices, only "starting from Rs X" if asked. Never guarantee availability, booking, refunds, or visa approval. Fixed-departure brands present a dated trip list and ask "do these dates work?"; they must never ask "when do you want to travel".

### faqs

Manual FAQs to add beyond CSV import:
- Package scope: domestic vs international, and which destinations are covered.
- Itinerary customization: what a custom quote includes and excludes.
- Booking & payment: advance/deposit (typically ~30%), EMI, GST.
- Cancellation & refund policy.
- Visa / flight / travel-insurance assistance and 24x7 on-trip support.
- Pricing ("starting from", why a package costs more) and company credibility.

### quickReplies

Seed: per-destination canned intros (a short itinerary teaser per popular destination, each with its itinerary PDF attached), a "starting-from" pricing reply, booking/advance-payment details, cancellation-policy answer, and a post-quote follow-up trio (recap, gentle nudge, final check-in).

### rules

Trigger 1: Start destination nurture.
  When: lead moved to the "Nurturing" stage.
  Then: initiate the matching per-destination follow-up sequence.
  Delay: immediate.
Trigger 2: Stop on reply.
  When: inbound reply / keyword detected on a lead in an active sequence.
  Then: stop_sequence.
  Delay: immediate.
Trigger 3: Escalate hot intent.
  When: keyword detected ("call me / book / price / discount").
  Then: move lead to the "Human Intervention" stage.
  Delay: immediate.
Trigger 4: Quote follow-through.
  When: lead moved to the "Quotation Sent" stage.
  Then: set_call_reminder for the agent to walk the lead through the itinerary and quote.
  Delay: same day.
Trigger 5: Park cold leads.
  When: sequence_completed with no reply.
  Then: move_to_stage "No Response".
  Delay: after the last message.
Lead-source note: about 38% connect a source. Wire Meta Lead Ads by default (destination campaigns map to the campaign_name attribute), plus Google Sheets and JustDial.

## b2b

### stages

Pipeline: "Leads", type: Leads
Stages (in order):
1. New Lead. AI starts here. Set as default stage.
2. Qualified. Auto-shifted once the service-branch questions and a contact preference are captured (deliberately low bar).
3. In Conversation. Staff moves manually while actively working the lead.
4. Good Lead. Staff marks high-intent leads worth expert time.
5. Lead Won. Mark as Won stage. Engagement signed / consult paid / demo closed.
6. No Response. Reached but never replied.
Also add: Nurturing, Lead Lost, Junk. Route job-seekers, vendors and spam to Junk and never qualify them.
Sub-segment swaps: SaaS use "Demo Booked, Demo Done, Closed Won". Compliance add "Docs Collected, Payment Link Sent, Paid (Won)". Immigration add "Counselling Booked, Counselled, Enrolled".

### attributes

1. Service Interested In. Dropdown. Values: Pvt Ltd Registration, LLP Registration, Section 8 / NGO, Trademark, GST Registration, FSSAI / Food License, ROC Compliance, Other. Description: "the specific service the lead is asking about". This is the router: it unlocks the per-service branch, so author it first from the client's real service menu.
2. Business / Entity Type. Dropdown. Values: Idea stage, Not yet registered, Proprietor, Partnership, Pvt Ltd, Company. Description: "the lead's current business form and stage".
3. City / Location. Text or dropdown. Description: "the lead's city, used to check serviceable area".
4. Decision Timeline. Dropdown. Values: Today / this week, This month, 1-3 months, Just exploring. Description: "how soon the lead wants to start".
5. Decision Maker. Dropdown. Values: Self, Partners, Family, Investor group, After expert's advice. Description: "who signs off on the decision".
Adapt per sub-segment: Immigration add visa / program type, destination country, qualification and experience. SaaS add team size / scale and use-case. Industrial add volume / investment.
Important: Add a description to every attribute. AI uses descriptions for extraction accuracy.

### sequences

SHAPE REFERENCE ONLY. These are typical sequence shapes for a B2B-services firm. The wording below is illustrative; the writer must rebuild content from the actual Client Profile (brand name, service menu, location, pricing posture). B2B buyers de-risk before they buy, so lead with credibility and process clarity, not discounts.

Sequence 1: Call-Me Recovery (3 messages over 2 days)
  Trigger: Rep called a New Lead but the lead did not pick up.
  Day 1: short note that we tried to call, ask for a good time.
  Day 2: re-offer a callback slot, restate the service they enquired about.
  Day 3: final nudge with a self-serve booking link. Attach: consultation booking link.
  Stop: lead replies or moves to In Conversation.

Sequence 2: DNP (4 messages over 5 days)
  Trigger: Lead in "New Lead" for 4+ hours with no reply.
  Day 1: re-open the enquiry, ask which service they need.
  Day 2: share how the consultation works and what happens after the enquiry.
  Day 4: light credibility touch (years in practice, cases handled). Attach: service brochure.
  Day 5: last attempt, offer to send the document checklist. Stop: any reply, or moves to Qualified.

Sequence 3: Credibility Nurturing (4 messages over 7 days)
  Trigger: Lead in "Nurturing".
  Beats: process clarity, then a relevant case example, then turnaround / timeline expectations, then a soft consult offer.
  Attach: per-service document checklist. Stop: lead books a consult or moves to Good Lead.

Sequence 4: Lead-Lost Revival (3 messages over 6 days)
  Trigger: Lead in "Lead Lost" for 30+ days.
  Beats: re-introduce, a new-service or seasonal reason to re-engage, final call-to-book. Stop: any reply.

Gate every attachment: never say "sent" unless the file actually delivered. whatsapp_api mode: use approved WABA templates if the client is on the Cloud API.

### orgInfo

Bot persona: "Aarav" (illustrative), an unnamed-style front-desk concierge who qualifies then hands to a named human expert ("our CA", "our consultant", "our counsellor"). Many B2B scripts keep the bot unnamed and carry an anti-naming rule: never name the founder, say "expert team".
Qualification questions (in order):
  1. Which service do you need? (numbered menu; free-text mapped to the closest option. This is the router.)
  2. Is the business an idea, not yet registered, or already operating? (+ entity type)
  3. Branch-specific scope for that service (e.g. company reg: which state, how many partners / directors).
  4. How soon do you want to start? (today / this week / just exploring)
  5. Any budget in mind? (optional; "not decided yet" is fine)
  6. Are you the decision-maker, or is it a partner / family / after-expert decision?
Bump-up count: 2 (min 60 min gap between bump-ups).
Business hours: 9 AM to 7 PM IST.
Handoff: mark Qualified once branch questions + contact preference are captured; terminal action is a human callback / expert consultation / office-visit appointment (SaaS: demo or Calendly booking; compliance: document checklist then payment link). Copy the client's hard gates where present: serviceable-area check, eligibility matrix, minimum-budget gate.
Hard rules: No guarantees (visa approval, loan / funding, tender wins, ROI, exact timelines). No exact price in chat: starting-price only or defer to the consult; never negotiate or discount. No final legal / tax / medical advice: defer to the expert on the call. KB-only, never fabricate pricing / features / eligibility. Deflect job-seekers, vendors and spam. Mirror the lead's language (Hinglish is first-class); do not mix scripts in one message. Never request passwords or credentials.

### faqs

Manual FAQs to add beyond CSV import:
- Pricing process, EMI and deposit terms (explain the process, not the exact number).
- What happens after I enquire, and how the consultation works.
- Service scope and inclusions, plus turnaround / timelines per service.
- Documents required per service, and eligibility / prerequisites.
- Credentials and experience, past work / testimonials, and refund policy.
- Where you are located, online vs office, and timings.
Replace the Kraya-seeded generic 6-FAQ pack (it ships in business / education / healthcare flavours and is often mis-matched to the org) with real, service-specific FAQs; ignore any leftover template scaffold headers.

### quickReplies

Seed: Service-list opener (the real menu), Proposal / quotation follow-up trio (recap, nudge, final), Consultation-booking confirmation, Document-checklist share (per service), 'Call Not Picked' / call-me reply, and a fees-process objection reply (explain the process, never the number).

### rules

Trigger 1: New Lead outreach.
  When: lead moved to stage "New Lead".
  Then: initiate_sequence (Call-Me or Credibility Nurturing).
  Delay: immediate.
Trigger 2: No-response DNP.
  When: no response from lead in "New Lead" for 4+ hours.
  Then: initiate_sequence (DNP).
Trigger 3: Sequence completed.
  When: a sequence completes with no reply.
  Then: move_to_stage "No Response" (or "Nurturing" for warm leads).
Trigger 4: Opt-out / stop keyword.
  When: keyword_detected ("stop", "not interested", opt-out). This overrides everything.
  Then: stop_assigned_sequence.
Trigger 5: Booked-consult reminder.
  When: lead moved to "Good Lead" (consult or demo booked).
  Then: set_call_reminder for the staff owner.
Lead-source note: B2B-services capture is the sparsest of any ICP. Wire IndiaMART and Meta Lead Ads where the client already advertises; do not assume a connector exists.

## agencies

### stages

Pipeline: "Sales", type: Leads
Stages (in order):
1. New Lead. AI starts here. Set as default stage.
2. Qualified. Auto-shifted once qualification completes.
3. In Conversation. AI or rep works the lead while scoping continues.
4. Hot / Warm Lead. Rep grades heat after interest is confirmed.
5. Proposal Sent. Rep moves after the quotation is shared.
6. Negotiation. Rep moves during the consultative close.
7. Lead Won. Mark as Won stage. Retainer or project onboarded (agencies) or advance/token paid (studios).
8. No Response. Non-responsive leads park here. Add Nurturing and Lead Lost as holding stages.
Note: Median funnel is deep (11 stages), this is the deepest of any ICP. Studio sub swaps steps 5-6 for Shortlisted then Call Done then Advance Paid (Won); AI must NEVER mark Won or Lost, a human confirms the advance.

### attributes

1. Service Interest. Dropdown. Values: Performance Marketing, Branding & Creative, Content, Social Media, Web / Tech, Full Stack. Description: "the service line the lead wants; this is the universal Q1 and the routing key".
2. Engagement Type. Dropdown. Values: Project (one-time), Retainer (monthly), Sprint (3-month), Audit / Consultation. Description: "how the lead wants to work with the team".
3. Budget Tier. Dropdown. Values: Under 1L, 1L-5L, 5L-25L, 25L+. Description: "scope-and-budget band the lead falls in" (studios use Under 50K, 50K-1L, 1L+).
4. Urgency. Dropdown. Values: Immediate, This month, This quarter, Just exploring. Description: "how soon they want to start".
5. Brand Stage / Size. Dropdown. Values: Just Starting, Growing, Established, Enterprise. Description: "business context the AI reads to tailor the pitch".
Studio sub: swap in Event Date (date, the primary availability router), Functions (multi-select: Wedding, Pre-Wedding, Corporate, Product), Location / Venue (in-city vs destination travel flag), Deliverables; drop the revenue framing. Performance sub: add Monthly Ad Spend / Revenue Band and Current Setup (ads running? website? current ROAS?). Meta-ad accounts add meta_lead_form / meta_leadgen_id tracking.
Important: Add a description to every attribute. AI uses descriptions for extraction accuracy.

### sequences

SHAPE REFERENCE ONLY. These are typical sequence shapes for a marketing agency or creative studio. The wording below is illustrative; the writer must rebuild content from the actual Client Profile (brand name, service lines, location, pricing).

Sequence 1: Nurturing (3 messages over 3 days)
  Trigger: Lead moved to "New Lead" with no reply.
  Day 1: warm intro plus one relevant case study or past-work link.
  Day 2 (+1d): ROI or process framing, how the team delivers results.
  Day 3 (+1d): soft push to book a discovery call.
  Attach: case-study link (agencies) or transformation reel / past-wedding gallery (studios).
  Stop: lead replies or books a call.

Sequence 2: DNP Recovery (4 messages over 5 days)
  Trigger: Lead in "New Lead" 4+ hours with no reply, or rep called and lead did not pick up.
  Day 1: short re-attempt about the enquiry.
  Day 2 (+1d): re-share the value, invite a quick reply.
  Day 4 (+2d): offer a convenient callback slot.
  Day 6 (+2d): last check-in before pausing outreach.
  Stop: any reply or a booked call.

Sequence 3: Discount / Offer (3 messages over 3 days)
  Trigger: Interested lead who has gone quiet before a proposal.
  Day 1: limited onboarding or free-audit offer.
  Day 2 (+1d): remind of the offer plus a proof point.
  Day 3 (+1d): final-day nudge to the booking link.
  Attach: rate-card or package PDF only where productised.
  Stop: reply, booking, or offer expiry.

Sequence 4: FOMO Reminder (3 messages over 3 days)
  Trigger: Lead engaged but not yet booked.
  Day 1: scarcity framing (limited strategy slots this month; studios: date is filling up).
  Day 2 (+1d): social proof, a recent client win or event.
  Day 3 (+1d): direct ask to lock the call.
  Stop: booking or an explicit no.

### orgInfo

Bot persona: an unnamed brand-team assistant that qualifies then hands to a human strategist (illustrative handle "Riya" only if a name is needed). Framed as a "Digital Growth Consultant, not a sales rep"; studios: represent the {brand} team, not any individual member. Never the founder.
Qualification questions (marketing flow, in order):
  1. Which service are you interested in?
  2. Business type plus name and city.
  3. Main challenge / current setup (ads running? website?).
  4. Monthly ad spend or revenue tier.
  5. How soon are you looking to start?
  6. Capture name + phone, then share the booking link.
Studio flow: event date (the hard gate) then single vs multi-day, location, functions, services, budget, timeline, then offer call + portfolio.
Handoff: mark Qualified once service + challenge + spend/revenue tier + timeline + name/phone are captured (agencies), or date + location + service + in-range budget (studios; if the date is not confirmed the lead is NOT qualified). Ack pattern: thank, confirm details captured, name the human team, state the next step. Terminal action is a discovery / strategy / audit call via the booking link, withheld until name + phone are captured.
Bump-up count: 2 (min 60 min gap between bump-ups).
Business hours: 10 AM to 7 PM IST.
Hard rules: one question at a time. Never quote fixed pricing before scoping. Never promise guaranteed results, ROI, or rankings. Never re-ask a Meta-prefilled field (validate first). Silent disqualification: never tell a lead they are disqualified, flag sub-threshold spend or budget internally. Never confirm a specific meeting time on the team's behalf. Studios: AI never marks Won or Lost, and never guarantees date availability until an advance is paid.

### faqs

Manual FAQs to add beyond CSV import:
- Pricing / quotation process (how quoting works, not a fixed number) and package customisation.
- Deliverables, timelines, and revisions.
- Portfolio / past work and results.
- Refund and payment terms (deposit / EMI) and in-house vs freelance team.
- "Do you guarantee results?" (answer honestly: no guarantees).
- Marketing add: "do I need a website first", "can you audit my ads", SEO/backlinks basics, "an agency failed me before". Studio add: candid vs traditional, drone/venue permissions, RAW-file policy, delivery windows, travel charges.
Warning: strip any edtech-template FAQ leakage (student/course/recorded-class questions) and the generic 6-FAQ seed before go-live; replace with real agency FAQs.

### quickReplies

Seed: Packages / rate-card opener, Portfolio share (link + one named result), Proposal follow-up trio, Negotiation reply (scope-first, never discount in chat), Booking-link drop, plus Follow-Up / Initial Response / Reminder groups.

### rules

Trigger 1: New-lead nurture.
  When: lead moved to "New Lead".
  Then: initiate the Nurturing sequence.
Trigger 2: Keyword stop.
  When: keyword_detected (lead replies with interest or a question).
  Then: stop_assigned_sequence. This ICP leans hardest on keyword-stop because leads reply fast.
Trigger 3: Sequence completed.
  When: the Nurturing sequence completes with no booking.
  Then: move_to_stage "No Response".
Trigger 4: DNP on silence.
  When: no_response_from_lead.
  Then: initiate the DNP Recovery sequence.
Trigger 5: Call reminder.
  When: a discovery / strategy / audit call is booked.
  Then: set_call_reminder for the assigned rep.
Lead-source note: wire Meta Lead Ads by default (30% of this ICP; Google Sheets 24%, JustDial 5%). Agencies run Meta lead ads on themselves, so validate or skip click-to-WhatsApp prefilled fields.

## education

### stages

Pipeline: "Admissions", type: Leads
Stages (in order):
1. New Lead. AI starts here. Set as default stage.
2. Qualified. Auto-shifted after the qualification flow completes.
3. In Conversation. Lead engaged, AI still nurturing, not yet committed.
4. Good Lead. Counsellor moves manually when the lead shows strong intent.
5. Enrolled. Mark as Won stage. Fees paid / admission confirmed. Rename to the client's word ("Admitted", "Joined").
6. No Response. Lead went cold across follow-ups.
Note: Insert "Demo Booked" + "Demo Done" if they sell via a free class (K-12, coaching). Add "Unqualified" if they disqualify on eligibility or budget. Create per-counsellor pipelines matching the counsellor's name for Round Robin only if 3+ counsellors.

### attributes

1. Course/Program. Dropdown. Values: the client's actual course list from the call. This is the central branch every other question hangs off. Description: "the course or program the lead is enquiring about".
2. Buyer Type. Dropdown. Values: Student, Working Professional, Parent (enquiring for child). Description: "who the enquiry is for; AI branches its tone on this".
3. Decision Timeline. Dropdown. Values: This Month, 1-3 Months, Just Exploring. Description: "how soon they plan to start or decide".
4. Learning Mode. Dropdown. Values: Online, Offline, Hybrid. Description: "preferred delivery mode".
5. Preparation Level. Dropdown. Values: Beginner, Some Prep, Advanced. Description: "current readiness; gates fit for coaching or exam programs".
Important: Add a description to every attribute. AI uses descriptions for extraction accuracy. Swap dropdown values for the client's real menu. High-ticket / skilling: add financial_readiness, current_ctc, intent_level. Study-abroad: swap in Destination Country, Degree Level (UG/PG/PhD), Test Status (GMAT/GRE/IELTS).

### sequences

SHAPE REFERENCE ONLY. These are typical sequence shapes for an education brand. The wording below is illustrative; the writer must rebuild content from the actual Client Profile (brand name, courses, location, intake dates, pricing). Mode: extension. Every message uses {lead_name} and names the specific program.

Sequence 1: DNP Follow-up (3 messages over 3 days)
  Trigger: Lead in "New Lead" for 4+ hours with no reply, or counsellor called but lead didn't pick up.
  Day 0: short re-attempt referencing the enquiry and the course they asked about.
  Day 1: check in about booking the counselling call. Attach: booking / Calendly link.
  Day 2: final, escalating follow-up. If genuinely interested, book a slot so a counsellor can guide them.
  Stop: lead replies or books a call.

Sequence 2: FOMO Reminder (3 messages over 3 days)
  Trigger: Qualified but not Enrolled; batch filling.
  Day 0: mention seats filling / intake closing soon for their program.
  Day 1: reinforce the real batch or intake deadline. Attach: brochure or fee-structure image.
  Day 2: last call before the batch starts.
  Stop: lead enrolls or replies "stop".

Sequence 3: Discount Offer (2 messages over 2 days; ONLY if the client actually runs an offer)
  Trigger: warm lead stalled on price.
  Day 0: share the current offer / EMI framing for their course.
  Day 2: remind the offer expires; nudge to book the call.
  Stop: lead enrolls or opts out.

Sequence 4: Nurturing (4 messages over 4 days)
  Trigger: lead marked "No Response" or low intent.
  Day 0: soft value message, an outcome or testimonial. Attach: testimonials.
  Day 1: share curriculum and what they will learn. Attach: brochure.
  Day 2: address the common objection ("is it worth it").
  Day 3: light re-invite to talk when they are ready.
  Stop: lead re-engages or asks to stop.

### orgInfo

Bot persona: "Priya" (illustrative), a warm admissions counsellor who introduces herself and the brand in msg 1. Use the client's named rep or bot if they have one.
Qualification questions (in order, one per message):
  1. Name and city.
  2. Student, working professional, or parent enquiring for a child.
  3. Which course or program are you interested in (the branching menu of the client's real courses).
  4. Specialisation / level.
  5. Entrance-exam status or score (only if relevant).
  6. When are you planning to start (intake / timeline).
  7. Preferred learning mode (Online / Offline / Hybrid).
  8. Budget (high-ticket / skilling only).
Bump-up count: 2 (min 60 min gap between bump-ups).
Business hours: 10 AM to 8 PM IST.
Handoff: when all answered, send the Final Acknowledgement ("our admission expert will connect with you shortly") and move the lead to Qualified; capture the callback slot but never confirm it; hand to a human on explicit request. Booking-link variant: share a Calendly / booking link. Video-gate variant (exam coaching): send the VSL, wait for "DONE", then send the booking link. Disqualify on age / eligibility / "won't invest": move to Unqualified and stop.
Hard rules: never disclose fees until the profile is known and only if asked. Never guarantee admission, placement, scholarship, or visa. One question per message; never re-ask an answered one. If the lead asks something, answer briefly from the FAQ then resume qualification.

### faqs

Manual FAQs to add beyond CSV import:
- Fees / EMI / refund policy (send the fee structure, often an image).
- Course curriculum, duration, and eligibility / prerequisites.
- Certification, recognition, and placement / mentorship outcomes.
- Enrollment process, batch timings, and online-vs-offline logistics.
- Credibility: who runs it, testimonials, and "why is it more expensive / is it worth it".
- Study-abroad only: destinations, IELTS/TOEFL, SOP/LOR, visa, intakes; plus program-concept explainers ("What is X").

### quickReplies

Seed: per-course info replies (one per major program, with brochure attached), Certificate/recognition answer, Objection-handling group (fees / "why expensive" / "is it worth it" / "need to ask parents"), counselling-call booking confirmation, plus Initial Response / Follow-Up / Reminder groups.

### rules

Trigger 1: New enquiry welcome.
  When: new lead created (lands in "New Lead").
  Then: send welcome and initiate the DNP Follow-up sequence.
  Delay: immediate.
Trigger 2: Cold new lead.
  When: lead sits in "New Lead" 4+ hours with no reply.
  Then: initiate DNP Follow-up.
  Delay: 4 hours.
Trigger 3: Stop on opt-out.
  When: keyword "stop" (or an unsubscribe reply) detected.
  Then: stop the assigned sequence.
  Delay: immediate.
Trigger 4: DNP exhausted.
  When: DNP Follow-up sequence completes with no reply.
  Then: move lead to "No Response" and initiate Nurturing.
  Delay: immediate.
Trigger 5: Qualified follow-up.
  When: lead in "Qualified" for 3 days with no counsellor action.
  Then: set a call reminder for the counsellor.
  Delay: 3 days.
Lead-source note: ~41% of orgs connect a source and education skews to Meta (FB/IG) Lead Ad forms (18%), Google Sheets (27%), IndiaMART (6%). Wire the Meta Lead Ad connector if they run FB/IG ads (add meta_lead_form / meta_lead_form_id attributes); use Google Sheets sync if they keep leads in a sheet.

## retail

### stages

Pipeline: "Orders", type: Leads (single pipeline; AI-on at the first stage; Round Robin only if 3+ reps).
Stages (in order):
1. New Lead. AI starts here. Set as default stage.
2. Qualified. Auto-shifted once catalogue shared AND acknowledgement sent.
3. Nurturing. Lead engaged but undecided.
4. Good Lead. High-intent / short-timeline buyer flagged for the team.
5. Order Confirmed. Mark as Won stage. Order placed / paid.
6. No Response. Lead went cold after outreach.
Also add: Cold Lead, Lead Lost.
Note: If they sell wholesale, add a parallel "B2B Wholesale" stage behind a verification gate. Store-visit businesses (furniture, jewellery, optical) add "Store Visit Booked".

### attributes

1. Product Category. Dropdown. Values are the client's catalog lines (illustrative apparel set: Sarees, Kurtis and Suits, Lehengas; jewellery: Rings, Necklaces, Earrings; furniture: Sofa, Bed, Dining). Description: "the product line the buyer is asking for".
2. Purchase Type. Dropdown. Values: Personal Purchase, Wholesale / Bulk Purchase. Description: "routes retail vs bulk; AI reads this to gate wholesale pricing".
3. Variant / Spec. Dropdown. Category-specific values (apparel: fabric / occasion; jewellery: metal / finish; furniture: style / seating). Description: "the spec that branches the flow and picks the right catalogue".
4. Budget. Dropdown. Values: Under ₹500, ₹500 - ₹2,000, ₹2,000+. Description: "price band; keep only if they qualify on price, else defer to team".
5. Delivery City / PIN. Text. Description: "delivery location for shipping and COD serviceability".
Important: Add a description to every attribute. AI uses descriptions for extraction accuracy. For wholesale buyers add a "Monthly Requirement" (number) attribute.

### sequences

SHAPE REFERENCE ONLY. These are typical sequence shapes for a retail / e-commerce brand. The wording below is illustrative; the writer must rebuild content from the actual Client Profile (brand name, catalog lines, trust hook, pricing, branches).

Sequence 1: DNP Follow-up (3 messages over 2 days)
  Trigger: Lead in "New Lead" for 4+ hours with no reply, or rep messaged but lead went silent.
  Day 0: short nudge referencing the category they asked about + offer to share the catalogue.
  Day 1: re-send the matching catalogue link + one product benefit.
  Day 2: gentle "still keen?" with a starting-from price hook.
  Attach: category catalogue PDF / Drive link.
  Stop: lead replies, or moves to Qualified / Order Confirmed.

Sequence 2: Post-Interest Hot-Buyer Close (3 messages over 2 days)
  Trigger: Lead gave category + variant / viewed catalogue but has not confirmed.
  Day 0: recap their pick + ask for delivery city and Prepaid/COD preference.
  Day 1: trust hook (quality / authenticity guarantee) + gentle scarcity ("limited stock").
  Day 2: last nudge + offer to reserve the item.
  Attach: size chart / spec image.
  Stop: order confirmed, or lead asks for a human.

Sequence 3: Thinking / Comparing Nurture (3 messages over 5 days)
  Trigger: Lead engaged but undecided, moved to "Nurturing".
  Day 0: one-line reassurance on range + starting price.
  Day 2: a simple buying guide for the category (how to choose).
  Day 4: real customer testimonial (ONLY if one exists in the Client Profile; otherwise replace this day with a buying-guide or value message. Never ship a placeholder).
  Attach: comparison / spec image.
  Stop: lead re-engages, or moves stage.

Sequence 4: Lead-Lost Dormant Revival (3 messages over 4 days)
  Trigger: Lead in "Lead Lost" or "Cold Lead" for 30+ days, no activity.
  Day 0: new-arrivals or festive teaser.
  Day 2: discount / FOMO ("offer ends soon").
  Day 4: final re-engagement + catalogue link.
  Attach: offer / new-collection catalogue PDF.
  Stop: lead replies, or opts out.

### orgInfo

Bot persona: "Naina", ethnic-wear personal shopper / category consultant. Warm human expert, explicitly not a bot and not a form-filler.
Opener: "Welcome to {org_name}" + one-line category intro + the product-category menu (or one question).
Qualification questions (in order):
  1. Personal purchase or wholesale / bulk?
  2. Which product category?
  3. Which variant / spec (size, fabric, metal, style)?
  4. Budget band? (optional; skip if they defer on price)
  5. Delivery city / PIN.
  6. Timeline to buy.
  7. Name + delivery address + Prepaid/COD (at close).
Handoff: the moment the category is known, share the matching catalogue, send an acknowledgement ("our team will connect with options"), then mark Qualified (gate: catalogue shared AND ack sent). Escalate to a human immediately for pricing / discount / negotiation, bulk / wholesale, complaints, damage / refund. Wholesale branch: ask for GST / business card / Instagram before unlocking wholesale catalogue or pricing.
Bump-up count: 2 (min 60 min gap between bump-ups).
Business hours: 10 AM to 8 PM IST.
Hard rules: never fix or negotiate a final price on chat, give ranges only or defer to the team. Accept letter / number / keyword replies interchangeably. Auto-tag high-intent or short-timeline leads. If a testimonial or branch detail is missing from the Profile, write the message without it and flag the gap in Builder Notes; never ship placeholder text into live copy.

### faqs

Manual FAQs to add beyond CSV import:
- Product catalogue: what is available and how to browse (send catalogue PDF / Drive link / wa.me catalogue).
- Pricing & payment: price ranges, discounts, EMI, COD, deposit.
- Delivery & logistics: shipping cost, timelines, outstation / international.
- Returns / exchange / refund / warranty.
- Authenticity / quality / certification (jewellery 925 / BIS hallmark, watches genuine-vs-clone, apparel fabric / handcrafted).
- Customization / bulk / MOQ / B2B qualification, plus brand story / heritage / testimonials and store location & timings.

### quickReplies

Seed: ORDER (how to order, step by step), Addresses & Contacts (branch list + Maps), price-band replies ("Starting Range", real bands from the catalog), payment details (bank/UPI + screenshot ask), order-status / dispatch update, delivery-delay empathy reply, and a review request for delivered orders — in the customer-facing language.

### rules

Trigger 1: Start nurture on stage move.
  When: lead_moved_to_stage = "Nurturing".
  Then: initiate the Thinking / Comparing Nurture sequence.
  Delay: immediate.
Trigger 2: Route by product keyword.
  When: keyword_detected (a category / product word in the reply).
  Then: set_lead_attribute Product Category and tag for routing.
  Delay: immediate.
Trigger 3: Tag buyer-type.
  When: keyword_detected ("wholesale", "bulk", "shop owner", "GST").
  Then: set_lead_attribute Purchase Type = Wholesale and flag for the verification gate.
  Delay: immediate.
Trigger 4: Advance on sequence completion.
  When: sequence_completed (Post-Interest Hot-Buyer Close) with no reply.
  Then: move_to_stage "No Response".
  Delay: after the last message.
Trigger 5: Revive dormant leads.
  When: lead_moved_to_stage = "Lead Lost" or "Cold Lead" for 30+ days.
  Then: initiate Lead-Lost Dormant Revival.
  Delay: 30 days.
Lead-source note: wire Meta (FB / IG) Lead Ads by default for D2C retail; add IndiaMART if they sell wholesale.

## manufacturing

### stages

Pipeline: "Quotation", type: Leads
Stages (in order):
1. New Lead. AI starts here. Set as default stage.
2. Qualified. Auto-shifted once product, quantity, location and contact are captured.
3. Sample Required. Staff moves here when the buyer asks for a sample or swatch. Drop this stage if the client does not sample.
4. PI Sent (Waiting Acceptance). Staff moves here after sharing the Proforma Invoice.
5. Waiting for Payment. Staff moves here to chase the advance or full payment.
6. Order Confirmed. Mark as Won stage. Payment received, stock locked.
7. Repeat Order Follow-up. Staff moves here to chase reorders.
8. Not Qualified (Low Quantity/Budget). Locked disqualification stage for below-MOQ, personal or out-of-area buyers.
Note: Add Nurturing, Good Lead and No Response holding stages between Qualified and the tail. Add a Dealership Approach stage if they recruit distributors, and territory stages only if they route leads by region.

### attributes

1. Product Category. Dropdown. Values: the client's real catalog lines (for a polymer maker: PVC Pipes, Fittings, Sheets, Granules). Description: "which product line the buyer needs; drives quote routing".
2. Buyer Type. Dropdown. Values: End-user, Dealer, Architect, Contractor, OEM, Distributor (keep only the client's real channels). Description: "channel of the buyer; gates dealer pricing and routes retail buyers out".
3. Monthly Quantity / MOQ. Dropdown. Values: volume bands in the client's unit (Below 500, 500 to 2000, 2000 plus kg or pcs). Description: "order volume; below-MOQ leads are disqualified".
4. Material / Spec. Dropdown. Values: the spec axis that makes the product quote-ready (size, model, GSM, grade, capacity, gas type). Description: "technical requirement the team needs to price it".
5. Company + GST. Text. Description: "firm name and GST number; required before sharing the dealer price list".
Important: Add Existing Customer and Season/Peak Time attributes for seasonal or repeat businesses. Add a description to every attribute. AI uses descriptions for extraction accuracy.

### sequences

SHAPE REFERENCE ONLY. These are typical sequence shapes for a manufacturer or industrial supplier. The wording below is illustrative; the writer must rebuild content from the actual Client Profile (brand name, product lines, buyer types, service areas, MOQ).

Sequence 1: Identify Requirement (5 messages over 7 days)
  Trigger: Lead lands in "New Lead" and has not stated a clear requirement.
  Day 1 (+1m): warm opener, ask buyer type and product needed.
  Day 1 (+5m): short reminder to reply with buyer type so the right details go out.
  Day 2: check in once on the requirement.
  Day 4: "we are closing pending enquiries this week; reply if you still need it."
  Day 7: final "closing this enquiry for now, message us anytime."
  Attach: product catalogue. Stop condition: lead replies with a requirement or moves to "Qualified".

Sequence 2: Waiting for Payment (3 messages over 3 days)
  Trigger: Lead moved to "Waiting for Payment" after the PI is shared.
  Day 1: "material is kept ready per the PI; update us once payment is processed."
  Day 2: gentle nudge, note the stock is held for them.
  Day 3: peak-season line, ask for a small advance to reserve stock.
  Attach: Proforma Invoice. Stop condition: payment confirmed or lead moves to "Order Confirmed".

Sequence 3: Sample Feedback (3 messages over 4 days)
  Trigger: Lead in "Sample Required" with the sample delivered and no feedback.
  Day 1: ask if the sample met the spec.
  Day 2: offer to adjust the spec or share alternatives.
  Day 4: ask to move to a formal quotation if the sample works.
  Attach: spec sheet. Stop condition: feedback given or lead moves to "PI Sent (Waiting Acceptance)".

Sequence 4: DNP Re-attempt (3 messages over 3 days)
  Trigger: Rep called and the lead did not pick up, or no reply for 4 plus hours.
  Day 1: short re-attempt referencing their enquiry.
  Day 2: offer a callback window.
  Day 3: escalating "closing this enquiry" message.
  Attach: brochure. Stop condition: lead replies or a call connects.

### orgInfo

Bot persona: unnamed brand voice, "{org_name} Sales Assistant". Hands off to a named senior rep (illustrative: "Rohan").
Opener: "Welcome to {org_name}, India's manufacturer of [X]" plus a credibility line (years, manufacturer-direct, ISO/GST) and the product menu, then Q1.
Qualification questions (in order):
  1. Product or requirement (routing menu).
  2. Buyer type (dealer, contractor, end-user, OEM).
  3. Quantity or MOQ.
  4. Technical spec (size, model, GSM, grade, capacity).
  5. Location or city.
  6. Timeline, then soft budget.
  7. Business verification (GST or visiting card).
  8. Contact.
Handoff: mark "Qualified" once product, quantity, location and contact are captured, then acknowledge "our team will review and share a quote shortly." Escalate to the named senior rep on sample, site or factory visit, price negotiation, bulk or export order, or dealership enquiry. Disqualify below-MOQ, personal or out-of-area buyers to "Not Qualified (Low Quantity/Budget)".
Bump-up count: 2 (min 60 min gap between bump-ups).
Business hours: 10 AM to 7 PM IST.
Hard rules: NEVER quote price, MOQ, discount or delivery date in chat; always defer to "our team will share a quote or PI." Verify GST or visiting card before sharing any dealer price list. Route personal or retail buyers out if the client is B2B-only. In peak season collect a small advance to lock stock before handing to the senior team. Add bot_languages (Hindi plus regional) if the client serves regional markets.

### faqs

Manual FAQs to add beyond CSV import:
- Product range plus custom, OEM, bulk and white-label capability.
- How ordering and quotation work, including the Proforma Invoice process.
- Pricing, bulk discounts and MOQ (answered via KB, never quoted live in chat).
- Delivery, shipping, service areas and export coverage.
- Installation, warranty, after-sales, and company credentials (ISO, GST, years, export).
- Dealership terms, payment and advance terms, and trust proof (genuineness of imports, why premium).

### quickReplies

Seed: PI/Payment follow-up (stock held, advance ask), Visiting-card/GST request (gate for dealer pricing), Sample-swatch offer, Address-for-delivery request, below-MOQ polite decline (restate the minimum), and a 'backup supplier' opener for buyers with an existing vendor; generate multilingual copies (Hindi plus regional) if the client serves regional markets.

### rules

Trigger 1: Identify requirement.
  When: lead moved to "New Lead".
  Then: initiate the Identify Requirement sequence.
  Delay: +1 min.
Trigger 2: Product-menu keyword.
  When: keyword_detected matches a product-menu term.
  Then: route the lead to the right product line and stop the generic sequence.
  Delay: immediate.
Trigger 3: Enquiry gone cold.
  When: sequence_completed with no reply.
  Then: move the lead to "No Response" (or "Nurturing" for warm leads).
  Delay: after the final message.
Trigger 4: Payment chase.
  When: lead moved to "Waiting for Payment".
  Then: start the Waiting for Payment sequence and send the approved PI template.
  Delay: +5 min.
Trigger 5: Below MOQ.
  When: keyword_detected signals quantity under the MOQ.
  Then: move the lead to "Not Qualified (Low Quantity/Budget)".
  Delay: immediate.
Lead-source note: wire IndiaMART (and JustDial) by default; the B2B-marketplace connector is the clearest Manufacturing tell. Add Meta Lead Ads for an export or international client.
