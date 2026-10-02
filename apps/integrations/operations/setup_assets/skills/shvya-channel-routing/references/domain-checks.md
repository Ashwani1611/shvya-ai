# Channel routing domain checks

## Evidence
Resolve the exact lead/source/channel, connected account identity/mode, pipeline binding, current lead pipeline, messaging settings, AI eligibility, integration lifecycle/health and provider delivery path.

## Known traps
- Connected account does not authorize every pipeline.
- A phone/username/provider ID does not select an organization.
- API, Coexistence and Hosted capabilities/storage differ.
- Correct inbound routing does not prove outbound sender affinity.
- Current lead pipeline can change after acquisition; outbound must respect the current routing contract.
- A connected provider account can still have broken webhook subscription or invalid templates/tokens.
- Fallback to another connected sender can create cross-pipeline leakage.

## Verification
Validate topology source → account → pipeline → lead → sender → provider. Run routing validation and lead-specific trace. Verify post-change account/pipeline binding and representative outbound eligibility without sending unless authorized.

## Handoffs
Provider-specific details → WhatsApp/Instagram/email; AI eligibility → AI debugger; lifecycle → integration manager.
