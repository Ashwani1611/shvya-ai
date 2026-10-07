# Kraya industry templates (seeded at onboarding)

Source: `Kraya-Laravel/config/industry.php`, `config/industry_faqs.php`, `config/industry_leads.php`. These are what `POST /organizations/onboarding` seeds into a new org for its industry (`getIndustryConfig()` falls back to `other` for unknown keys and for `other: <text>`). An account handed to the agent will normally already contain these. Reuse the sequence names when a rule needs them, or replace them; never create a second copy.

Industry keys: `education_training`, `healthcare_medical`, `real_estate_construction`, `travel_tourism`, `manufacturing_industrial`, `other`


---

## education_training (Education & Training)

### Sequences

#### Discount/Offer

_Highlights exclusive offers such as "Early Bird Discounts," "Fee Waivers," or "Limited-Time Enrollment Bonuses." Encourages hesitant leads to finalize their admission decisions before the opportunity expires._

Mode: `extension` · messages: 3

- **Message 1** (whatsapp, immediate)
  > 👋 Hello and welcome to our learning community!
  >
  > Here's something special to help you start strong:
  >
  > - 🎉 Enjoy a *discount on your first enrollment* with us.
  > - 📚 Perfect for any subject, skill, or goal you want to pursue.
  > - 💬 Just mention this message when you sign up to activate the offer.
  >
  > ✨ Let's make your learning journey exciting from Day 1!
- **Message 2** (whatsapp, +1 days)
  > Gentle Reminder 🌟
  >
  > - This discount is our way of saying thank you for joining us.
  > - We know everyone learns differently - that's why our programs are flexible.
  > - Take this chance to start something new, at your own pace.
  > - We're right here to support your goals every step of the way. 🚀
- **Message 3** (whatsapp, +2 days)
  > Last Chance ⏰
  >
  > - The offer is *ending soon*!
  > - Whether you want to take a short class or a complete course - this is your time.
  > - Let's make learning more affordable and rewarding.
  > - Don't miss your special discount - *join today*! 🎓

#### FOMO Reminder Sequence

_Creates urgency for admissions, scholarships, or limited seats in courses. Emphasizes deadlines, placement rates, and success transformations of past students to drive quick action._

Mode: `extension` · messages: 3

- **Message 1** (whatsapp, immediate)
  > Spots Filling Fast 🚀
  >
  > - Our *new sessions and learning slots are filling up quickly!* - Secure your preferred schedule before it's taken.
  > - We'd love to see you learning and growing with us. 🌟
- **Message 2** (whatsapp, +1 days)
  > Quick Reminder ⏰
  >
  > - Time is ticking - only a few spots remain for the current batch.
  > - Join now to get access to personalized learning support.
  > - Let's lock in your schedule before this batch closes. 📆
- **Message 3** (whatsapp, +2 days)
  > Final Call 🚨
  >
  > - This is your *last chance* to enroll before registrations close
  > - Be part of a dynamic learning community.
  > - Take the leap today - let's start building your future together! 💫

#### DNP Follow-Up Sequence

_Re-engages students who showed interest but didn't book or attend a counseling session. The messages build trust by highlighting success stories, personalized guidance, and the importance of timely career moves. Designed to remind, educate, and reschedule seamlessly._

Mode: `extension` · messages: 3

- **Message 1** (whatsapp, immediate)
  > Missed You 🌱
  >
  > - We noticed we might have missed connecting with you earlier.
  > - No worries - we understand things get busy.
  > - Whenever you're ready, we'll be here to help you continue your learning journey. 💬
- **Message 2** (whatsapp, +1 days)
  > Gentle Check-In 👋
  >
  > - Just checking in to see how you're doing!
  > - We'd love to help you get back on track whenever it's convenient.
  > - Let's make learning easy and flexible around your schedule. 😊
- **Message 3** (whatsapp, +2 days)
  > Always Here for You 💫
  >
  > - Learning is a journey, and we'll be here whenever you're ready to restart.
  > - Reach out anytime - we'll make sure you have everything you need.
  > - Let's help you reach your goals, one step at a time. 🌟

### Smart triggers

| Name | Trigger | Action | Keywords / binding | Enabled |
|---|---|---|---|---|
| Stop on STOP Keyword | keyword_detected | stop_assigned_sequence | keywords: stop | True |
| Stop if Lead Won | lead_moved_to_stage | stop_assigned_sequence | on: Lead Won | False |
| Trigger FOMO Reminder Sequence | sequence_completed | initiate_sequence | → seq: FOMO Reminder Sequence; on: DNP Follow-Up Sequence | False |

### Knowledge base seed (org_info)

`about` (1500 chars):

```
AI Launchpad Academy helps learners go from “curious about AI” to “confident, job-ready” through mentor-led online programs.  
Our curriculum is designed and taught by industry professionals from leading tech companies. We focus on project-based learning, portfolio building, and clear career paths into AI, ML, Data, and Analytics roles.

Core Programs:
- AI & Machine Learning Master Program  
- Data Science & Analytics Bootcamp  
- Python for AI & Automation (Beginner Friendly)  
- MLOps & Deployment Essentials  
- Career Accelerator: Resume, LinkedIn & Interview Prep

Key Differentiators:
- Live mentor-led sessions with small cohorts  
- Real-world capstone projects and GitHub-ready portfolios  
- 1:1 career guidance, mock interviews, and profile reviews  
- Flexible schedules for working professionals

Guidelines:
- Ask questions that uncover the learner's goal, background, and timeline so advisors can recommend the right program.  
- Keep tone supportive and non-judgmental; many learners are beginners.  
- Do **not** guarantee jobs, salaries, or admission to specific companies.  
- Avoid quoting prices unless clearly provided; instead, say:  
  “Our academic advisor will share complete fee details and available offers.”  
- If unsure about a technical or policy query:  
  “That's a great question — our academic advisor will explain that in detail on a quick call.”  
- Use insights from **Validation_Snippets.pdf** for short, positive validations after the learner's replies.
```

`qualification_requirements` (4541 chars):

