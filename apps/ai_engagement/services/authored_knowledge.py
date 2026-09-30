"""Organization-authored FAQ evidence, separate from private Playbook policy."""
from __future__ import annotations

import re

from apps.ai_engagement.services.playbook import parse_playbook
from apps.ai_engagement.services.retrieval import _query_tokens


def _tokens(text):
    return set(_query_tokens(text))


def faq_pairs(raw):
    """Accept plain/Markdown Q:/A: labels; never extract answers from Rules."""
    text = parse_playbook(raw).get('faq', '')
    pairs = []
    question, answer = '', []
    in_answer = False
    private = False
    for line in text.splitlines():
        # Authors commonly bold either the label or the entire Q/A line. Only
        # interpret markers inside the parsed FAQ section, never Playbook Rules.
        labeled = line.replace('**', '').replace('__', '')
        q = re.match(r'^\s*(?:[-*•]|\d+[.)])?\s*(?:Q(?:uestion)?\s*\d*)\s*[:.)-]\s*(.+)', labeled, re.I)
        a = re.match(r'^\s*(?:[-*•]\s*)?A(?:nswer)?\s*[:.)-]\s*(.*)', labeled, re.I)
        if q:
            if question and answer:
                pairs.append((question, '\n'.join(answer).strip()))
            question, answer, in_answer, private = q.group(1).strip(), [], False, False
        elif re.match(r'^\s*(?:[-*•]\s*)?(?:internal\s+|private\s+|confidential\s+)?(?:notes?|rules?|instructions?)\s*(?:[:.]|$)', labeled, re.I):
            in_answer, private = False, True
        elif a and question and not private:
            answer, in_answer = [a.group(1).strip()], True
        elif in_answer:
            answer.append(line)
    if question and answer:
        pairs.append((question, '\n'.join(answer).strip()))
    return [(q, a) for q, a in pairs if a][:100]


def matching_authored_answers(*, organization, question, limit=4):
    from apps.ai_engagement.models import FAQ, OrgInfo
    info = OrgInfo.objects.filter(organization=organization).only('ai_playbook').first()
    candidates = [
        {'source_id': f'playbook:faq:{index}', 'source_type': 'playbook_faq',
         'question': q, 'content': a}
        for index, (q, a) in enumerate(faq_pairs(getattr(info, 'ai_playbook', '')))
    ]
    candidates.extend(
        {'source_id': f'faq:{row.pk}', 'source_type': 'organization_faq',
         'question': row.question, 'content': row.answer}
        for row in FAQ.objects.filter(organization=organization, is_active=True).order_by('pk')[:100]
    )
    query = _tokens(question)
    if not query:
        return []
    scored = []
    for item in candidates:
        tokens = _tokens(item['question'])
        overlap = query & tokens
        score = len(overlap) / max(len(query | tokens), 1)
        exact = ' '.join(question.casefold().split()).rstrip('?.') == ' '.join(item['question'].casefold().split()).rstrip('?.')
        if exact or (overlap and (score >= 0.4 or query <= tokens)):
            scored.append({**item, 'score': 1.0 if exact else score})
    return sorted(scored, key=lambda item: -item['score'])[:limit]
