"""Organization-authored FAQ evidence, separate from private Playbook policy."""
from __future__ import annotations

import re

from django.db.models import Case, IntegerField, Q, Value, When

from apps.ai_engagement.services.playbook import parse_playbook
from apps.ai_engagement.services.retrieval import _QUERY_STOP_WORDS, _query_tokens, _tokens as _source_tokens


def _tokens(text):
    # The 16-token query budget must not truncate organization-authored answers.
    # Otherwise a topic after a long introduction is selected by SQL and then
    # discarded here even when it directly answers the customer's question.
    return set(_source_tokens(text)) - _QUERY_STOP_WORDS


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
    query = set(_query_tokens(question))
    if not query:
        return []
    info = OrgInfo.objects.filter(organization=organization).only('ai_playbook').first()
    candidates = [
        {'source_id': f'playbook:faq:{index}', 'source_type': 'playbook_faq',
         'question': q, 'content': a}
        for index, (q, a) in enumerate(faq_pairs(getattr(info, 'ai_playbook', '')))
    ]
    # Rank before bounding; later relevant FAQs must not disappear behind old rows.
    candidate_filter = Q()
    candidate_rank = Value(0, output_field=IntegerField())
    for token in query:
        question_match = Q(question__icontains=token)
        answer_match = Q(answer__icontains=token)
        candidate_filter |= question_match | answer_match
        candidate_rank += Case(When(question_match, then=Value(3)), default=Value(0))
        candidate_rank += Case(When(answer_match, then=Value(1)), default=Value(0))
    rows = (FAQ.objects.filter(organization=organization, is_active=True)
            .filter(candidate_filter).annotate(candidate_rank=candidate_rank)
            .order_by('-candidate_rank', 'pk')[:100])
    candidates.extend(
        {'source_id': f'faq:{row.pk}', 'source_type': 'organization_faq',
         'question': row.question, 'content': row.answer}
        for row in rows
    )
    scored = []
    for item in candidates:
        tokens = _tokens(item['question'])
        overlap = query & tokens
        score = len(overlap) / max(len(query | tokens), 1)
        answer_overlap = query & _tokens(item['content'])
        answer_coverage = len(overlap | answer_overlap) / len(query)
        exact = ' '.join(question.casefold().split()).rstrip('?.') == ' '.join(item['question'].casefold().split()).rstrip('?.')
        if exact or (overlap and (score >= 0.4 or query <= tokens)) or answer_coverage >= 0.75:
            scored.append({**item, 'score': 1.0 if exact else max(score, answer_coverage * 0.6)})
    return sorted(scored, key=lambda item: -item['score'])[:limit]


def authored_answer_candidates(*, organization, question, limit=12, max_chars=12000):
    """Offer bounded complete Q/A facts for the existing semantic reply guard.

    Lexical absence is not evidence absence: a Hindi question can refer to an
    English FAQ. These are candidates, not relevance decisions. Callers must
    require independent relevance verification and cannot use an extractive
    approval shortcut. Keep complete pairs so truncation cannot remove a refund
    exception, price condition, or another important qualification.
    """
    from apps.ai_engagement.models import FAQ, OrgInfo

    query = set(_query_tokens(question))
    info = OrgInfo.objects.filter(organization=organization).only('ai_playbook').first()
    candidates = [
        {'source_id': f'playbook:faq:{index}', 'source_type': 'playbook_faq',
         'question': q, 'content': a}
        for index, (q, a) in enumerate(faq_pairs(getattr(info, 'ai_playbook', '')))
    ]
    rank = Value(0, output_field=IntegerField())
    for token in query:
        rank += Case(When(question__icontains=token, then=Value(3)), default=Value(0))
        rank += Case(When(answer__icontains=token, then=Value(1)), default=Value(0))
    rows = (FAQ.objects.filter(organization=organization, is_active=True)
            .annotate(candidate_rank=rank).order_by('-candidate_rank', '-updated_at', '-pk')[:100])
    candidates.extend(
        {'source_id': f'faq:{row.pk}', 'source_type': 'organization_faq',
         'question': row.question, 'content': row.answer}
        for row in rows
    )
    candidates.sort(key=lambda item: -(
        3 * len(query & _tokens(item['question'])) + len(query & _tokens(item['content']))
    ))
    selected, seen = [], set()
    remaining = max_chars
    for item in candidates:
        content = f"Question: {item['question']}\nAnswer: {item['content']}"
        key = ' '.join(content.casefold().split())
        if key in seen or len(content) > remaining or not item['content'].strip():
            continue
        selected.append({**item, 'content': content})
        seen.add(key)
        remaining -= len(content)
        if len(selected) >= limit:
            break
    return selected
