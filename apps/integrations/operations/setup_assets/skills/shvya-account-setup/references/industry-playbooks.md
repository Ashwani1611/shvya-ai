# Industry playbooks for company-specific Shvya setup

These are design menus, not deployable facts or defaults that override the client's process. Use only the relevant industry and rewrite with actual products, approved policies, available assets and current MCP capabilities. Hours, personas, price bands, thresholds, offers and integrations come from the client. No industry automatically gets a discount, testimonial, free trial or connected lead source.

Choose `retail` for finished goods to consumers; `manufacturing` for physical goods supplied to businesses; `b2b` for business services/software; `agencies` for marketing/creative/production services; `education` for learning/admissions. A visa-led consultancy fits B2B professional services; admissions/test-prep fits education. Use `other` when no match is credible, with the same profile-first workflow.

The gate patterns are: **volume/fit** (buyer/product/quantity match), **deliverable inputs** (enough information for a specialist), **profile + intent** (appropriate offering and readiness), and **routing** (enough to get the person to the correct team). Optional details must not block a simpler agreed gate. Selecting an industry does not authorize regulated advice.

## Healthcare

- **Gate/Playbook:** deliverable inputs. Ask concern/department, relevant prior context and preferred location/timing only as needed. If the initial message states the treatment, skip the menu. Explicit human requests and urgent health concerns interrupt sales qualification. Give a general urgent-care direction for emergencies rather than inventing local numbers. Never diagnose, interpret reports as clinical advice, prescribe or guarantee results. Reception or a supported booking system confirms the slot.
- **Attributes:** treatment/department option, visit type, city/branch, preferred slot, callback preference, limited relevant intake context. Doctor names and medical questions require approved source material; do not collect unnecessary sensitive images or history.
- **Stages:** existing New/Qualified plus Appointment Requested, Appointment Confirmed, Visited, Treatment Started, No Show and Human Intervention where those are real events. Human/system confirmation owns appointment, attendance and payment milestones. Preserve Shvya protected names.
- **Cadences:** short enquiry recovery, consultation preparation, confirmed appointment reminder when scheduling supports it, no-show reschedule and after-visit administrative follow-up when authorized. Use sourced credentials, preparation information and logistics; no fear, fabricated scarcity or treatment-outcome promises.
- **Workflows:** silence routing, stop on reply/opt-out, handoff, supported appointment/reminder event. A no-show must be observed, not inferred from no chat reply. Avoid keyword diagnosis.
- **FAQ/Touchpoints/assets:** consultation process, verified fees within disclosure policy, hours/location, preparation, appointment changes, insurance only if actually provided. Clinic brochure and Maps link only when sourced. Operator groups can include Initial Response, Pre-Consultation, Rescheduling and Support.

## Real estate

- **Gate/Playbook:** profile + intent. Buy/rent/sell/invest branch; property type, location, budget and timeline; sellers provide property details rather than purchase budget, renters provide move-in and lease preferences. Avoid guarantees about returns, appreciation, possession, legal status or financing. A requested visit is distinct from a confirmed visit.
- **Attributes:** intent, actual inventory type, locality, budget amount/band with units, decision timeline, self-use/investment, preferred visit datetime where confirmed. No invented project list or arbitrary minimum budget.
- **Stages:** Site Visit Requested/Confirmed, Visit Done, Quotation Sent, Negotiation, Won, No Response, human handoff. Consultant owns negotiation and transaction confirmation.
- **Cadences:** matching-option education, visit preparation, post-visit discussion, finite recovery. Launch/price-revision messages only for a real dated event with current inventory and expiry. Distances, area prices and RERA references need exact sources.
- **Workflows:** stage-based follow-up, visit reminder with supported anchor, reply/opt-out stops, no-response parking. Budget rules must use a validated criterion or canonical field state, not substring-matched numeric text.
- **FAQ/Touchpoints/assets:** project scope, booking steps, documented payment structure, site visit logistics, sourced legal identifiers/documents, loan assistance only if offered. Brochures, floor plans and Maps links need an actual send path. Quick replies focus on availability verification, directions, visit requests and quotation follow-up.

## Fitness

