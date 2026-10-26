"""Read-only projection of saved, verified reports for the reference website.

No new ranking, model inference or API calls take place when reading the feed.
The newest assessment of a news item wins, including a later rejection.
"""
from datetime import datetime, timedelta, timezone, date
from decimal import Decimal, InvalidOperation
import json

from news_analytics.common import require
from news_analytics.database import connect
from news_analytics.llm_io import report_input
from news_analytics.workflow import history, batch_report
from .events import group_feed, event_key, story_key
from .admission import admits
from .report_context import cited_analytics
from .financial_events import identify as financial_identity, sources as financial_sources, same_fact
from . import topic_quota


def feed_revision(db, company_id):
    """Small, stable event token; no repeated loading of report snapshots."""
    from news_analytics.common import sha, dumps
    require(isinstance(company_id, str) and company_id.strip(), 'Выберите компанию')
    with connect(db, True) as c:
        company = c.execute('SELECT input_hash FROM companies WHERE id=?', (company_id,)).fetchone()
        require(company is not None, 'Компания не найдена')
        batches = c.execute('''SELECT count(*),max(created_at),max(completed_at),sum(status='completed')
            FROM company_report_batches WHERE company_id=?''', (company_id,)).fetchone()
        archived=[]
        if c.execute("SELECT 1 FROM sqlite_master WHERE name='archived_report_batches'").fetchone():
            archived=list(c.execute('SELECT count(*),max(archived_at) FROM archived_report_batches WHERE company_id=?',(company_id,)).fetchone())
    today = datetime.now(timezone(timedelta(hours=3))).date().isoformat()
    return sha(dumps([company[0], list(batches), today,archived,topic_quota.VERSION]))


def company_payments(document):
    relation = document.get('relationships', {}).get('payments', {})
    members = document.get('facts', {}).get('members', [])
    own = {document['subject_id']} | {m.get('subject_id', m.get('id')) for m in members
                                    if isinstance(m.get('subject_id', m.get('id')), str)}
    directory, grouped = relation.get('counterparty_directory', {}), {}
    for row in relation.get('rows', []):
        try:
            amount = Decimal(str(row.get('amount_source_value')))
            count = int(row.get('payment_count') or 0)
            if not amount.is_finite():
                continue
        except (InvalidOperation, ValueError, TypeError):
            continue
        payer, recipient = row.get('payer_id'), row.get('beneficiary_id')
        directions = []
        if payer in own:
            directions.append(('out', recipient))
        if recipient in own:
            directions.append(('in', payer))
        for direction, counterparty in directions:
            if not counterparty:
                continue
            key = direction, counterparty
            entry = grouped.setdefault(key, dict(direction=direction, counterparty=counterparty,
                amount=Decimal(0), count=0, own=counterparty in own,
                industry=directory.get(counterparty, {}).get('main_okved_name')))
            entry['amount'] += amount
            entry['count'] += count
    rows = sorted(grouped.values(), key=lambda r: r['amount'], reverse=True)
    for row in rows:
        row['amount'] = str(row['amount'])
    return dict(incoming=[r for r in rows if r['direction'] == 'in'][:4],
                outgoing=[r for r in rows if r['direction'] == 'out'][:4],
                currency=relation.get('currency'), amount_unit=relation.get('amount_unit'),
                period_start=relation.get('period_start'), period_end=relation.get('period_end'))


def independent_context(document, packet, batch):
    """Saved company scenarios remain distinct from news; no new retrieval here."""
    records=[]
    for prose in document.get('independent_risks') or []:
        mid=prose.get('mechanism_id')
        evidence=cited_analytics(prose,packet)
        mechanisms=evidence['analytical_mechanisms']
        if not mechanisms:
            continue
        records.append(dict(id=mid,feed_id='independent-'+mid,independent=True,
            title=prose.get('headline') or mechanisms[0]['title'],
            publication_date=document['as_of'],completed_at=batch.get('completed_at'),
            source={},applicability='conditional',reason='Отдельный аналитический сценарий для компании. Он не объясняет выбранную новость.',
            narrative=prose,topic=prose.get('topic') or 'Контекст компании',
            source_blocks=[],fragments=[],source_traceability={},limitations=[],required_conditions=[],
            **evidence,
            connections=[],assessments=[],run_id=batch['run_id'],report_as_of=document['as_of']))
    return records


