DOC_TOPICS = {
    "overview": {
        "title": "Build a better sales journey.",
        "intro": "Shvya brings lead management, WhatsApp conversations, AI engagement and structured follow-up into a connected sales workspace.",
        "sections": [
            ("Start with your sales process", ["Choose one lead source and one measurable outcome. For example, help a new enquiry become a qualified conversation, or reconnect with an interested lead who has stopped replying."]),
            ("Three parts, one journey", ["Cadence defines the rhythm of follow-up: the sequence, timing and next action.", "AI playbooks guide conversations with your company knowledge and engagement instructions.", "Workflows connect events and actions, so lead capture and sales activity stay connected."]),
            ("Your team remains part of the process", ["Automation helps maintain consistency. Your team handles judgment, exceptions, commitments and the conversations that need a person."]),
        ],
    },
    "quickstart": {
        "title": "Your first sales flow",
        "intro": "Start small, test with your own details, then expand the flow your team can confidently support.",
        "sections": [
            ("Prepare your workspace", ["Sign in to your Shvya dashboard and select your organization.", "Create the pipeline and stages that reflect your sales journey.", "Decide who owns new enquiries and who will handle qualified leads."]),
            ("Connect a source", ["Open Connect Hub. Choose a source such as Meta Lead Ad Forms or Google Sheets, complete the provider setup and map the lead fields to your pipeline."]),
            ("Prepare your knowledge", ["Add company information, FAQs and relevant documents. Keep pricing, terms and product details current."]),
            ("Test and activate", ["Use an internal test lead to verify field mapping and source attribution.", "Review messages, timing, reply behaviour and handoff.", "Activate the intended workflow after your test is clear."]),
        ],
    },
    "cadence": {
        "title": "Sales cadence",
        "intro": "Create a repeatable follow-up rhythm so an interested enquiry does not depend on someone remembering the next step.",
        "sections": [
            ("How a cadence works", ["A sequence groups follow-up steps over time. The follow-up state tracks the active sequence, upcoming send time and recent conversation activity."]),
            ("Create a useful sequence", ["Choose the lead segment and purpose.", "Write the opening message around the original enquiry.", "Set follow-up timing appropriate to the buying journey.", "Test the sequence before enabling it for customers."]),
            ("Reply-aware follow-up", ["Incoming replies can delay the next send according to configured settings. Review your organization timing settings before activation."]),
        ],
    },
    "playbooks": {
        "title": "AI playbooks",
        "intro": "Give AI the business context it needs to have more useful sales conversations.",
        "sections": [
            ("Build your knowledge foundation", ["Add organization information and engagement instructions.", "Maintain FAQs for common customer questions.", "Add documents relevant to your products and services.", "Remove obsolete offers or instructions."]),
            ("Guide the conversation", ["Define what to learn from a lead: requirements, location, timeline and buying criteria. Avoid making commitments your business has not approved."]),
            ("Move from reply to next step", ["Use the conversation to identify intent and capture useful context. Bring a team member in for proposals, unusual requests or decisions that need judgment."]),
        ],
    },
    "workflows": {
        "title": "Connected workflows",
        "intro": "Connect a business event to the next sales action, with context that travels with the lead.",
        "sections": [
            ("Design the flow", ["Choose the event that should start the flow.", "Identify required lead fields and destination stage.", "Configure the relevant action and responsible owner.", "Test both expected and incomplete-data scenarios."]),
            ("Example: a new Meta enquiry", ["A form submission enters the mapped pipeline. Your team can use the lead context to begin follow-up, understand requirements and move the opportunity to the right stage."]),
            ("Make ownership clear", ["Automation works best when someone is responsible for exceptions and handoffs. Keep the customer history, notes and next step visible."]),
        ],
    },
    "integrations": {
        "title": "Connect Hub",
        "intro": "Connect the sources that bring enquiries into Shvya and the tools your team already uses.",
        "sections": [
            ("Available integrations", ["WhatsApp Business", "Google Sheets", "Email", "Meta Lead Ad Forms", "Meta Conversions API", "Razorpay", "Justdial", "IndiaMART", "Shvya API", "Webhooks"]),
            ("Setup guidance", ["Provider accounts, credentials and access permissions are required where applicable. Map fields carefully and verify test events before production use."]),
        ],
    },
    "measurement": {
        "title": "Measure what moves",
        "intro": "Use your own sales data to understand whether a flow is helping.",
        "sections": [
            ("Track the journey", ["Time from enquiry to first response.", "Share of leads that reply to a follow-up.", "Share of conversations that become qualified opportunities.", "Share of qualified opportunities that become won deals."]),
            ("Improve one step at a time", ["Review the point where leads stop moving. Adjust the message, timing or handoff and observe the outcome."]),
        ],
    },
    "troubleshooting": {
        "title": "Troubleshooting",
        "intro": "Check the connection, the lead context and the workflow state before changing the whole sales process.",
        "sections": [
            ("Lead did not appear", ["Check source connection, field mapping, provider access and webhook delivery status."]),
            ("Message did not send", ["Check WhatsApp setup, template status, recipient phone format, account limits and cadence state."]),
            ("AI answer was not useful", ["Review the knowledge base, engagement instructions and whether the requested answer exists in approved business content."]),
        ],
    },
}


def docs_index():
    return [(slug, topic["title"]) for slug, topic in DOC_TOPICS.items()]
