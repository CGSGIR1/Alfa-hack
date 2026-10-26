"""Grounded, durable evidence for the new report agents' local tools."""
from copy import deepcopy
import json
from datetime import date

from news_analytics.database import connect
from news_analytics.llm_io import report_input

CATALOG_KEYS=('analytical_documents','analytical_mechanisms','analytical_source_fragments')
CLAIM_KEYS=('what_happened','why_it_matters','risks','opportunities','bank_view')


def cited_analytics(prose,packet):
    """Project the final writer's citations, independently of retrieval candidates."""
    refs={ref for key in CLAIM_KEYS for claim in prose.get(key,[]) if isinstance(claim,dict)
          for ref in claim.get('refs',[]) if isinstance(ref,str)}
    if prose.get('mechanism_id'):
        refs.add(prose['mechanism_id'])
    mechanisms=[m for m in packet['analytical_mechanisms'] if m['id'] in refs
        or m['document_id'] in refs or set(m['source_fragment_ids'])&refs]
    source_ids=refs|{sid for m in mechanisms for sid in m['source_fragment_ids']}
    sources=[s for s in packet['analytical_source_fragments'] if s['id'] in source_ids or s['document_id'] in refs]
    doc_ids=refs|{m['document_id'] for m in mechanisms}|{s['document_id'] for s in sources}
    return dict(analytical_mechanisms=mechanisms,analytical_source_fragments=sources,
                analytical_documents=[d for d in packet['analytical_documents'] if d['id'] in doc_ids])


def extras_for_report(db,report):
    expanded=report_input(report,include_candidates=True,per_news=10,total=max(10,10*len(report['news'])))
    docs={d['id']:d for d in expanded['analytical_documents']}
    mechanisms={m['id']:m for m in expanded['analytical_mechanisms']}
    sources={s['id']:s for s in expanded['analytical_source_fragments']}
    candidates={n['news_id']:[] for n in report['news']}
    for link in expanded['connections']:
        m=mechanisms[link['mechanism_id']]
        candidates[link['news_id']].append(dict(mechanism=m,document=docs[m['document_id']],
            sources=[sources[s] for s in m['source_fragment_ids']],news_fragment_id=link['news_fragment_id']))
    full={}
    with connect(db,True) as connection:
        for n in report['news']:
            row=connection.execute('SELECT source_json FROM news WHERE id=?',(n['news_id'],)).fetchone()
            doc=json.loads(row[0]) if row else {}
            blocks=doc.get('source_blocks') or []
            if blocks:full[n['news_id']]='\n\n'.join(b['text'] for b in blocks)
    market=[]
    if report['as_of']==date.today().isoformat():
        from src.pipeline import indicators
        aspects=[q for n in expanded['news'] for q in n.get('company_match',{}).get('aspects',[])]
        codes=indicators.codes_for_company(expanded['company']['facts'],aspects)
        with indicators.using_database(db):market=indicators.snapshot(codes)
    return dict(full_texts=full,candidates=candidates,indicator_database=str(db),market_indicators=market,
                catalog={key:deepcopy(expanded[key]) for key in CATALOG_KEYS})


def used_catalog(document,extras):
    refs=set()
    for item in document.get('items',[])+document.get('independent_risks',[]):
        if item.get('mechanism_id'):refs.add(item['mechanism_id'])
        for key in ('what_happened','why_it_matters','risks','opportunities','bank_view'):
            for claim in item.get(key,[]):refs.update(claim.get('refs') or [])
    original=extras.get('catalog') or {}
    mechs=[m for m in original.get('analytical_mechanisms',[]) if m['id'] in refs or set(m['source_fragment_ids'])&refs]
    source_ids=refs | {s for m in mechs for s in m['source_fragment_ids']}
    sources=[s for s in original.get('analytical_source_fragments',[]) if s['id'] in source_ids]
    doc_ids=refs | {m['document_id'] for m in mechs} | {s['document_id'] for s in sources}
    return dict(analytical_mechanisms=mechs,analytical_source_fragments=sources,
                analytical_documents=[d for d in original.get('analytical_documents',[]) if d['id'] in doc_ids])


def with_catalog(packet,document):
    packet=deepcopy(packet)
    for key in CATALOG_KEYS:
        known={r['id']:r for r in packet[key]}
        for row in (document.get('evidence_catalog') or {}).get(key,[]):
            if row['id'] in known and row!=known[row['id']]:
                raise ValueError('Conflicting report evidence: '+row['id'])
            known.setdefault(row['id'],deepcopy(row))
        packet[key]=list(known.values())
    return packet
