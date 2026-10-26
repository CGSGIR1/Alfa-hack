"""Adapt saved search reports through the canonical, evidence-preserving format."""
from copy import deepcopy
from pathlib import Path
import sys


def comparison_to_package(report,market_indicators=None):
    try:
        from news_analytics.llm_io import report_input
    except ModuleNotFoundError as error:
        if error.name!='news_analytics':raise
        sys.path.insert(0,str(Path(__file__).resolve().parents[2]/'company-news-analytics'))
        from news_analytics.llm_io import report_input
    package=report_input(report)
    expanded=report_input(report,include_candidates=True,per_news=10,total=max(10,10*len(report['news'])))
    package['market_indicators']=deepcopy(market_indicators or [])
    docs={d['id']:d for d in expanded['analytical_documents']}
    mechanisms={m['id']:m for m in expanded['analytical_mechanisms']}
    sources={s['id']:s for s in expanded['analytical_source_fragments']}
    candidates={n['news_id']:[] for n in report['news']}
    for link in expanded['connections']:
        m=mechanisms[link['mechanism_id']]
        candidates[link['news_id']].append(dict(mechanism=m,document=docs[m['document_id']],
            sources=[sources[s] for s in m['source_fragment_ids']],news_fragment_id=link['news_fragment_id']))
    extras=dict(full_texts={n['news_id']:'\n\n'.join(b['text'] for b in (n.get('source_blocks') or [])) for n in report['news']},
                candidates=candidates,catalog={key:deepcopy(expanded[key]) for key in
                ('analytical_documents','analytical_mechanisms','analytical_source_fragments')})
    return package,extras