```
#### 1️⃣ Program Interest  [Validation Source: By Program]
Ask:  
> “Hi there! Thanks for reaching out to *AI Launchpad Academy.* Which area are you most interested in right now — *AI & ML*, *Data Science*, or *Python basics*?”

If unclear or “not sure”:  
> “No worries at all — which topics sound more exciting to you: building AI models, working with data, or learning to code from scratch?”

---

#### 2️⃣ Learning Goal  [Validation Source: By Goal]
Ask:  
> “What's your main goal — *career switch*, *growth in your current role*, or *learning for academics / curiosity*?”

If multiple goals: ask which is **highest priority** for the next 6-12 months.

---

#### 3️⃣ Background & Experience  [Validation Source: By Profile]
Ask:  
> “Could you share a bit about your background — are you a student, fresher, or working professional? And what did you study or what do you work in currently?”

If very short answer:  
> “Got it — and do you have any prior coding or math experience, even basics?”

---

#### 4️⃣ Timeline & Availability  [Validation Source: By Stage]
Ask:  
> “When are you planning to start learning — *this month*, *within 3 months*, or *just exploring for later*?”

If they say “just exploring”:  
> “That's totally okay — having clarity early really helps. We can still suggest a path so you know what to aim for.”

---

#### 5️⃣ Learning Preferences (Temperature-Based)  [Validation Source: By Preference]
Ask (balanced / consultative):  
> “What would suit you better — *live mentor-led batches* with fixed timings, or a *more flexible, self-paced flow* with doubt support?”

For higher question temperature (4-5), add:  
> “Also, how many hours per week can you realistically invest in learning?”

---

#### 6️⃣ Handoff Details  [Validation Source: General]
Ask:  
> “Awesome, this really helps. Could you please share your *full name* and *best WhatsApp number / email* so our academic advisor can send you detailed program options and guide you personally?”

---

### Logic Rules
- Move step-by-step; skip obvious questions if the learner already provided details.  
- If answers are extremely brief, use one clarifier—do not interrogate.  
- If the learner directly asks for a call or counselling session, collect name + contact + core interest, then move to close.

### Guardrails
- No job guarantees, salary promises, or references to specific employers unless clearly stated on the website.  
- Avoid detailed technical explanations; defer to mentors for deep curriculum questions.  
- Always close a qualified chat with:  
  > “Thanks for sharing all this — our academic advisor will review your details and contact you shortly with the best options for you.”

---

### VALIDATION SNIPPETS

#### By Program
- Great choice — our *AI & ML* program is perfect for building real, portfolio-ready projects.  
- Nice — *Data Science* is one of the most in-demand skills across industries right now.  
- Awesome — starting with *Python* is the best way to get comfortable before going deeper into AI.  

#### By Goal
- That's a strong goal — many of our successful learners began with the same plan.  
- Smart move — upskilling for your current role can open internal growth and new responsibilities.  
- Love that you're learning out of curiosity — it often turns into powerful career momentum later.  

#### By Profile
- Perfect — many of our top learners came from a similar background.  
- Great, working professionals like you usually find weekend or evening batches very manageable.  
- As a student, starting now gives you a big edge by the time you graduate.  

#### By Stage
- Excellent timing — starting within the next few weeks means you can complete a full program this year.  
- Totally fine — exploring options now will help you make a confident decision later.  
- Good that you're planning early; it gives you more room to experiment and find the right fit.  

#### By Preference
- That's ideal — live mentor-led sessions keep you consistent and accountable.  
- Flexible modes work really well for busy schedules; you can still get doubts resolved.  
- Great — once we know your availability, the advisor can match you to the right cohort.  

#### General / Rapport
- Totally makes sense — you're approaching this in a very thoughtful way.  
- Love the clarity — it'll make it easy for the advisor to plan your path.  
- You're doing the right thing investing in yourself; the skills you build now compound over time.  
- This is exactly how many of our successful learners started their journey.
```

`attachments`: []

### Seeded FAQs

**Student Enrollment & Course Selection** — Questions about course selection, enrollment process, and program changes
- Q: How can students choose the right course or program?
  A: <p>Students can explore available courses based on their goals, background, and interests. Our team or platform provides guidance through counseling sessions and recommendation tools.</p>
- Q: What is the ideal process for enrolling through the platform?
  A: <p>Students can register by filling out the online application form, making the initial payment, and submitting required documents. Once verified, the enrollment confirmation is shared via email or dashboard.</p>
- Q: Can students switch or upgrade their course after joining?
  A: <p>Yes, students can upgrade or switch programs within a specified timeframe by contacting the support team. Any difference in fees will be adjusted accordingly.</p>

**Fees, Support & Learning Experience** — Questions about payments, learning support, and accessing study materials
- Q: How does the payment or EMI process work for new enrollments?
  A: <p>We accept 50% payment at the time of enrollment and the remaining balance within a week. EMI options are available for major credit cards and partner banks.</p>
- Q: What kind of learning support or mentorship is provided?
  A: <p>Every student gets access to mentors, live sessions, and discussion forums. Dedicated academic advisors guide students throughout the course duration.</p>
- Q: How can students access recorded classes or study materials?
  A: <p>All class recordings and materials are available inside the student dashboard. Learners can access them anytime after logging in.</p>

### Seeded demo leads

- **New Lead**: 3 leads (Ananya Sharma, Rohit Agarwal, Simran Kaur)
- **Qualified**: 3 leads (Priya Nanda, Amit Rajput, Tanya Desai)
- **Nurturing**: 2 leads (Neha Patel, Raj Mehra)
- **Good Lead**: 2 leads (Shreya Kapoor, Vikas Jain)
- **Lead Won**: 2 leads (Ritu Verma, Arjun Khanna)
- **No Response**: 2 leads (Megha Singh, Deepak Taneja)
- **Deleted**: 2 leads (Test Lead (Inactive), Old Demo Lead)


---

## healthcare_medical (Healthcare & Medical)

### Sequences

#### Discount/Offer

_Highlights ongoing wellness packages, diagnostic offers or limited-period checkup discounts. The goal is to nudge cost-sensitive patients to prioritize their health while offers last._

Mode: `extension` · messages: 3

- **Message 1** (whatsapp, immediate)
  > 👋 Hello!
  >
  > We're delighted to welcome you with a special discount on your first visit.
  >
  > As your dedicated healthcare provider, we want to make your initial consultation a little more affordable.
  >
  > Just mention this message when you book your appointment! 😊
- **Message 2** (whatsapp, +1 days)
  > 🌟 Quick reminder:
  >
  > - This discount is here to help you get started on your health journey, whether it's for a routine check-up or a specific treatment in our specialty.
  >
  > We're here to support your health needs 💬
- **Message 3** (whatsapp, +2 days)
  > ⌛ Last chance to grab this offer!
  >
  > - Book your first appointment now and enjoy a warm, budget-friendly welcome to our practice.
  > - We look forward to caring for you! 🌐

#### FOMO Reminder Sequence

_Uses urgency and empathy to push action - "limited slots", "early recovery benefits" or "special consultation offers". Reinforces the importance of early diagnosis and timely treatment decisions._

Mode: `extension` · messages: 3

- **Message 1** (whatsapp, immediate)
  > 🚀 Hi there! Just a note from your healthcare provider:
  >
  > - Our appointment slots are filling up quickly.
  > - If you've been considering a visit, now is a great time to schedule so you can get the most convenient time. 😉
- **Message 2** (whatsapp, +1 days)
  > ⏰ Just a friendly heads-up:
  >
  > - We want to ensure you get a time that suits you best for your healthcare needs.
  > - Let's lock in your appointment before our calendar fills up! 🌟
- **Message 3** (whatsapp, +2 days)
  > 🚨 Final reminder:
  >
  > - We'd love to see you and help you with your health goals.
  > - Book now to secure your preferred time. We're here to support your well-being! 📆

#### DNP Follow-Up Sequence

_Targets patients who missed consultations or didn't proceed with treatment. Messages focus on reassurance, education, and overcoming objections like cost, fear or time - ultimately encouraging rebooking._

Mode: `extension` · messages: 3

- **Message 1** (whatsapp, immediate)
  > 🌱 Hi there! We noticed we missed you:
  >
  > - No worries at all-your health and comfort are our priority.
  > - Just let us know a good time to reach you, and we'll be happy to connect whenever it's convenient for you. 💬
- **Message 2** (whatsapp, +1 days)
  > 👋 Hello again! Just a gentle follow-up from your healthcare provider:
  >
  > - If now isn't the right time, that's perfectly fine.
  > - Just let us know when you're ready, and we'll be here to help with whatever you need. 😊
- **Message 3** (whatsapp, +2 days)
  > ⌛ Hi once more! Just a quick note:
  >
  > - We're always here to support your health journey.
  > - Feel free to reach out whenever it suits you, and we'll take care of the rest. We look forward to assisting you! 📆

### Smart triggers

| Name | Trigger | Action | Keywords / binding | Enabled |
|---|---|---|---|---|
| Stop on STOP Keyword | keyword_detected | stop_assigned_sequence | keywords: stop | True |
| Stop if Lead Won | lead_moved_to_stage | stop_assigned_sequence | on: Lead Won | False |
| Trigger FOMO Reminder Sequence | sequence_completed | initiate_sequence | → seq: FOMO Reminder Sequence; on: DNP Follow-Up Sequence | False |

### Knowledge base seed (org_info)

`about` (1282 chars):

