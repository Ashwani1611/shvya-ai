# Reference contents

- Configurable Shvya areas
- Complete source authoring method, adapted for SHVYA
- Qualification Flow
- Qualification Requirements
- Example
- **Rules**
- Welcome Message
- Qualification Questions
- Question 1
- Question 2
- Question 3 - if lead choses hair transplant option in Q1
- Question 3 - if lead choses hair growth option in Q1
- Question 3 - if lead choses dandruff or scalp issue option in Q1
- Question 3 - if lead choses other option in Q1
- Final Acknowledgment Message
- Qualification
- Org Information
- Knowledge Base
- FAQs
- Smart Triggers
- Attributes
- Stages
- Sequences
- Integrations

# Configurable Shvya areas

This is the operator map. Exact tool arguments come from the current MCP schema, not this table.

| Area | Shvya artifact | Authoring and verification requirement |
|---|---|---|
| Qualification | AI Brain > AI Playbook | Canonical questions, branch eligibility, evidence criteria, acknowledgment, attribute mappings, stage/reminder rules; compile and simulate |
| Company information | AI profile `about`, `bot_languages` | Stable verified facts, identity, service coverage and permitted languages; never make About a second conflicting Playbook |
| Knowledge | URL knowledge sources, documents/chunks/publication | Approved sources, versions and provenance; ready ingestion and publication before claiming retrieval works |
| FAQs | organization question/answer records | Grounded concise answers, deduped by question; current `upsert_faq` has no category fields |
| Workflows | event + conditions + one action | Canonical schema, real tenant IDs, Source filter, stops and loop prevention; validate/simulate |
| Attributes | text/numeric/date/datetime/option definitions | One described field per collected decision, exact returned keys and option values; avoid credentials and unnecessary sensitive data |
| Stages/pipelines | CRM funnel with AI flags | One meaning and owner per stage; preserve protected stages; no automatic payment/won assertion from chat text |
| Cadences | sender-bound sequence and ordered steps | Hosted free-form, API approved templates, email or internal reminders as actually supported; preview timing |
| Integrations | connected channel and source configuration | Inventory, mapping, consent, ownership and health; unsupported connection steps become tasks |
| Touchpoints | saved replies and categories | Real company-specific content in useful operator groups; one reply serves one situation |
| Messaging settings | account and organization switches | Pipeline-linked routing, business hours, active-conversation delay, AI/follow-up/bump-up settings; scope overlaps audited |
| Commitments | tracked implementation ledger | Verbatim promise, date, due date or none given, owner, status; no invented Ops CRM endpoint |

A requested calendar, team assignment, round robin, billing change, template creation or arbitrary integration is not automatically supported by setup tools. Check the currently exposed contract. If absent, deliver exact content/mapping and a task for the responsible owner. Do not represent a draft as connected, booked, paid, uploaded or delivered.

Suggested scale, never a quota: 6–12 described attributes, only meaningful stages, 3–6 useful Cadences, 15–30 grounded FAQs and 12–20 Touchpoints. Small funnels may need much less. Prefer a compact complete Playbook over a prescribed character target.




# Complete source authoring method, adapted for SHVYA

> SHVYA ADAPTATION: Read runtime-contract.md before using this full reference. Preserve this method's detail, but compile its output into native SHVYA schemas. Kraya field names, API routes, database queries, token scripts and past performance claims are historical context, not live capabilities. Sample businesses, prices and policies remain examples. Human handoff/opt-out takes precedence over continued qualification; use verified double-brace CRM tokens and provider bindings. This reference does not authorize sending, enrollment or activation.

# Qualification Flow

The qualification flow is basically a prompt run to generate a reply for the message that the lead has sent over Whatsapp to the agent. This prompt has a specific set of guidelines and instructions on how to behave. Every organization can configure certain inputs and data on their account, which is injected dynamically into this prompt to generate the appropriate reply and behavior of the agent for talking to their leads during the qualification phase and as well as post qualification.

The following is the content configurable on an organization account:

1. Qualification Requirements
2. Organization Information
3. Knowledge Base
4. FAQs
5. Smart Triggers
6. Attributes
7. Stages
8. Sequences
9. Integrations


# Qualification Requirements

