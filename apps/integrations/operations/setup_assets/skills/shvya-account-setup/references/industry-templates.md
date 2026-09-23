# Seed recognition and reconciliation

The legacy bundle's templates describe that company's onboarding seeds, not Shvya defaults. They include six seed categories: education/training, healthcare, real estate, travel, manufacturing and generic/other. Most contain Discount/Offer, FOMO Reminder and DNP Cadences, basic stop/terminal/chaining rules, six FAQs, a sample company profile and demo leads; the generic set includes Nurturing, DNP, Discount and Call Me.

Do not provision these exact seeds in Shvya. They contain invented company names, discounts, prices/payment percentages, fulfilment windows, support availability, testimonials and eligibility assumptions. Their job in this adaptation is to help recognize imported scaffolding and avoid duplicate or wrong-industry content.

## Reconciliation procedure

1. Inventory actual Shvya configuration with returned IDs, definition snapshots and dependency counts. Do not infer a clean account from an onboarding date.
2. Mark every intended entity `reuse`, `update`, `create`, `retire if requested`, or `defer`. Match by meaning and references as well as normalized name; two equal names need disambiguation.
3. Inspect generic promotional/recovery copy for factual grounding, stage eligibility, timing, STOP behavior and repeated content. An existing DNP and new No Response Recovery may be the same job.
4. Compare About/Playbook/FAQs to verified company materials. Flag the wrong service taxonomy, sample business identity, fake urgency, missing source links and unsupported guarantees.
5. Resolve dependencies before changing stages, options, Cadences or Workflows. Archive retirement candidates only when authorized and canonical checks pass; do not delete sample-looking leads as a setup convenience.
6. Record before/after IDs and preserved dependencies. A package may identify candidate cleanup without changing any tenant.

## Typical seed leakage to detect

| Industry | Suspicious inherited assumption requiring verification |
|---|---|
| Education | A sample academy's programs, jobs/placement claims, EMI/50% payment terms or recorded-class support |
| Healthcare | Generic clinic departments, instant booking confirmation, free follow-ups, insurance or telemedicine availability |
| Real estate | Sample projects, RERA/legal document promises, financing arrangements, guaranteed availability or appreciation |
| Travel | 30% deposit, 24-hour support, visa assistance, invented seasonal capacity and refund terms |
| Manufacturing | Sample automation capabilities, bulk discounts, 10–15-day delivery and on-site installation |
| Other | An events-company identity on an unrelated business, vendor promises, arbitrary discounts and 24-hour support SLA |

These are audit cues, never facts about a Shvya tenant. Live discovery and authoritative company sources decide the actual configuration.