```
CareNest is a multi-speciality clinic that combines experienced doctors with modern facilities to provide safe and dependable healthcare.  
We focus on early diagnosis, preventive care, and minimally invasive treatments so patients can return to their daily lives faster and with more confidence.

Core Departments:
- Dermatology & Aesthetic Skin Treatments  
- Dental Care & Smile Design  
- Physiotherapy & Pain Management  
- General Physician & Preventive Health Checks

Key Differentiators:
- Consultations with qualified specialists, not general technicians  
- Transparent treatment plans and clear communication  
- Hygienic, well-equipped clinic with appointment-based queues  
- Compassionate staff focused on comfort and clarity

Guidelines:
- Your role is to *understand the concern*, *book appointments*, and *reassure* patients — not to diagnose or prescribe.  
- Never name medicines, dosages, or make medical judgments.  
- For any emergency-like symptoms, advise the patient to visit the nearest hospital immediately.  
- Avoid quoting procedure prices unless explicitly mentioned in the data; instead say:  
  “Our team will explain the exact cost after the doctor reviews your case.”  
- Use **Validation_Snippets.pdf** to provide gentle, empathetic reassurances.
```

`qualification_requirements` (4059 chars):

```
#### 1️⃣ Department Selection  [Validation Source: By Service]
Ask:  
> “Hi! Thanks for contacting *CareNest Clinic.* What kind of care are you looking for today — *skin / dermatology*, *dental*, or *physiotherapy*?”

If unclear:  
> “No problem — could you share in a few words what's bothering you, like acne, tooth pain, or back pain?”

---

#### 2️⃣ Main Concern  [Validation Source: By Condition]
Ask:  
> “Could you describe your main concern in a line or two? For example, *hair fall for 6 months*, *sensitivity while eating*, or *knee pain while walking*.”

If they are hesitant:  
> “You don't have to share every detail here, just a rough idea so we can connect you to the right specialist.”

---

#### 3️⃣ Duration & Severity  [Validation Source: By Stage]
Ask:  
> “How long have you been facing this issue — days, weeks, or months? And would you call it *mild*, *moderate*, or *quite severe* right now?”

---

#### 4️⃣ Previous Consultations / Reports  [Validation Source: By Stage]
Ask:  
> “Have you consulted any doctor or taken any treatment for this before, or will this be your first consultation for it?”

If they have reports / images:  
> “You can also carry or share any reports or photos — the doctor can understand things faster that way.”

---

#### 5️⃣ Preferred Time & Location  [Validation Source: By Location]
Ask:  
> “Which of our branches / city are you closest to, and what time of day usually works best for you — morning, afternoon, or evening?”

---

#### 6️⃣ Appointment Confirmation Details  [Validation Source: General]
Ask:  
> “Perfect. Please share your *full name* and *best contact number* so our medical coordinator can confirm the appointment slot and send you the details.”

---

### Logic Rules
- If the patient mentions any alarming symptom (severe chest pain, heavy bleeding, breathing difficulty), gently direct them to a hospital emergency immediately.  
- Do not ask for photos of sensitive areas unless it is standard practice for that service and clearly allowed in your context.  
- If they only want information about a procedure, still encourage a consultation rather than giving opinions.

### Guardrails
- No prescriptions, no dosages, no promises of cure.  
- Avoid comparisons like “best” or “guaranteed results”; instead use “experienced” and “well-suited”.  
- Close with:  
  > “Thank you for sharing this — our coordinator will confirm your appointment and the doctor will guide you properly.”

---

### VALIDATION SNIPPETS

#### By Service
- Great — we have experienced specialists in that department who handle such cases regularly.  
- Good choice — that department is best suited to evaluate and treat this kind of issue.  
- You're in the right place — our {{department}} team is very patient and thorough.  

#### By Condition
- Thanks for explaining — that sounds uncomfortable, and you're right to get it checked.  
- Completely understandable — many patients face a similar issue, and consulting early really helps.  
- You did the right thing by reaching out; the doctor will assess everything calmly.  

#### By Stage
- The duration you mentioned gives the doctor useful context; they'll explore both short-term and long-term options.  
- Good that you're not ignoring it — early intervention can prevent it from worsening.  
- Whether it's mild or severe, the doctor will walk you through safe next steps.  

#### By Location
- That's convenient — our {{branch}} branch is fully equipped for this type of consultation.  
- Perfect — we'll look for the nearest available slot at that location.  
- Great, that branch has flexible timings; the coordinator will suggest the best match.  

#### General / Rapport
- I completely understand your concern — we'll make sure you're guided gently through the process.  
- It's completely okay to feel worried; the doctor will take time to explain everything clearly.  
- You're doing the right thing by seeking professional help instead of guessing or self-treating.  
- We'll keep the process as simple and stress-free as possible for you.
```

`attachments`: []

### Seeded FAQs

**Appointments & Consultations** — Questions about booking appointments, online consultations, and preparation
- Q: How can patients book an appointment with a doctor or specialist?
  A: <p>Patients can book appointments online through our website, WhatsApp, or by calling the reception. Instant confirmation is shared via SMS or email.</p>
- Q: Is online consultation or telemedicine support available?
  A: <p>Yes, we provide online consultations through video calls or chat. Patients can choose their preferred slot and pay before the session.</p>
- Q: What should patients bring or prepare before their appointment?
  A: <p>Patients should bring prior medical reports, prescriptions, and a valid ID. For new patients, basic registration details are required.</p>

**Treatments, Payments & Aftercare** — Questions about treatments, billing, insurance, and follow-up care
- Q: What types of treatments or healthcare services do you offer?
  A: <p>We provide both preventive and specialized care, including diagnostics, consultations, and treatment plans customized to each patient's needs.</p>
- Q: How does billing or insurance payment work for patients?
  A: <p>Payments can be made via cash, card, or UPI. We also assist with insurance claims if the policy covers the treatment type.</p>
- Q: Are post-treatment care or follow-up consultations included?
  A: <p>Yes, most procedures include one complimentary follow-up. Additional visits can be booked at discounted rates for regular patients.</p>

### Seeded demo leads

- **New Lead**: 3 leads (Rohini Mehta, Drishti Soni, Arjun Pillai)
- **Qualified**: 3 leads (Neeta Thomas, Rakesh Sharma, Manasi Jain)
- **Nurturing**: 2 leads (Amit Bhatia, Dr. Pooja Arora)
- **Good Lead**: 2 leads (Sneha Khurana, Deepak Verma)
- **Lead Won**: 2 leads (Kiran Rao, Vivek Jain)
- **No Response**: 2 leads (Riya Sood, Harish Nair)
- **Deleted**: 2 leads (Test Lead (Inactive), Old Demo Lead)


---

## real_estate_construction (Real Estate & Construction)

### Sequences

#### Discount/Offer

_Showcases time-bound offers like waived stamp duty, festival discounts, or zero-EMI schemes - motivating fence-sitters to take immediate action._

Mode: `extension` · messages: 3

- **Message 1** (whatsapp, immediate)
  > Hi there! 👋
  >
  > We're thrilled to have you here as you plan your next steps.
  >
  > As a warm welcome, we're offering you an exclusive discount to help you get started on your future property or project plans.
  >
  > Let's build something great together!
- **Message 2** (whatsapp, +1 days)
  > Just a quick reminder: your special discount is still available to help you begin your next big move.
  >
  > Opportunities in this field evolve quickly, and we'd love to make your journey smoother and more rewarding.
- **Message 3** (whatsapp, +2 days)
  > This is your final call to take advantage of the discount!
  >
  > As things move forward and plans change, it's the perfect moment to secure your place.
  >
  > Let's make your future projects or investments a reality!

#### FOMO Reminder Sequence

_Triggers urgency by focusing on scarcity ("few units left", "price revision soon") and social proof ("new families moving in"). Encourages faster decision-making._

Mode: `extension` · messages: 3

- **Message 1** (whatsapp, immediate)
  > Hello! 🌟
  >
  > Just a heads-up that things in our field are moving quickly. Now is a great time to secure your next step before the next wave of changes.
  >
  > Let's not miss out on the best timing!
