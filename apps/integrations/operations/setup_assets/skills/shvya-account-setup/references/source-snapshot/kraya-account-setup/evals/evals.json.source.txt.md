{
  "skill_name": "kraya-account-setup",
  "evals": [
    {
      "id": 1,
      "prompt": "I'm from Kraya ops. We have a new client, Fix My Hair, a hair transplant and PRP clinic in HSR Layout Bangalore. Pasting the brainstorming call transcript below and the checklist doc is attached. Website is https://fixmyhair.example. Leads mostly come from Instagram ads, sales cycle about 2 weeks, they don't share prices in chat. Their Kraya login is owner@fixmyhair.example / <password>. Set up the whole account on staging.\n\n[transcript...]",
      "expected_output": "The agent runs the full phased workflow: profile, qualification spec, sequences, account config, then executes every API step in order on the provided account (attributes, stages after Qualified, 5 sequences, org info with bot_languages, grounded FAQs, core rules bound to real ids, quick replies, bump-ups), verifies via GET calls, and hands back a summary with ids and a gaps list. No emoji or placeholders reach the account; pricing is deferred per the client's choice.",
      "files": []
    },
    {
      "id": 2,
      "prompt": "The client Bakistry (custom cakes, Pune) says the day-2 message in their nurture sequence sounds too salesy and they want their Diwali offer mentioned instead. Also add a 'Quotation Sent' stage after Qualified. Account creds are in the session.",
      "expected_output": "The agent lists sequences, fetches the full nurture sequence, proposes a rewritten day-2 message (no emoji, no dashes, one asset, one CTA, keeps all other messages with their ids), waits for confirmation, then upserts the whole message list. For the stage it reads metadata, computes an order strictly greater than Qualified, proposes name/description/colour/AI flag, and creates it on confirmation. It does not touch New Lead / Qualified.",
      "files": []
    },
    {
      "id": 3,
      "prompt": "ops here. new client onboarded already on Kraya (industry template stuff is in there). they are a B2B pipe fittings manufacturer in Rajkot, MOQ 500 pcs, dealers and distributors only, Hinglish. i only have the profile doc from sales, no call transcript. can you set up qualification + attributes + stages + rules for now, skip sequences until we get the transcript",
      "expected_output": "The agent flags the missing transcript as a gap, applies the manufacturing playbook and the volume/fit gate (buyer type branch, quantity vs MOQ, below-MOQ polite disqualification, reverse-lead redirect), writes bot_languages 'English, Hindi' or Hinglish per the profile, creates dropdown attributes (Buyer Type, Product Category, Monthly Quantity, Qualification Status...), the B2B trading stages with AI off, the core rules that don't need sequences, and explicitly reuses or leaves the seeded industry sequences rather than duplicating them. It reports what was skipped and why."
    }
  ]
}
