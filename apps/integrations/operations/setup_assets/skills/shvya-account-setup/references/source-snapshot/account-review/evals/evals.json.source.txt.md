{
  "skill_name": "account-review",
  "evals": [
    {
      "id": 1,
      "prompt": "https://api.kraya-ai.com/analytics/dashboard?org_id=a29a561d-f346-40fb-bd6b-4b102806dd48\n\nlet's analyze this client",
      "expected_output": "The agent recognises a bare dashboard URL as a review request, resolves the org id, and runs Phase 0 before anything else: sale, CRM card, stage, time in stage, flags, POC, and the gap between the sale date and the first real lead. It then fans out the three subagent prompts in a single message and runs the production-traffic sweep itself in the main thread rather than delegating it. The final report leads with a verdict, contains a requirement-by-requirement table with separate 'configured?' and 'live?' columns, quotes the client verbatim for requirements and the bot verbatim for defects, reports distinct-lead counts rather than run counts, names what is genuinely working with numbers, and ends with ranked actions that each name a layer and an owner."
    },
    {
      "id": 2,
      "prompt": "Ops says the account for Rishabh Overseas is fully built — org info, 16 stages, 5 sequences, 19 rules. The client is complaining nothing happens. Can you check?",
      "expected_output": "The agent does not accept the configuration listing as evidence. It runs check B and finds the activation layer: how many rules are enabled, whether any sequence has a live trigger, how many leads each sequence is assigned to, and above all the rule-execution count for the org. It reports a zero execution count as the headline of the review rather than a detail, distinguishes 'configured and off' from 'not configured', and checks whether generic onboarding template content is running in place of the bespoke build. It does not report sequence `enabled = 0` as proof of anything, because that column is a no-op."
    },
    {
      "id": 3,
      "prompt": "This client says the bot quoted a price of 29,999 to one of their leads and they never authorised that. Is the AI hallucinating?",
      "expected_output": "The agent greps `qualification_requirements`, the `about` block, the FAQs and the sequence copy for the figure BEFORE answering. If the figure is in the config it reports a policy deviation owned by ops and the client, names where in the config it sits, and looks for when and why it was added; it does not call it a hallucination. If the figure is absent from every config surface it reports a model defect owned by the prompt. Either way it quantifies the blast radius in distinct leads with a date range, quotes one real reply verbatim, and says whether it is still happening."
    },
    {
      "id": 4,
      "prompt": "Big spike of AI errors on this org last Tuesday — looks like a lot of leads got no reply. How bad was it for the client?",
      "expected_output": "The agent collapses the failed runs to distinct lead ids before quantifying anything, then checks per affected lead whether a later run succeeded. It recognises the retry-storm pattern — a large run count resolving to a handful of leads that all recovered — and reports client impact as small while flagging the wasted spend separately. It reads the error text to identify the cause and says plainly whether it is a platform or vendor problem rather than an account setup problem, so it goes to the right queue."
    },
    {
      "id": 5,
      "prompt": "PMU is on a DFY plan and their health score is 75, Healthy. Ops wants to know if we can leave them alone until renewal.",
      "expected_output": "The agent refuses to let the health score settle the question, noting it measures usage intensity rather than whether the setup matches the client. It runs checks D, F and G: what the bot actually said to real leads, which paid features are unused, whether the pack tags match what was sold, credit runway against the burn rate, last login, and whether the client is getting the outcome they described on their assessment call measured against their own stated baseline. It surfaces any open client-reported bug that is still unresolved, and any commitment made on a call that was never delivered, as renewal risk."
    },
    {
      "id": 6,
      "prompt": "Review this account and tell me if the org info is any good.",
      "expected_output": "The agent runs check E rather than giving a literary opinion. It calls the existing conflict audit for the fact ledger, promise-without-material and stale-content checks instead of repeating them, then adds the review-specific ones: facts carrying more than one value across org info, FAQs and live replies; instructions the platform cannot execute, specifically anything telling the AI to write, verify or order an attribute write; rules with no material behind them, such as a catalogue referenced with `sendable_files` empty; the size of `qualification_requirements` against the reliability thresholds; and settings that contradict the prompt, such as interactive options being off while the spec is built on numbered lists. It reports ambiguity concretely, quoting the instruction and saying which two ways it could be read."
    }
  ]
}