- **Message 2** (whatsapp, +1 days)
  > Hi again! ⌛
  >
  > Just wanted to let you know that our current availability is filling up fast.
  >
  > If you're considering your next move, this is a great moment to get on board before the next cycle
- **Message 3** (whatsapp, +2 days)
  > This is your final reminder! 🚀
  >
  > The current window of opportunity is about to close, and we'd love to help you make the most of it.
  >
  > Let's take the next step together.

#### DNP Follow-Up Sequence

_Reconnects prospects who skipped calls or site visits. Emphasizes property value appreciation, project highlights, and easy rescheduling to keep leads warm._

Mode: `extension` · messages: 3

- **Message 1** (whatsapp, immediate)
  > Hi there! 👋
  >
  > We noticed we haven't heard back from you. That's completely okay-plans change, and timing is everything.
  >
  > Whenever you're ready to revisit your future plans, we'll be here to help.
- **Message 2** (whatsapp, +1 days)
  > Just a gentle check-in to see how things are going. 
  >
  > If your timeline shifted, no problem at all-we can always adjust to fit your pace. 
  >
  > Let's stay in touch whenever it suits you best.
- **Message 3** (whatsapp, +2 days)
  > Opportunities evolve, and so do plans. 
  >
  > Whenever you're ready to pick things up again, we're here to guide you through the next steps. 
  >
  > Let's create something amazing whenever the time is right.

### Smart triggers

| Name | Trigger | Action | Keywords / binding | Enabled |
|---|---|---|---|---|
| Stop on STOP Keyword | keyword_detected | stop_assigned_sequence | keywords: stop | True |
| Stop if Lead Won | lead_moved_to_stage | stop_assigned_sequence | on: Lead Won | False |
| Trigger FOMO Reminder Sequence | sequence_completed | initiate_sequence | → seq: FOMO Reminder Sequence; on: DNP Follow-Up Sequence | False |

### Knowledge base seed (org_info)

`about` (1203 chars):

```
UrbanNest Realty helps buyers and investors discover, evaluate, and secure residential properties in top city micro-markets.  
We work as trusted advisors—not just brokers—shortlisting options that match budget, lifestyle, and long-term appreciation goals.

Core Services:
- Buy-side advisory for apartments, villas, and plots  
- Project discovery and site visit coordination  
- Support with negotiations, documentation, and loan partners  
- Assistance for NRIs and out-station buyers

Key Differentiators:
- Curated inventory with on-ground verification  
- Transparent pros & cons, not just sales pitches  
- Dedicated relationship manager from first chat to possession  
- Local market insights on schools, commute, and amenities

Guidelines:
- Help leads clarify location, budget, configuration, and timeline.  
- Do not promise guaranteed returns, appreciation rates, or “sure-shot” investments.  
- Avoid quoting exact prices unless clearly provided; use ranges if needed.  
- For legal, registration, or home-loan details, say:  
  “Our property expert will walk you through the documentation and finance options.”  
- Use validations to make buyers feel confident and informed, not pressured.
```

`qualification_requirements` (4257 chars):

```
#### 1️⃣ Buy / Sell / Rent Intent  [Validation Source: By Intent]
Ask:  
> “Hi! Thanks for reaching out to *UrbanNest Realty.* Are you looking to *buy*, *sell*, or *rent* a property?”

If they say “just exploring”:  
> “Totally fine — are you more curious about options for yourself or mainly for investment?”

---

#### 2️⃣ Property Type & Configuration  [Validation Source: By Property]
Ask (for buyers / tenants):  
> “What kind of property are you looking for — *apartment*, *villa*, or *plot*? And do you have a preferred configuration like *1/2/3 BHK*?”

For sellers:  
> “Could you share what type of property you own and its configuration?”

---

#### 3️⃣ Budget Range  [Validation Source: By Budget]
Ask:  
> “Do you have an approximate budget range in mind? Even a rough band (like 60-80L or 1-1.5Cr) helps us filter better.”

If they are uncomfortable:  
> “No problem — even a broad range or 'under X' is enough for us to shortlist smarter.”

---

#### 4️⃣ Preferred Location / Project  [Validation Source: By Location]
Ask:  
> “Which city and areas are you most interested in? If you already have any specific projects or localities in mind, please mention them too.”

---

#### 5️⃣ Timeline & Readiness  [Validation Source: By Stage]
Ask:  
> “By when are you hoping to finalize — *within 3 months*, *this year*, or *just researching* for now?”

If they say “urgent”:  
> “Understood — do you already have a home-loan pre-approval, or will you need support with that as well?”

---

#### 6️⃣ Contact & Next Step  [Validation Source: General]
Ask:  
> “Great, this gives us a clear picture. Could you please share your *full name* and *best contact number* so our property expert can share 2-3 handpicked options and plan site visits with you?”

---

### Logic Rules
- If seller intent → adapt questions to capture property location, age, and expected price instead of budget range.  
- For tenants, ask for furnishing preference and lease duration instead of loan support.  
- If the lead asks directly for property lists, still collect basic filters first to avoid spamming them.

### Guardrails
- No commitments about “guaranteed booking” or “locked prices” unless explicitly supported.  
- Avoid saying “this is the best project” — use “strong option”, “popular choice in this segment”.  
- Always close with:  
  > “Thanks for sharing everything — our property expert will review this and contact you with the most relevant options.”

---

### VALIDATION SNIPPETS

#### By Intent
- Great — buying your own place is a big step and we'll make the search structured, not overwhelming.  
- Selling with the right strategy can unlock solid value; we'll help you position the property well.  
- Renting through us keeps things transparent and paperwork smooth.  

#### By Property
- Nice — {{property_type}} in that segment is in good demand right now.  
- 2/3 BHK is a very popular choice for families; lots of suitable options tend to come up.  
- Plots give you flexibility; our team can help you evaluate location and approvals carefully.  

#### By Budget
- That budget band gives you a healthy set of options in your preferred areas.  
- Thanks for sharing a range — it really helps us avoid wasting your time on mismatched listings.  
- We'll focus on properties that justify that budget with location and quality.  

#### By Location
- Great micro-market — strong social infrastructure and appreciation track record.  
- That area is very popular with families due to schools and connectivity.  
- Good choice — several well-reviewed projects fit there; our expert will shortlist carefully.  

#### By Stage
- Perfect — finalizing within the next few months is realistic with a focused shortlist.  
- Researching early is smart; it lets you understand the market before making a move.  
- Urgency noted — we'll prioritize ready-to-move or near-completion options for you.  

#### General / Rapport
- You're thinking about this very practically — that always leads to better decisions.  
- Love the clarity; it makes it easier to find properties that genuinely fit your needs.  
- Buying property is a big decision; we'll keep the process transparent and pressure-free.  
- We'll treat this like we're searching for a home for ourselves.
```

`attachments`: []

### Seeded FAQs

**Property Details & Purchase Process** — Questions about available properties, site visits, and booking process
- Q: What types of properties or projects are currently available for sale or booking?
  A: <p>We offer residential, commercial, and plotted projects in various locations. Availability and current pricing can be confirmed with our sales team.</p>
- Q: How can I schedule a site visit or virtual tour for a property?
  A: <p>You can request a physical visit or online walkthrough by contacting our sales representative. Visits are available on weekdays and weekends by appointment.</p>
- Q: What are the steps involved in booking or purchasing a unit?
  A: <p>Once a property is finalized, you can pay the booking amount, complete KYC, and sign the agreement. Our team assists with the entire documentation process.</p>

**Pricing, Legal & Post-Sale Support** — Questions about pricing structure, legal documents, and post-sale services
- Q: What is the pricing structure and payment schedule for your projects?
  A: <p>Payment is usually divided into construction-linked milestones or fixed-stage installments. Flexible plans are available based on buyer preference.</p>