def delivery_day(value, fallback):
    """Feed filters use report delivery, independently of the article's age."""
    if value:
        try:
            stamp=datetime.fromisoformat(value.replace('Z','+00:00'))
            if stamp.tzinfo is None:
                stamp=stamp.replace(tzinfo=timezone.utc)
            return stamp.astimezone(timezone(timedelta(hours=3))).date().isoformat()
        except (ValueError,TypeError):
            pass
    return fallback


def feed(db, company_id, news_days=7, as_of=None, admission_policy=None, topic_quota_hours=12):
    require(isinstance(company_id, str) and company_id.strip(), 'Выберите компанию')
    today = date.fromisoformat(as_of) if as_of else datetime.now(timezone(timedelta(hours=3))).date()
    with connect(db, True) as c:
        row = c.execute('SELECT source_json FROM companies WHERE id=?', (company_id,)).fetchone()
        require(row is not None, 'Компания не найдена')
        company = json.loads(row['source_json'])
    projected_company = dict(id=company_id, subject_type=company.get('subject_type', 'company'),
        profile=company.get('profile_text', ''), facts=company.get('facts', {}),
        limitations=company.get('limitations', []), payments=company_payments(company))
    batches = history(db, company_id)
    items, seen, excluded, reports = [], set(), 0, []
    latest_report=None
    independent=[]
    seen_independent=set()
    policy_excluded=0
    for batch in sorted(batches,key=lambda b:b.get('completed_at') or b.get('created_at') or '',reverse=True):
        result = batch.get('result')
        reports.append({k: batch.get(k) for k in ('run_id', 'status', 'created_at', 'completed_at', 'last_error')})
        if batch['status'] != 'completed' or not result:
            continue
        candidates = [i for i in result.get('items', []) if i['news_id'] not in seen]
        seen.update(i['news_id'] for i in candidates)
        report, _ = batch_report(db, batch['run_id'], company_id)
        packet = report_input(report)
        from .report_context import with_catalog
        packet=with_catalog(packet,result.get('narrative') or {})
        document=result.get('narrative') or {}
        # An absent section in a later report does not withdraw an earlier
        # company scenario. Keep one latest saved version of each mechanism.
        if document.get('as_of') and document['as_of']<=today.isoformat():
            for context in independent_context(document,packet,batch):
                if context['id'] not in seen_independent:
                    seen_independent.add(context['id'])
                    context['delivery_date']=delivery_day(context.get('completed_at'),context['report_as_of'])
                    independent.append(context)
        indexed = {n['id']: n for n in packet['news']}
        for item_index,item in enumerate(candidates):
            news = indexed.get(item['news_id'])
            if not news or item.get('applicability') not in ('applicable', 'conditional'):
                excluded += 1
                continue
            match=next((n for n in report['news'] if n['news_id']==item['news_id']), {})
            if admission_policy and not admits(match, **admission_policy):
                excluded+=1
                policy_excluded+=1
                continue
            published = date.fromisoformat(news['publication_date'])
            # Completed reports are an archive, not the source-news queue.
            # Their saved source snapshots survive source cleanup indefinitely.
            if published > today:
                excluded += 1
                continue
            with connect(db, True) as c:
                original = c.execute('SELECT source_json FROM news WHERE id=?', (item['news_id'],)).fetchone()
            original = json.loads(original['source_json']) if original else (match or news)
            identity=item.get('feed_identity') or {}
            prose = item.get('narrative') or next((n for n in result.get('narrative', {}).get('items', [])
                                                 if n['news_id'] == item['news_id']), {})
            connections = [c for c in packet['connections'] if c['news_id'] == item['news_id']]
            evidence=cited_analytics(prose,packet)
            mechanism_ids={m['id'] for m in evidence['analytical_mechanisms']}
            connections=[c for c in connections if c['mechanism_id'] in mechanism_ids]
            decision = next((n for n in result.get('narrative', {}).get('assessment', {}).get('news_assessments', [])
                             if n['news_id'] == item['news_id']), {})
            # Sector tags originate in the prepared news, not from a browser scoring heuristic.
            sectors = [s for f in report['news'] if f['news_id'] == item['news_id']
                       for fragment in f['selected_fragments'] for s in fragment.get('sectors', [])]
            items.append(dict(id=item['news_id'], title=item['title'], publication_date=news['publication_date'],
                delivery_sequence=item_index,
                event_key=identity.get('event_key') or event_key(original),
                story_key=identity.get('story_key') or story_key(original),
                financial_identity=identity.get('financial') or financial_identity(original),
                source=item.get('source', news['source']), applicability=item['applicability'], reason=prose.get('why_you_see_this') or '',
                narrative=prose, required_conditions=decision.get('required_conditions', []),
                topic=next((s for s in sectors if isinstance(s, str) and len(s) <= 45), 'Новости бизнеса'),
                source_blocks=news.get('source_blocks', []), fragments=news['fragments'],
                source_traceability=news.get('source_traceability', {}), limitations=news.get('limitations', []),
                **evidence,
                assessments=[], connections=connections,
                run_id=batch['run_id'], completed_at=batch.get('completed_at'),
                report_as_of=result.get('as_of')))
            if latest_report is None:
                document=result.get('narrative') or {}
                latest_report={key:document.get(key) for key in ('summary','key_recommendations','missing_information')}
                latest_report['as_of']=result.get('as_of')
    items.sort(key=lambda n: (n.get('completed_at') or '',n['delivery_sequence']), reverse=True)
    context_ids={n['id'] for n in independent}
    windowed=topic_quota.feed_windows(items+independent,topic_quota_hours)
    # The visible feed is shared with company scenarios, so the quota covers
    # their displayed labels too. Other scenarios keep their existing identity.
    independent=[n for n in windowed if n['id'] in context_ids and not n.get('topic_quota')]
    cards = group_feed([n for n in windowed if n['id'] not in context_ids or n.get('topic_quota')])
    references=financial_sources(db) if any(n.get('financial_identity') for n in cards) else []
    for card in cards:
        known={p['id'] for p in card['related_publications']}
        saved_sources=next((i.get('feed_identity',{}).get('financial_sources',[]) for b in batches
            for i in (b.get('result') or {}).get('items',[]) if i['news_id']==card['id']),[])
        additional=[ref for identity,ref in references if same_fact(card.get('financial_identity'),identity)]
        for ref in saved_sources+additional:
            if ref['id'] not in known:
                known.add(ref['id']);card['related_publications'].append(ref)
        card['delivery_date']=delivery_day(card.get('completed_at'),card.get('report_as_of') or card['publication_date'])
    market=[]
    from src.pipeline import indicators
    with indicators.using_database(db):
        codes=indicators.codes_for_company(company.get('facts') or {},[])
        market=[m for m in indicators.snapshot(codes) if m.get('date','')<=today.isoformat()]
    return dict(company=projected_company, today=today.isoformat(), window_days=news_days,
                report_retention_days=None,feed_period_default='all',period_basis='delivery_date',
                latest_report=latest_report,market_indicators=market,independent_risks=independent,
                independent_risks_scope='history',
                news=cards, history=reports, excluded_from_feed=excluded,
                grouped_publications=sum(n['id'] not in context_ids or bool(n.get('topic_quota')) for n in windowed)-len(cards),
                interest_rate_interval_hours=topic_quota_hours,
                policy_excluded=policy_excluded,
                ranking='Saved retrieval and LLM assessment; no client-side reranking')