The qualification requirement is supposed to explain how the AI should behave when talking to leads. The general pattern includes:
- A rules section to identify or store certain match cases and ideas on how it should react or handle scenarios.
- A welcome message, which should be the first message that is sent to the lead when a lead gets created in the system.
- A qualification questions section, which lists out the different questions that should be asked to the lead. The exact question content is available; it can mention the exact question content, or otherwise it can just be general information about what is the question to be asked, along with any options and any certain conditions or scenarios on what should be the behavior if the lead responds with a specific reply to that question, or what should be the next question based on the response to the PAS question.
- An acknowledgement message that should be sent to the lead after they've been qualified.
- A final qualification section, which tells the condition on which the lead should be considered qualified. It could be, for example, if they've been qualified, if they've answered the first two questions, if they've completed the entire flow, or if they've provided a specific information.

Below is an example of a qualification requirement for a sample organization that qualifies their qualification requirement on a certain set of questions, where the third question is different based on the response of the first question.

## Example
<example>
## **Rules**

1. Greet the user with a welcome message if a conversation hasn't already started
2. If the lead mentions OR asks about any specific treatment keyword at any point in the conversation,
   then:
   - Mark Q1 as answered (do NOT ask Q1 again).
   - Map it to the correct Q1 option (A-F)
   - Answer the lead’s query (info request) briefly.
   - Continue the flow from the next unanswered question (usually Q2).
3. If the lead asks any general queries (pricing, timings, address, packages, doctor details) -> Skip the welcome message and give the relevant information first
4. If the lead has said "Stop / Not interested / Dont message further" -> Send the Exit Message and stop replying to the lead
5. After all required answers are collected, send the Final Acknowledgment Message.
6. The lead should be qualified only after the 3 questions have been answered

## Welcome Message
use the lead name if provided in the lead data
<welcome_message>
Hi {name}! 👋
Welcome to **Fix My Hair** — a trusted center for **Hair Growth Treatments**.

To assist you better, I'll ask you a few quick questions 😊
</welcome_message>

## Qualification Questions

### Question 1
<question_content>
What treatment are you looking for today?
We offer the following treatments:

A. Hair Transplant
B. Hair Growth
C. Dandruff or Scalp Issue
D. Other (please specify)
</question_content>

### Question 2
<question_content>
May I know your name, age, and location?
</question_content>
Proceed forward once name, age and location are provided.

### Question 3 - if lead choses hair transplant option in Q1
<question_content>
When would you like to schedule your consultation?
</question_content>
Lead can give a definite date or an estimate like next week or so which is also acceptable to proceed forward.


### Question 3 - if lead choses hair growth option in Q1
<question_content>
Are you using any other hair growth treatments?
</question_content>

### Question 3 - if lead choses dandruff or scalp issue option in Q1
<question_content>
Please share a picture of your scalp
</question_content>

### Question 3 - if lead choses other option in Q1
skip to final acknowledgment message


## Final Acknowledgment Message
<acknowledgement_message>
Thank you for sharing these details 🙏
Our team at **Fix My Hair** will connect with you shortly.

You can alternatively reach out to us at +91-1234567890.
</acknowledgement_message>

## Qualification
The lead should be considered qualified after they've answered the first two questions
</example>






# Org Information
The org information is basically information about the organization, which, in brief, tells the AI what the organization does, what kind of services they provide, any extra specific information that should be available to the AI all the time and is mandatory for it to know when conversing with leads, and is a non-negotiable to exclude. This content would always be injected in the prompt that is run to generate a reply to the lead's messages during the qualification flow or during general support queries when the lead has completed qualification and is part of the different sales stages.



# Knowledge Base

The knowledge base is a set of files, documents, URLs, and websites that contain information about the organization, and the content of it is extracted and stored in pinecone (vector db) in chunks. A separate lookup query is used based on the conversation to extract relevant information and inject it into the prompt for generating the reply.



# FAQs

The FAQs are a set of questions and answers that are commonly asked by leads when speaking to the salesperson of the organization. These questions should ideally be short FAQ information that is available for the agent to reference to answer any questions that the lead might ask. These are also stored in the vector database and queried dynamically and injected into the prompt like the knowledge base based on the lookup query to match and fetch relevant content.
The FAQs should be structured as articles, so you have:
- An article title, which ideally is the question
- Article content, which is the answer to the question  These can then be categorized into categories, and each category should have a title and a description.
The generated set of FAQ should ideally be a CSV with four columns:
- Category Title
- Category Description
- Article Title
- Article Content