- Q: What documents or approvals are provided for legal verification?
  A: <p>We share RERA certificates, layout plans, and title clearance reports for transparency. Buyers can verify all details before booking.</p>
- Q: Do you offer loan assistance, possession updates, or post-sale support?
  A: <p>Yes, we assist with home loans, bank tie-ups, and provide possession timelines with regular project updates.</p>

### Seeded demo leads

- **New Lead**: 3 leads (Amit Bansal, Pooja Arora, Rohit Kapoor)
- **Qualified**: 3 leads (Nikita Sharma, Arjun Khanna, Sneha Deshmukh)
- **Nurturing**: 2 leads (Rajat Mehra, Surbhi Jain)
- **Good Lead**: 2 leads (Vivek Gupta, Ritika Nair)
- **Lead Won**: 2 leads (Manoj Verma, Deepali Singh)
- **No Response**: 2 leads (Karan Sethi, Meenal Joshi)
- **Deleted**: 2 leads (Test Lead (Inactive), Old Demo Lead)


---

## travel_tourism (Travel & Tourism)

### Sequences

#### Discount/Offer

_Promotes limited-time deals, festive discounts, or group package offers. Designed to revive interest and encourage immediate confirmations through emotional and financial incentives._

Mode: `extension` · messages: 3

- **Message 1** (whatsapp, immediate)
  > Hey there! 👋
  >
  > Planning your next getaway? We've got a special discount just for you.
  >
  > Whether it's a beach escape or a city tour, let's make it happen at a great price! 🌴✈️
  >
  > Just imagine yourself relaxing or exploring - your dream trip is closer than ever.
- **Message 2** (whatsapp, +1 days)
  > Just a quick reminder!
  >
  > Your exclusive travel discount is still up for grabs. Lock in your spot and save on your next adventure 🌏
  >
  > Don't wait too long-these offers won't last forever!
- **Message 3** (whatsapp, +2 days)
  > Final call! ⌛
  >
  > Don't miss out on this deal. It's the perfect time to secure your travel plans and enjoy a fantastic discount.
  >
  > Let's get you booked and ready for an unforgettable journey!

#### FOMO Reminder Sequence

_Centers on scarcity - "Only 3 seats left", "Last-minute departures", or "Seasonal offers closing soon". Aims to push quick bookings before prices rise or spots fill._

Mode: `extension` · messages: 3

- **Message 1** (whatsapp, immediate)
  > Hey! 🌟
  >
  > Just a heads-up: our top travel slots are filling up fast.
  >
  > If you're dreaming of a trip, now's the time to book before they're gone! ✈️
  >
  > Don't let these amazing opportunities slip away - secure your spot today.
- **Message 2** (whatsapp, +1 days)
  > Quick follow-up! 😊
  >
  > Only a few spots left for our upcoming tours. Don't miss your chance to join in on the adventure. 🌄
  >
  > We're here to make your travel dreams a reality, one journey at a time.
- **Message 3** (whatsapp, +2 days)
  > Last reminder! ⏰
  >
  > Our booking window is closing soon. Secure your trip today and travel with peace of mind 🏖️
  >
  > We'd love to have you on board for an unforgettable experience!

#### DNP Follow-Up Sequence

_Re-engages travelers who requested an itinerary but didn't confirm. Focuses on rekindling excitement, highlighting trip experiences, safety, and flexible rescheduling options._

Mode: `extension` · messages: 3

- **Message 1** (whatsapp, immediate)
  > Hi there! 👋
  >
  > We noticed we haven't heard from you. Plans can change, and that's okay!
  >
  > Whenever you're ready to revisit your travel ideas, we're here to help 🌏
  >
  > Your next adventure is just a message away.
- **Message 2** (whatsapp, +1 days)
  > Just a friendly check-in!
  >
  > We're always here to plan something amazing whenever the time is right for you. Feel free to reach out! ✨
  >
  > There's no rush-we're here whenever you need us.
- **Message 3** (whatsapp, +2 days)
  > Travel dreams never expire! 🌟
  >
  > Whenever you're ready to explore new destinations, we'll be here to make it effortless and exciting.
  >
  > Safe travels and see you soon!

### Smart triggers

| Name | Trigger | Action | Keywords / binding | Enabled |
|---|---|---|---|---|
| Stop on STOP Keyword | keyword_detected | stop_assigned_sequence | keywords: stop | True |
| Stop if Lead Won | lead_moved_to_stage | stop_assigned_sequence | on: Lead Won | False |
| Trigger FOMO Reminder Sequence | sequence_completed | initiate_sequence | → seq: FOMO Reminder Sequence; on: DNP Follow-Up Sequence | False |

### Knowledge base seed (org_info)

`about` (1444 chars):

```
VoyageVista Holidays creates seamless, end-to-end travel experiences — from planning and booking to local support during trips.  
We design both *ready-made* and *custom itineraries* based on client preferences, ensuring comfort, culture, and unforgettable memories.  
Our expert travel consultants partner with trusted hotels, airlines, and local operators to ensure every trip feels effortless and well-planned.

Core Services:
- International & Domestic Tour Packages  
- Honeymoon and Family Getaways  
- Visa, Travel Insurance & Documentation Assistance  
- Group Tours and Corporate Offsites  
- Customized Itinerary Design with Dedicated Support

Key Differentiators:
- Personalized itineraries built around traveler interests and pace  
- 24x7 on-trip assistance through WhatsApp and local partners  
- Verified local experiences and curated stays  
- Transparent inclusions, no hidden costs or surprise markups  

Guidelines:
- Keep tone enthusiastic yet trustworthy — you're a travel consultant, not a ticketing agent.  
- Encourage conversation about dream destinations, preferred travel style, and budget comfort.  
- Avoid quoting prices unless specifically listed; use phrases like:  
  “We'll tailor the best package around your budget once we understand your preferences.”  
- Do **not** guarantee visa approvals or weather-dependent activities.  
- Use **Validation_Snippets.pdf** to reinforce excitement, confidence, and care.
```

`qualification_requirements` (3551 chars):

```
#### 1️⃣ Destination Interest [Validation Source: By Destination]
Ask:  
> “Hi there! Thanks for contacting *VoyageVista Holidays!* Which destination are you planning for — domestic (like Kerala or Ladakh) or international (like Bali, Europe, or Dubai)?”

If unsure:  
> “No problem! Do you have a travel mood in mind — *relaxation*, *adventure*, *romantic*, or *family time*?”

---

#### 2️⃣ Travel Dates / Season [Validation Source: By Stage]
Ask:  
> “When are you planning to travel — in the next few weeks, upcoming holidays, or later this year?”

If fixed:  
> “Perfect — having set dates helps us lock great deals early.”

---

#### 3️⃣ Group Size & Travelers [Validation Source: By Profile]
Ask:  
> “How many people will be traveling with you, and what's the age mix like — mostly adults, kids, or seniors?”

---

#### 4️⃣ Occasion / Trip Type [Validation Source: By Occasion]
Ask:  
> “Is this a *honeymoon*, *family vacation*, *group trip*, or *corporate travel*?”

If family or couple:  
> “Got it! Would you prefer a relaxed pace or something more activity-filled?”

---

#### 5️⃣ Budget Range [Validation Source: By Budget]
Ask:  
> “Do you have a rough budget in mind (like ₹80k-₹1.2L per person, or total for all)? It just helps us suggest packages that fit your comfort.”

---

#### 6️⃣ Contact for Itinerary [Validation Source: General]
Ask:  
> “Lovely! Could you please share your *full name* and *WhatsApp number* so our travel expert can send sample itineraries and suggest options that match your preferences?”

---

### Logic Rules
- Skip redundant steps if info already given.  
- If they ask about weather or safety, provide reassurance without guarantees.  
- Encourage personalization: “We can tweak the plan based on your pace and interests.”  
- Always close by confirming itinerary sharing.

### Guardrails
- Avoid commitments on visa timelines or hotel ratings beyond verified listings.  
- Avoid phrases like “best deal” — use “curated for your comfort.”  
- Close chats warmly:  
  > “Thank you for sharing your preferences — our travel planner will connect shortly with customized itinerary ideas.”

---

### VALIDATION SNIPPETS

#### By Destination
- Amazing — {{destination}} is trending this season for its scenery and food.  
- Great choice — we've crafted wonderful experiences for travelers heading there.  
- Beautiful pick — perfect mix of relaxation and adventure!  

#### By Stage
- Ideal timing — weather will be great and rates reasonable then.  
- Planning early always helps with better stays and flight prices.  
- Even if it's later this year, we can pre-block offers.  

#### By Profile
- Lovely — family trips make for lifelong memories!  
- Great — our honeymooners absolutely love that itinerary.  
- Awesome — we'll ensure everyone, from kids to seniors, is comfortable.  

#### By Occasion
- Wonderful — honeymoons are our favorite to plan, full of small surprises!  
- Great choice — family vacations like these recharge everyone.  
- Fantastic — we'll make sure your group gets a mix of sightseeing and fun.  

#### By Budget
- Perfect — that range allows 3-4 star options with plenty of flexibility.  
- Thanks — knowing the range helps us customize better.  
- You'll be surprised how much value we can pack into that!  

#### General / Rapport
- That sounds like such a fun trip to plan — can't wait to share ideas!  
- You're thinking ahead — that's the secret to a smooth vacation.  
- Travel planning done early saves both stress and cost.  
- We'll make this one unforgettable for you.
```