- **Gate/Playbook:** profile + intent or routing. Goal, offering, branch, experience and preferred trial/consultation as relevant. A free trial is mentioned only if offered. Avoid body-shaming, medical advice and guaranteed transformation timelines. A named AI persona identifies honestly.
- **Attributes:** goal, actual programs, membership interest, experience, branch and preferred slot. Health limitations route to a qualified professional; do not generate medical suitability judgments.
- **Stages:** Trial Requested, Trial Confirmed, Trial Attended, Membership Active, No Response and Human Intervention as needed. Staff confirms attendance and payment.
- **Cadences:** enquiry nurture, trial preparation/reminder, no-show reschedule, decision support and member onboarding if in scope. Use verified schedules, beginner information and permitted genuine success stories; no fake joining-offer clock.
- **Workflows:** silence recovery, trial-stage preparation, reply/opt-out stops, converted-stage sales stop. A scheduled Cadence cannot claim the lead trained or attended without evidence.
- **FAQ/Touchpoints/assets:** membership terms/fees, class timetable, location, cancellation, trainer qualifications and what to bring. A schedule or membership PDF should be current; avoid edtech/student FAQ leakage.

## Travel

- **Gate/Playbook:** routing or profile + intent depending on quote complexity. Destination, dates or fixed departure selection, travelers, duration and budget only as necessary. For a fixed-departure trip ask whether listed dates work rather than suggesting arbitrary availability. Group/corporate, payment, price negotiation and urgent in-trip issues go to the responsible team. Never guarantee visa approval, availability, refunds or weather.
- **Attributes:** destination, travel month/dates, trip type, traveler count with adult/child distinction if needed, departure city, duration, budget and hotel preference. Passport readiness only for relevant international planning.
- **Stages:** Itinerary Requested/Sent, Quotation Sent, Booking Confirmed, Future Interest and Human Intervention. Human/provider confirms booking and payments.
- **Cadences:** destination-specific information with a real itinerary asset, quote discussion, enquiry recovery, authorized post-booking preparation. New-trip or seasonal revival needs permission and a real new reason. Do not promise 24-hour support unless documented.
- **Workflows:** destination condition selects the right Cadence; silence recovery; request for specialist stops generic nurture; confirmed booking stops pre-sale copy. Scheduling uses current pipeline sender.
- **FAQ/Touchpoints/assets:** inclusions/exclusions, payment terms, change/cancellation policy, visa/insurance assistance scope and real support contacts. Quote validity and deposit percentages are client facts, never industry defaults.

## B2B services and technology

- **Gate/Playbook:** profile + intent or routing. Start with actual service/use case, business context, branch-specific scope, timeline and next-step preference. Budget and decision-maker questions are optional unless the agreed gate needs them. SaaS may capture team size/use case; professional services collect relevant scope without passwords or credentials. Never guarantee ROI, visa/loan/tender approval, legal outcomes or implementation dates.
- **Attributes:** actual service, business type, use case/problem, team size where relevant, location, timeline and contact preference. Preserve canonical source attribution.
- **Stages:** Discovery Requested/Booked, Demo Done, Proposal Sent, Documents Requested/Received, Negotiation and Won as relevant. Route job seekers/vendors to a distinct owner rather than qualifying them as buyers.
- **Cadences:** process/credibility nurture, callback recovery, proposal clarification, real document-checklist delivery and dormant recontact when permitted. No invented case studies, customer logos or product features.
- **Workflows:** stage-to-Cadence, no-response recovery, reply/opt-out stop, specialist handoff and supported reminders. Do not adopt a legacy hardcoded company-registration menu for a SaaS client.
- **FAQ/Touchpoints/assets:** service inclusions, onboarding steps, supported integrations, real limitations, pricing model/disclosure, documents needed, cancellation and support. Shvya AI's own setup should use verified Shvya product facts plus its actual Ria policy, not another industry's claims.

## Agencies and creative production

- **Gate/Playbook:** profile + intent. Marketing branch asks service, business context, challenge/current setup, spend if required and timeline. Event/photo/video branch asks date, location, production needs and budget if required. Tentative date is not availability confirmation. Never speak as the founder unless that is an explicitly appropriate and truthful team representation; no guaranteed ROAS, rankings or booked talent.
- **Attributes:** actual service, project/retainer, goal, scope, start timeline, budget/spend; studios may use event date, venue and deliverables. Do not create a nonexistent multi-select type: use available field types deliberately.
- **Stages:** Discovery, Brief Received, Proposal Sent, Negotiation, Advance Verified, Project Confirmed. Human owns commercial agreement and payment verification.
- **Cadences:** portfolio/process explanation, discovery preparation, proposal discussion and recovery. A free audit or limited onboarding capacity requires evidence. Include scope/revision mechanics rather than generic ROI promises.
- **Workflows:** new-enquiry flow, reply stop, human handoff, finite silence recovery and supported reminders. A proposal-sent stage must reflect an actually sent proposal.
- **FAQ/Touchpoints/assets:** deliverables, revision terms, timelines, portfolio, source-backed results, refund/payment policy and logistics. Studios add RAW-file, travel and venue-permission policy only if documented. Remove unrelated course/student FAQ seeds.