# Smart Triggers
Smart triggers is a functionality on Kraya that allows the user to fire actions based on certain triggers. The triggers are basically events that happen in the system as certain actions take place. Based on these events, the trigger can match and fire. Each trigger has an action linked to it. Whenever a trigger fires, the action is executed.
Based on what the lead requires, we should set up a set of triggers that will achieve the complete workflow and automation for the particular account and qualification and post-qualification workflow.

The current triggers that we support are:
1. Lead moves to a stage: This trigger fires whenever a lead is moved into a specific stage of a specific pipeline.
2. A sequence ends: this trigger fires whenever an assigned sequence to a lead ends i.e. all the messages in the sequence have been sent.
3. New need created: This trigger fires whenever a lead gets created in a particular pipeline & stage
4. No response from lead: This trigger fires whenever X time has passed since the last response from the lead.
5. Keyword detected: this trigger fires whenever a lead message has been received and it matches a set of keywords that have been provided with the trigger. These keywords are a comma-separated set of keywords, or it can be set to a constant star, which would match any keyword or any word.
6. Lead stays in stage for x time: this fires when x time has passed since the lead has been in that particular stage.
7. Call logged: this fires when a call is logged for a lead matching the provided status (done or no response)

All triggers can be configured to fire for a specific stage and piping only or multiple stages and piping. Along with that, they can also have conditions for matching the attribute on the particular lead. Let's say, if a lead has attribute X, we can configure it so that it only fires if the value of attribute X matches some text Y or contains some text Y.

The actions that we support are:
1. Move to stage: this action moves the lead to a specific stage of a specific pipeline.
2. Start sequence: this action starts an assigned sequence to a lead.
3. Stop sequence: this action stops an assigned sequence to a lead.
4. Set call reminder: this action sets a call reminder for a lead. This requires also specifying a particular offset time, which would be the time added to the current time for setting the call reminder of a future date, along with an optional note. The user can also configure to overwrite any existing reminders.
5. Send template message: this action sends a template message to a lead. This action can only be configured if a WhatsApp API account is connected to send a whatsapp API template message
6. Toggle AI: this action toggles the AI/AF switch for a lead.
7. Toggle auto followup: this action toggles the auto followup switch for a lead.


# Attributes

These would be a set of attributes that would be required to identify a lead or track the lead for the given accounts service or product workflow. These attributes can be of specific types. The current types that are supported include:
- text
- numbers
- date
- datetime
- an option picker (which is a brokendown based specific set of values)


# Stages

Stages would be the different set of the sales pipeline funnel for the organization. A lead would be created in the New Lead stage. AI would talk to those leads. Once the leads get qualified, they would move into the Qualified stage. These two stages are hard coded and will always exist. After that, the users can configure the stages as they want, with as many stages as they need.
Identify what stages should be needed for their workflow and set that up, generate stage names along with brief descriptions of what that stage means and what it would be used for. This would be information available for the AI as well to decide if a lead should be moved into that stage.


# Sequences

Sequences are a set of messages that are sent to the lead on a specific schedule. Identify what kind of sequences the user would want to send to their leads, along with the content of each message and the schedule for when those messages should be sent. The sequence is configured such that you assign the different messages that should be sent in the sequence and when the next message should be sent relative to the previous message (for example, one hour after the previous message, two hours after the previous message, or one day after the previous message).


# Integrations

Identify what are the lead sources for the particular account. meta lead ads/indiamart/justdial/zoho/gohighlevel/neodove/website signup/third party crm/google sheets
How would those leads come into Kraya?
Which integrations would they need to configure to get those leads inside Kraya?  If they are landing directly on WhatsApp, then they either use the WhatsApp API or the extension. If they have an external CRM, would they need an integration for that? If they are using Meta Lead Ad Forms, they can either configure it so leads come into a Google Sheet and connect the Google Sheet via the Google Sheet integration, or have a direct connection of the Meta Lead Ad Forms.