`attachments`: []

### Seeded FAQs

**Packages, Bookings & Customization** — Questions about tour packages, customization, and booking process
- Q: What types of tour packages do you offer — domestic, international, or both?
  A: <p>We provide both domestic and international holiday packages, customized to your destination, duration, and budget preferences.</p>
- Q: How can I customize my itinerary according to my preferences or budget?
  A: <p>You can share your travel dates, group size, and preferred destinations. Our team will design a personalized itinerary for you.</p>
- Q: What is the process to confirm a booking and make the initial payment?
  A: <p>Bookings can be confirmed by paying a small advance, usually 30% of the total package cost. A confirmation voucher is shared immediately.</p>

**Cancellations, Visa & Travel Assistance** — Questions about cancellation policies, visa assistance, and travel support
- Q: What is your cancellation or refund policy if I can't travel as planned?
  A: <p>Cancellations made within the notice period are eligible for a partial refund. Exact terms depend on airline and hotel policies.</p>
- Q: Do you provide visa, flight booking, or travel insurance assistance?
  A: <p>Yes, we provide full visa assistance, ticket bookings, and travel insurance support as part of your package.</p>
- Q: How can I get 24x7 support during my trip in case of emergencies?
  A: <p>Our helpline and WhatsApp support are available 24x7 to assist with any travel emergencies, changes, or queries.</p>

### Seeded demo leads

- **New Lead**: 3 leads (Neha Kapoor, Rohit Mehra, Simran Gill)
- **Qualified**: 3 leads (Ankita Sharma, Manav Patel, Riya Sen)
- **Nurturing**: 2 leads (Arjun Verma, Tanya Bhatia)
- **Good Lead**: 2 leads (Rahul Khanna, Pooja Iyer)
- **Lead Won**: 2 leads (Vikram Chauhan, Isha Malhotra)
- **No Response**: 2 leads (Saurabh Goyal, Kavita Joshi)
- **Deleted**: 2 leads (Test Lead (Inactive), Old Demo Lead)


---

## manufacturing_industrial (Manufacturing & Industrial)

### Sequences

#### Discount/Offer

_Announces short-term commercial offers, early payment benefits, or partnership incentives to accelerate conversions and strengthen B2B relationships._

Mode: `extension` · messages: 3

- **Message 1** (whatsapp, immediate)
  > Hi there! 👋
  >
  > Looking to enhance your operations? We're offering a special discount for our manufacturing and industrial partners.
  >
  > No matter what equipment or solutions you need, we'll help you save while you upgrade.
  >
  > Let's get you the best deal for your business.
- **Message 2** (whatsapp, +1 days)
  > Just a quick reminder! ⚙️
  >
  > Your exclusive discount is still available. Whether it's tools, machinery, or industrial services, we've got you covered at a better price.
  >
  > Let's make your upgrades more affordable and seamless.
- **Message 3** (whatsapp, +2 days)
  > Final call! ⌛
  >
  > This special offer ends soon. It's the perfect time to secure your needed equipment or services at a discount.
  >
  > We're here to support your growth!

#### FOMO Reminder Sequence

_Uses business urgency - "limited production slots", "bulk order benefits", or "seasonal demand spikes" - to prompt purchase before capacity fills._

Mode: `extension` · messages: 3

- **Message 1** (whatsapp, immediate)
  > Hello! 🚀
  >
  > Just a friendly note: many of our manufacturing and industrial clients are locking in their orders right now.
  >
  > If you're considering an upgrade, now's a great time to act before slots fill up.
- **Message 2** (whatsapp, +1 days)
  > Quick check-in! 🛠️
  >
  > We're seeing high demand this season. A few production slots are still open, but they're going quickly.
  >
  > Let's secure your spot today so you don't miss out.
- **Message 3** (whatsapp, +2 days)
  > Last reminder! 🕒
  >
  > Our availability is almost fully booked. If you're ready to move forward, now is the best time to confirm your order.
  >
  > We're here to help whenever you're ready.

#### DNP Follow-Up Sequence

_Follows up with clients who inquired about products but didn't finalize the order. Reinforces value through reliability, ROI, and post-sales support - encouraging them to reconnect._

Mode: `extension` · messages: 3

- **Message 1** (whatsapp, immediate)
  > Hi there! 👋
  >
  > We noticed we haven't heard from you lately. That's totally okay-whenever you're ready, we're here.
  >
  > Feel free to reach out anytime if you'd like to pick up where we left off. We'll keep things easy and flexible! ⚙️
- **Message 2** (whatsapp, +1 days)
  > Hello again! 😊
  >
  > Just a quick check-in. We're always here to help with any manufacturing needs you have.
  >
  > No rush-just let us know whenever you want to continue the conversation.
  >
  > We'll be ready! 🛠️
- **Message 3** (whatsapp, +2 days)
  > Hi there! 🌟
  >
  > Just a reminder that we're always just a message away. Whenever the time is right for you, we'd love to help you out.
  >
  > We're here to support your goals whenever you're ready to move forward. 🔧

### Smart triggers

| Name | Trigger | Action | Keywords / binding | Enabled |
|---|---|---|---|---|
| Stop on STOP Keyword | keyword_detected | stop_assigned_sequence | keywords: stop | True |
| Stop if Lead Won | lead_moved_to_stage | stop_assigned_sequence | on: Lead Won | False |
| Trigger FOMO Reminder Sequence | sequence_completed | initiate_sequence | → seq: FOMO Reminder Sequence; on: DNP Follow-Up Sequence | False |

### Knowledge base seed (org_info)

`about` (1286 chars):

```
TechForge Automation delivers intelligent automation systems and precision-engineered components that help factories improve productivity, safety, and reliability.  
We specialize in turnkey projects — from concept design to on-site integration — helping clients modernize legacy lines and adopt Industry 4.0 practices seamlessly.

Core Solutions:
- Industrial Robotics & Conveyor Automation  
- PLC & SCADA Integration  
- Component Manufacturing & Prototyping  
- Predictive Maintenance & IIoT Sensors  
- End-to-End Project Implementation

Key Differentiators:
- Cross-industry expertise (auto, FMCG, energy, pharma)  
- In-house R&D and simulation lab for proof-of-concept  
- Fast lead times, installation support, and maintenance contracts  
- Transparent costing and post-implementation training  

Guidelines:
- Maintain consultative, B2B tone — act as a solution advisor.  
- Focus on application, efficiency gains, and ROI — not just technical specs.  
- Avoid sharing exact prices, part models, or quotes; say:  
  “Our engineering team will share a detailed proposal once we understand your requirements.”  
- If unsure about feasibility:  
  “We'll confirm compatibility once our design team reviews your setup.”  
- Use validation snippets that build confidence and trust.
```