## Education

- **Gate/Playbook:** profile + intent. Course, student/professional/parent, relevant background or prerequisites, learning goal, intake timeline and delivery mode. Branch by the real course list. Eligibility and exam scores apply only where needed. Do not guarantee admissions, placements, salary, scholarships or visas. Explicit parent/student routing should be appropriate to the supplied audience.
- **Attributes:** course, audience role, background, timeline, actual delivery modes, preparation level and relevant eligibility evidence. Study-abroad intake/country/test fields are not general education defaults.
- **Stages:** Counselling Requested/Booked, Demo Scheduled/Attended, Application In Progress, Enrolled and Payment Pending only as actual business events. Staff/system owns enrollment/payment verification.
- **Cadences:** curriculum/process, counselling preparation, real outcomes with permission, FAQ/objection explanations, no-response recovery. Intake/batch urgency only with a sourced date, capacity and expiry; no fictional seats filling.
- **Workflows:** lead-created welcome owner, finite silence recovery, counsellor reminder, reply/opt-out stops. Avoid duplicate welcome from both AI and Cadence. A watched-video gate must have a supported evidence mechanism; a message asking for video is not completed viewing.
- **FAQ/Touchpoints/assets:** curriculum, duration, eligibility, verified recognition, teaching mode, genuine fee/EMI/refund policy, actual learner support and enrollment steps. Brochures and fee structures must match current course versions.

## Retail and D2C

- **Gate/Playbook:** routing. Product/category can be sufficient for a sales handoff; variant, delivery location and payment preference follow only when needed. Branch personal/bulk if both are offered. Do not delay a simple routing gate with optional budget questions. Wholesale-price access can require verified business details only if the client actually enforces it.
- **Attributes:** product category, purchase type, actual variant/spec, delivery city/PIN, budget if needed and quantity for bulk. Never request a full delivery address earlier than necessary.
- **Stages:** Product Interest, Quote Sent, Order Confirmed, Dispatched, Delivered, Support and Human Intervention where observed. A payment screenshot is not proof of a paid order.
- **Cadences:** category information, buying guide, quote/selection support, finite recovery, authorized order information. Stock holds, limited availability, authenticity, delivery dates and seasonal offers must be verifiable.
- **Workflows:** segment routing, nurture stage start, reply/opt-out stops, confirmed-order sales stop. Do not revive opt-outs via a generic lost-stage marketing rule.
- **FAQ/Touchpoints/assets:** catalogue, size/spec guide, serviceability, shipping/returns/warranty, real COD/payment policy, store location and order support. Share genuine payment destinations only via approved company process.

## Manufacturing, wholesale and industrial supply

- **Gate/Playbook:** volume/fit or deliverable inputs. Real product, buyer type, numeric quantity/unit, technical specification, serviceable location and commercial timeline. Branch dealer/distributor/OEM/end-user only if the company uses those channels. Distinguish below MOQ, out-of-scope, supplier enquiry and uninterested. Share an approved MOQ if permitted; do not inherit the source's conflicting "never state MOQ" blanket rule.
- **Attributes:** product, buyer type, quantity numeric with unit, material/spec, location, company, timeline, sample status and verification state when supported. Collect GST/business documents only when necessary and through approved handling.
- **Stages:** Requirement Received, Quotation Sent, Sample Requested/Delivered, Feedback Pending, PI Sent, Payment Pending, Order Confirmed, Repeat Order and human handoff where useful. Commercial, sample and payment milestones are human-owned by default.
- **Cadences:** requirement clarification, quote details, sample feedback, permitted PI/payment reminder and repeat-order planning. Never say stock is held, payment due, delivery committed or sample received without its actual event/evidence.
- **Workflows:** no-response recovery, segment routing and stage-linked follow-up. Numeric MOQ is a validated numeric criterion, not `keyword contains 500`. Below-MOQ/low-budget and opt-out are different outcomes. Pause sales automation during human negotiation.
- **FAQ/Touchpoints/assets:** catalogues/spec sheets, verified certifications, ordering/quotation process, sample policy, actual logistics/warranty/installation, payment and dealership terms. No guessed margins, lead times, ISO/GST details or marketplace integrations.

## Other / mixed businesses

Build from the company profile. Choose the smallest meaningful routing or qualification gate, genuine funnel events, useful fields and available content. Mixed offerings can use a top-level product branch, with eligible required questions below it. Do not force the source's generic event-planning persona onto an unrelated business. Unsupported branches or actions remain explicit design gaps until the runtime supports them.
