"""Lossless identity bridge between the writer and the durable report outbox.

Writer prose is retained as narrative. Every selected news item, including an
explicit rejection, is acknowledged. Evidence quotes come from verified packet
spans, never from invented text or the writer's paraphrase.
"""
from copy import deepcopy

from news_analytics.analytics_sources import source_provenance
from news_analytics.common import require
from news_analytics.workflow import validate_result


def result_for_batch(packet, document):
    require(document.get('request_id') == packet['request_id'], 'Writer request_id mismatch')
    require(document.get('company_id') == packet['company']['id'], 'Writer company_id mismatch')
    require(document.get('as_of') == packet['as_of'], 'Writer date mismatch')
    quality = document.get('quality') or {}
    require(not quality.get('assessment_unresolved'), 'Unresolved LLM assessments; batch remains pending')
    require(not quality.get('report_issues_remaining'), 'Unresolved report errors; batch remains pending')
    assessment = document.get('assessment') or {}
    news_assessments = {n['news_id']: n for n in assessment.get('news_assessments', [])}
    links = {l['link_id']: l for l in assessment.get('link_assessments', [])}
    require(set(news_assessments) == {n['id'] for n in packet['news']}, 'Every news requires an explicit assessment')
    require(set(links) == {l['id'] for l in packet['connections']}, 'Every connection requires an explicit assessment')
    mechanisms = {m['id']: m for m in packet['analytical_mechanisms']}
    sources = {s['id']: s for s in packet['analytical_source_fragments']}
    prose = {n['news_id']: n for n in document.get('items', [])}
    company_context, items = [], []
    for news in packet['news']:
        decision = news_assessments[news['id']]
        assessments = []
        for connection in (c for c in packet['connections'] if c['news_id'] == news['id']):
            a = links[connection['id']]
            fit = a['label'] if a.get('fits_news') else 'rejected'
            evidence = []
            if fit in ('applicable', 'conditional'):
                for ref in mechanisms[connection['mechanism_id']].get('evidence_refs', []):
                    source = sources[ref['fragment_id']]
                    start, end = ref['start_char'], ref['end_char']
                    require(type(start) is int and type(end) is int and 0 <= start < end <= len(source['text']), 'Invalid evidence span')
                    evidence.append(dict(source_fragment_id=source['id'], mechanism_id=connection['mechanism_id'],
                        quote=source['text'][start:end], pages=ref.get('pages', []),
                        source_label=source_provenance(source['document_id'])['label']))
            assessments.append(dict(connection_id=connection['id'], news_mechanism_fit=fit,
                company_impact=dict(statement=a.get('reason', ''), applicability=a['label'],
                    company_basis=a.get('company_basis', []), required_conditions=a.get('required_conditions', [])),
                evidence=evidence))
            if a.get('relevant_to_company_independently') and a['label'] in ('applicable', 'conditional'):
                company_context.append(dict(connection_id=connection['id'], mechanism_id=connection['mechanism_id'],
                    applicability=a['label'], statement=a.get('reason', ''),
                    company_basis=a.get('company_basis', []), required_conditions=a.get('required_conditions', [])))
        narrative = prose.get(news['id'])
        event = ' '.join(c['text'] for c in (narrative or {}).get('what_happened', []) if c.get('type') == 'news_fact')
        items.append(dict(news_id=news['id'], title=news['title'], source=deepcopy(news.get('source', {})),
            news_fragment_ids=[f['id'] for f in news['fragments']],
            event_summary=event or news['title'], applicability=decision['label'], reason=decision.get('reason', ''),
            assessments=assessments, narrative=deepcopy(narrative)))
    result = dict(schema_version='company-analysis-report-1.1', request_id=packet['request_id'],
        company_id=packet['company']['id'], as_of=packet['as_of'], summary=document['summary'],
        items=items, company_context=company_context,
        overall_limitations=list(dict.fromkeys(packet['company'].get('limitations', []) + document.get('missing_information', []))),
        narrative=deepcopy(document))
    return validate_result(result, packet)