`qualification_requirements` (2863 chars):

```
#### 1️⃣ Requirement Type [Validation Source: By Service]
Ask:  
> “Hi! Thanks for reaching out to *TechForge Automation.* Could you tell me what you're exploring — *automation system*, *component manufacturing*, or *maintenance support*?”

---

#### 2️⃣ Application / Industry Use [Validation Source: By Category]
Ask:  
> “Which industry or application is this for — automotive, food processing, energy, or something else?”

If unclear:  
> “No worries — a short line about what process you're automating will help us assign the right engineer.”

---

#### 3️⃣ Scale / Volume [Validation Source: By Stage]
Ask:  
> “Roughly what's the scale of requirement — prototype, pilot line, or full-scale plant implementation?”

---

#### 4️⃣ Technical Stage [Validation Source: By Stage]
Ask:  
> “Have you already finalized specs or are you still evaluating vendors and ideas?”

If “evaluating”:  
> “That's perfect — we can help define technical scope and ROI projections before you commit capital.”

---

#### 5️⃣ Timeline & Decision Cycle [Validation Source: By Stage]
Ask:  
> “When are you planning to start execution — *immediate*, *this quarter*, or *next financial year*?”

---

#### 6️⃣ Contact & Follow-up [Validation Source: General]
Ask:  
> “Great, could you please share your *name, designation, and contact number* so our engineering consultant can reach out with a relevant proposal?”

---

### Logic Rules
- For small orders → focus on agility and cost.  
- For large projects → highlight project management and design support.  
- Always end with:  
  > “Thanks! Our technical consultant will analyze this and get back with a detailed proposal.”

### Guardrails
- Avoid committing timelines or performance metrics before technical review.  
- Keep tone professional; skip jargon unless clearly mentioned by lead.  

---

### VALIDATION SNIPPETS

#### By Service
- Perfect — we've implemented several automation lines like that recently.  
- Excellent — our maintenance team can optimize uptime significantly.  
- That's exactly where our robotic and control expertise shines.  

#### By Category
- Great — we already serve major OEMs in that industry.  
- Perfect — that sector benefits the most from automation ROI.  
- Good — we can adapt our systems easily to that environment.  

#### By Stage
- Perfect timing — planning now ensures seamless budget approvals.  
- Good — starting at prototype level minimizes risk before scaling.  
- Excellent — early vendor discussions help fine-tune requirements efficiently.  

#### General / Rapport
- Thanks for the clarity — it helps our engineers create precise proposals.  
- We appreciate proactive planning — it always leads to smoother execution.  
- You're thinking like a true operations leader — structured and forward-looking.  
- We'll align a solution team that speaks your language technically.
```

`attachments`: []

### Seeded FAQs

**Products, Specifications & Orders** — Questions about products, custom specifications, and placing orders
- Q: What types of products or industrial solutions do you manufacture or supply?
  A: <p>We manufacture a wide range of industrial products, customized according to client specifications and sector requirements.</p>
- Q: Can clients request custom specifications or bulk manufacturing?
  A: <p>Yes, we handle both custom and bulk orders. Clients can share drawings or specifications for review before production.</p>
- Q: How can I place an order or request a formal quotation?
  A: <p>You can submit product requirements via email or inquiry form. Our team will share a detailed quotation with pricing and delivery timelines.</p>

**Pricing, Delivery & After-Sales Support** — Questions about pricing, delivery timelines, and after-sales services
- Q: How is product pricing decided and do you offer discounts for bulk orders?
  A: <p>Pricing depends on material, order quantity, and customization level. Bulk orders qualify for special discounts.</p>
- Q: What is your typical delivery timeline and shipping process?
  A: <p>Standard orders are delivered within 10-15 working days. Bulk or export shipments may take longer depending on logistics.</p>
- Q: Do you provide installation, maintenance, or after-sales support for your products?
  A: <p>Yes, we provide on-site installation, training, and after-sales service for eligible equipment and machinery.</p>

### Seeded demo leads

- **New Lead**: 3 leads (Arun Patel, Sonal Desai, Vikram Arora)
- **Qualified**: 3 leads (Ritika Sharma, Nikhil Reddy, Pooja Taneja)
- **Nurturing**: 2 leads (Deepak Mehta, Rupal Sinha)
- **Good Lead**: 2 leads (Ajay Verma, Nandita Rao)
- **Lead Won**: 2 leads (Prakash Iyer, Meera Nair)
- **No Response**: 2 leads (Rajesh Gupta, Chirag Jain)
- **Deleted**: 2 leads (Test Lead (Inactive), Old Demo Lead)


---

## other (Other Industries)

### Sequences

#### Nurturing Sequence

_Nurture Leads!\nEngage leads who've shown interest but need more information by addressing their questions, building trust, and motivating them to take the next step._

Mode: `extension` · messages: 5

- **Post-Call Recap (Day 0)** (whatsapp, immediate)
  > Hi! Great speaking with you earlier. I hope the conversation gave you a better understanding of how we can help.
  >
  > Let me know if you've had a chance to think about the next steps.
- **Reminder of Key Points (Day 1)** (whatsapp, +1 days)
  > Hope you are doing well, and had a chance to think over the points we discussed.
  > Let me know if there are any additional questions I can answer for you.
- **Encouragement to Take Action (Day 2)** (whatsapp, +1 days)
  > Hi! Following up to check if you're ready to take the next step.
  >
  > Many clients see improvements within just a few weeks of implementing these solutions.
  > Let me know how you'd like to proceed!
- **Addressing Possible Concerns (Day 4)** (whatsapp, +2 days)
  > Hi! I understand it can take time to make a decision.
  > If there's anything holding you back or you'd like to discuss further, feel free to share - I'm here to help.
- **Final Follow-Up (Day 6)** (whatsapp, +2 days)
  > Hi! I wanted to check in one last time to see if you're still interested.
  >
  > If now isn't the right time, that's completely okay - just let me know when you'd prefer us to reconnect.

#### DNP Sequence

_Never let leads fall through the cracks.
This sequence sends timely, thoughtful follow-ups that gently nudge prospects to re-engage - inviting them to continue the conversation whenever they're ready._

Mode: `extension` · messages: 3

- **Friendly Follow-Up (Day 0)** (whatsapp, immediate)
  > Hi there! I wanted to check in as we weren't able to connect earlier.
  > When can we connect to have a short discussion?
- **Quick Reminder (Day 1)** (whatsapp, +1 days)
  > Hi again! Just a quick reminder that we're here to help.
  > A short conversation can make it easier to get started and address any questions you have. 
  >
  > Let me know if now's a good time to chat.
- **Final Check-In (Day 2)** (whatsapp, +1 days)
  > Hi! We'd love to support you with whatever you need to move forward.
  > If now isn't the right time, no problem - just let us know when you'd prefer us to reconnect.

#### Discount Sequence

_Create urgency and drive conversions with a time-limited offer.

This 3-part sequence nudges leads to take action by highlighting an exclusive discount - starting with excitement, building urgency, and ending with a strong final call to act._

Mode: `extension` · messages: 3

- **Day 1 - Exclusive Discount Just for You! 🎉** (whatsapp, immediate)
  > Hi {lead_name},
  >
  > We're excited to offer you an exclusive discount to help you get started.
  >
  > For the next 3 days, you can enjoy a special discount off your purchase.
  >
  > This is a limited-time offer, and we don't want you to miss out on this opportunity.
  >
  > Let us know if you have any questions or if you're ready to get started!
