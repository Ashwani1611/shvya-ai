"""Organization-authored FAQ evidence, separate from private Playbook policy."""
from __future__ import annotations

import re

from apps.ai_engagement.services.playbook import parse_playbook

_STOP = frozenset('a an the is are do does can could you your me my i we what how about please tell and of to in for'.split())


def _tokens(text):
    return {word.casefold() for word in re.findall(r'[^\W_][\w\u0900-\u0dff]*', str(text))
            if len(word) > 1 and word.casefold() not in _STOP}


def faq_pairs(raw):
    """Accept Q:/A: and Question:/Answer:; never extract answers from Rules."""
    text = parse_playbook(raw).get('faq', '')
    pairs = []
    question, answer = '', []
    in_answer = False
    for line in text.splitlines():
        q = re.match(r'^\s*(?:[-*]|\d+[.)])?\s*(?:Q(?:uestion)?\s*\d*)\s*[:.)-]\s*(.+)', line, re.I)
        a = re.match(r'^\s*(?:[-*]\s*)?A(?:nswer)?\s*[:.)-]\s*(.*)', line, re.I)
        if q:
            if question and answer:
                pairs.append((question, '\n'.join(answer).strip()))
            question, answer, in_answer = q.group(1).strip(), [], False
        elif a and question:
            answer, in_answer = [a.group(1).strip()], True
        elif re.match(r'^\s*(?:[-*]\s*)?(?:\*\*)?(?:internal\s+)?(?:notes?|rules?|instructions?)\s*(?:\*\*)?\s*:', line, re.I):
            in_answer = False
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
        if exact or (overlap and score >= 0.4):
            scored.append({**item, 'score': 1.0 if exact else score})
    return sorted(scored, key=lambda item: -item['score'])[:limit]
