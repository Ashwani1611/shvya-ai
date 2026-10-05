from apps.ai_engagement.services.response_composer import build_response_plan


def plan(*, sensitive=False, verified=False, knowledge=None):
    return build_response_plan(
        payload={"organization": {"id": "org", "about": "Lead automation"},
                 "lead": {"id": "lead", "qualification": {"engagement_mode": "conversation"}},
                 "grounding": {"sensitive": sensitive, "verified": verified},
                 "knowledge": knowledge or [],
                 "recent_conversation": {"messages": [{"direction": "inbound",
                     "body": "What support comes with DIY?"}]}},
        organization_id="org", lead_id="lead",
    )


def test_later_stage_support_keeps_url_and_uploaded_file_only_facts():
    chunks = [{"content": "DIY includes ticket-based support.", "source_url": "https://example.com/pricing",
               "source_type": "url"},
              {"content": "Each plan includes 5,000 AI Coins.", "document_id": 18,
               "source_type": "uploaded_file"}]
    facts = plan(knowledge=chunks).allowed_facts
    assert facts[0]["content"] == chunks[0]["content"]
    assert facts[0]["source_url"] == chunks[0]["source_url"]
    assert facts[1]["document_id"] == 18
    assert plan(sensitive=True, knowledge=chunks).allowed_facts == ()


def test_retained_passages_are_deduplicated_and_bounded():
    chunks = [{"content": "x" * 6000}, {"content": "x" * 6000},
              {"content": "y" * 6000}, {"content": "z" * 6000}, {"content": "w" * 6000}]
    facts = plan(knowledge=chunks).allowed_facts
    retained = [f for f in facts if f.get("source_type") == "knowledge_chunk"]
    assert len(retained) == 3
    assert sum(len(f["content"]) for f in retained) == 12000