- **Day 2 - Don't Miss Out! ⏳** (whatsapp, +1 days)
  > Hi {lead_name},
  >
  > Just a quick reminder that your exclusive discount is still available!
  >
  > You've got 24 hours left to save on your purchase.
  >
  > This is a great chance to experience everything we offer at a special price.
  >
  > Let us know if you need any help or if you're ready to claim your discount!
- **Day 3 - Last Chance to Save! 🚨** (whatsapp, +1 days)
  > Hi {lead_name},
  >
  > This is your final reminder!
  >
  > Your exclusive discount expires today.
  >
  > Don't miss your chance to get started at a special price!
  >
  > If you're ready, we're here to help you every step of the way. Claim your discount before it's gone!

#### Call Me Sequence

_Stay proactive and approachable with this gentle call follow-up sequence.

It helps you reconnect with leads who didn\'t respond earlier - showing persistence without pressure, while keeping the door open for a quick conversation._

Mode: `extension` · messages: 3

- **Day 1 - Tried to Call You 📞** (whatsapp, immediate)
  > Hi {lead_name},
  >
  > I tried reaching out to connect with you today, but we couldn't connect. 
  > When would be a good time for a quick call? Let me know what works best for you, and I'll be happy to chat!
- **Day 2 - Following Up on Our Call 📅** (whatsapp, +1 days)
  > Hi {lead_name},
  >
  > Just following up to see when would be a good time for us to connect today.
  >
  > I'm available at your convenience-let me know what works for you!
- **Day 3 - Last Check-In 📲** (whatsapp, +1 days)
  > Hi {lead_name},
  >
  > Checking in one last time!
  >
  > Does any time today work for you to connect?
  >
  > I'd love to help you out and answer any questions you might have.

### Smart triggers

| Name | Trigger | Action | Keywords / binding | Enabled |
|---|---|---|---|---|
| Stop on STOP Keyword | keyword_detected | stop_assigned_sequence | keywords: stop | True |
| Stop if Lead Won | lead_moved_to_stage | stop_assigned_sequence | on: Lead Won | False |
| Trigger Discount Sequence | sequence_completed | initiate_sequence | → seq: Discount Sequence; on: Nurturing Sequence | False |

### Knowledge base seed (org_info)

`about` (1122 chars):

```
BloomWorks Events designs unforgettable experiences through meticulous planning, creative storytelling, and flawless on-ground execution.  
From large corporate conferences to intimate destination weddings, BloomWorks manages every detail — concept, décor, logistics, vendors, and audience engagement.

Core Services:
- Corporate Events & Brand Launches  
- Destination Weddings & Social Celebrations  
- Exhibition & Expo Management  
- Stage Design, Décor & Vendor Coordination  
- On-site Management & Hospitality Teams

Key Differentiators:
- In-house creative design and fabrication  
- Strong vendor network across India and UAE  
- Expertise in both experiential branding and personal celebrations  
- Dedicated account manager for every event  

Guidelines:
- Keep tone imaginative yet grounded — blend creativity with assurance.  
- Never reveal vendor costs or exact budgets; say:  
  “Our planners will share a custom proposal once we finalize your theme and requirements.”  
- Avoid promising celebrity or influencer availability until confirmed.  
- Escalate qualified leads to the event director immediately.
```

`qualification_requirements` (2791 chars):

```
#### 1️⃣ Event Type [Validation Source: By Category]
Ask:  
> “Hi there! Thanks for reaching out to *BloomWorks Events.* What kind of event are you planning — *corporate*, *wedding*, *social*, or *brand launch*?”

---

#### 2️⃣ Scale & Guest Count [Validation Source: By Stage]
Ask:  
> “Approximately how many guests or attendees are you expecting?”

---

#### 3️⃣ Date & Venue [Validation Source: By Stage]
Ask:  
> “Do you already have a date or venue in mind, or are you still exploring options?”

If yes:  
> “Perfect — we can align décor and vendor availability accordingly.”

---

#### 4️⃣ Theme / Vision [Validation Source: By Idea]
Ask:  
> “How would you describe the look or vibe you're going for — *elegant*, *modern*, *traditional*, or *minimalist*?”

If they sound unsure:  
> “Totally fine — our creative team can show a few theme boards for inspiration.”

---

#### 5️⃣ Budget Range [Validation Source: By Stage]
Ask:  
> “Do you have a tentative budget range in mind? Even an approximate number helps us suggest practical concepts.”

---

#### 6️⃣ Contact & Proposal [Validation Source: General]
Ask:  
> “Lovely! Please share your *name, company / occasion name,* and *contact number* so our planner can prepare a tailored proposal for you.”

---

### Logic Rules
- For corporate → focus on brand identity and engagement.  
- For social → focus on emotion and personalization.  
- For weddings → focus on aesthetics, logistics, and family comfort.  

### Guardrails
- No commitment of celebrity acts, venues, or décor items without confirmation.  
- Always close warmly:  
  > “Thanks for sharing all this! Our event planner will reach out shortly with a curated concept and next steps.”

---

### VALIDATION SNIPPETS

#### By Category
- Amazing — we've delivered stunning {{event_type}} experiences that clients still talk about.  
- Fantastic — those events let our creative team shine with décor and engagement ideas.  
- Excellent — {{event_type}} projects bring out the best in our production crew.  

#### By Stage
- Great — that guest count helps us design seating and logistics efficiently.  
- Perfect — having a tentative date helps us pre-book key vendors.  
- Smart — early planning always results in smoother execution.  

#### By Idea
- Love that theme — we can elevate it with lighting, décor, and flow.  
- Elegant choice — timeless and sophisticated always impresses guests.  
- Great — our creative lead will sketch a few ideas around that vision.  

#### General / Rapport
- You've got a wonderful sense of detail — this event will be beautiful.  
- Excited already — can't wait to bring your vision to life.  
- The ideas sound fresh — our team will help refine them seamlessly.  
- You're in great hands; we'll make sure every element feels effortless.
```

`attachments`: []

### Seeded FAQs

**About Your Business & Services** — Questions about your business, services, and how to get started
- Q: What does your business do and what kind of customers do you work with?
  A: <p>We serve individuals and businesses across multiple sectors, offering customized products and services tailored to their goals.</p>
- Q: What are the main products or services you provide?
  A: <p>We provide end-to-end solutions ranging from consultation to delivery, depending on client needs. Details can be shared during onboarding.</p>
- Q: How can someone get started or connect with your team for more details?
  A: <p>You can reach out through our contact form, WhatsApp, or phone number. Our team will respond promptly to guide you further.</p>

**Pricing, Process & Support** — Questions about pricing, process, and customer support
- Q: How does your pricing or quotation process work?
  A: <p>Pricing is based on project scope, materials, and timeline. We share transparent quotations before starting any work.</p>
- Q: What are the usual steps once a customer shows interest in your service?
  A: <p>Our process involves understanding client requirements, sharing proposals, finalizing terms, and starting the project upon approval.</p>
- Q: How can customers reach your support or contact team for assistance?
  A: <p>Customers can contact support through email, WhatsApp, or call during business hours. We aim to resolve all queries within 24 hours.</p>

### Seeded demo leads

- **New Lead**: 3 leads (Riya Kapoor, Amit Verma, Sneha Singh)
- **Qualified**: 3 leads (Neha Patel, Rohit Malhotra, Vikram Joshi)
- **Nurturing**: 2 leads (Manish Tiwari, Simran Kaur)
- **Good Lead**: 2 leads (Tanya Roy, Aditya Sharma)
- **Lead Won**: 2 leads (Sahil Jain, Priya Mehta)
- **No Response**: 2 leads (Karan Sethi, Mitali Arora)
- **Deleted**: 2 leads (Test Lead (Inactive), Old Demo Lead)
