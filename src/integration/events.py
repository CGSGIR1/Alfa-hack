"""Conservative event identity, separate from relevance and business mechanisms.

Exact republications are suppressed before paid analysis. Daily official FX
fixings remain distinct events, but can share one updating card in the feed.
No article, source paragraph or historical assessment is deleted.
"""
from hashlib import sha256
import json
import re
from decimal import Decimal
from urllib.parse import urlsplit, urlunsplit
from .financial_events import identify, same_fact, obsolete, notification


def normalized_title(text):
    text = (text or '').lower().replace('ё', 'е')
    text = re.sub(r'\bбанк\s+россии\b|\bцентробанк\b', 'цб', text)
    return ' '.join(re.findall(r'[а-яa-z0-9]+', text))


def source_text(doc):
    blocks = doc.get('source_blocks') or []
    # Only the lead defines the event; background numbers must not merge events.
    return next((b.get('text', '') for b in blocks if b.get('text')), '')


def story_key(doc):
    financial=identify(doc)
    if financial:
        return 'official-usd-rub' if financial['thread_key']=='official_fx:usd-rub' else financial['thread_key']
    return None


def fx_identity(doc):
    financial=identify(doc)
    if not financial or financial['kind']!='official_fx' or financial['currency']!='usd' or not financial['period']:
        return None
    from .financial_events import MONTHS
    year,month,day=map(int,financial['period'].split('-'))
    return ('official-usd-rub',str(year),str(day),MONTHS[month-1])


def event_key(doc):
    fx = fx_identity(doc)
    if fx:
        return 'fx:' + ':'.join(fx)
    financial=identify(doc)
    if financial and financial['kind']=='key_rate':
        return 'rate:'+financial['period']+':'+json.dumps(financial['values'],sort_keys=True)
    if (doc.get('event') or {}).get('id'):
        return 'parser:' + doc['event']['id']
    title = normalized_title(doc.get('document_title', doc.get('title', '')))
    day = doc.get('document_date', doc.get('publication_date', ''))
    return 'title:' + day + ':' + sha256(title.encode()).hexdigest()


def canonical_url(doc):
    raw = (doc.get('source') or {}).get('url') or ''
    if not raw:
        return None
    parts = urlsplit(raw)
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), parts.path.rstrip('/'), '', ''))


def compare_event_features(a,b):
    """Explain a vector candidate using grounded metadata; never delete on this."""
    from news_analytics.event_identity import normalize
    left,right=normalize(a.get('event_identity'),a),normalize(b.get('event_identity'),b)
    base=dict(suppress=False,semantic_support_verified=False)
    if left['status']!='identified' or right['status']!='identified':
        return dict(base,relation='insufficient_evidence',reason='Для обеих статей нужны подтверждённые признаки одного главного события')
    def subject(event):
        return (tuple(sorted(normalized_title(s) for s in event['actors'])),normalized_title(event['action']),normalized_title(event['object']))
    if subject(left)!=subject(right):
        return dict(base,relation='different_or_uncertain_event',reason='Различаются участники, действие или предмет')
    if left['stage']!=right['stage']:
        return dict(base,relation='possible_update',reason='Изменилась стадия события')
    if left['date_role']!=right['date_role'] or left['date_role']=='publication':
        return dict(base,relation='insufficient_evidence',reason='Дата публикации не подтверждает дату события')
    if left['event_date']!=right['event_date'] or left['period']!=right['period']:
        return dict(base,relation='possible_update',reason='Изменились дата действия или период события')
    def metrics(event):
        return sorted((normalized_title(m['name']),str(Decimal(m['value']).normalize()),normalized_title(m['unit']),m.get('period') or '') for m in event['metrics'])
    if metrics(left)!=metrics(right):
        return dict(base,relation='possible_update',reason='Изменились показатели; округления и дополнительные сведения требуют проверки')
    return dict(base,relation='same_event_candidate',reason='Совпали подтверждённые признаки главного события; уникальные сведения статей нужно сохранить')


def filter_candidates(data, company_id, candidates):
    """One representative per exact event; never repeat completed events.

    Revisions of the SAME article may be reassessed. A different effective date
    (e.g. tomorrow's new FX fixing) is new evidence, not a duplicate.
    """
    docs = {n['id']: json.loads(n['source_json']) for n in data['news']}
    candidates=[c for c in candidates if c['news_id'] in docs]
    revisions = {n['id']: n.get('input_hash') for n in data['news']}
    completed = data.get('completed_event_news', set())
    financial_history=list(data.get('completed_financial_events',[]))
    financial_history.extend(f for nid in completed if nid in docs and (f:=identify(docs[nid])))
    financial_candidates={nid:identify(doc) for nid,doc in docs.items()}
    latest={}
    for candidate in candidates:
        f=financial_candidates[candidate['news_id']]
        if f and f.get('verified'):
            latest[f['thread_key']]=max(latest.get(f['thread_key'],''),f['period'])
    handled = {event_key(docs[nid]) for nid in completed if nid in docs}
    urls = {canonical_url(docs[nid]) for nid in completed if nid in docs and canonical_url(docs[nid])}
    accepted, selected, selected_financial = [], set(), []
    for candidate in sorted(candidates, key=lambda c: (-c['_score'], c['news_id'])):
        nid = candidate['news_id']
        if (nid,revisions.get(nid)) in data.get('completed_news_revisions', set()):
            continue
        doc = docs[nid]
        financial=financial_candidates[nid]
        if financial and financial.get('verified'):
            if financial['period']<latest.get(financial['thread_key'],''):
                continue
            if any(same_fact(financial,old) or obsolete(financial,old) for old in financial_history):
                continue
            if any(same_fact(financial,old) for old in selected_financial):
                continue
            selected_financial.append(financial)
            accepted.append(candidate)
            continue
        key, url = event_key(doc), canonical_url(doc)
        if key in selected or (nid not in completed and (key in handled or url and url in urls)):
            continue
        selected.add(key)
        accepted.append(candidate)
    return accepted


def group_feed(items):
    """One card per exact event, or an explicitly labelled official-FX thread."""
    cards, groups = [], {}
    for item in items:
        key = item.get('event_key') or event_key(item)
        thread=item.get('story_key') or story_key(item)
        if thread:
            key = 'thread:'+thread
        groups.setdefault(key, []).append(item)
    for key,members in groups.items():
        if key.startswith('thread:topic-quota:'):
            representative=max(members,key=lambda n:(n.get('completed_at') or '',n.get('delivery_sequence',0)))
        else:
            representative=max(members,key=lambda n:((n.get('financial_identity') or identify(n) or {}).get('period') or n['publication_date'],n['publication_date'],n.get('completed_at') or ''))
        publications=[{k:n[k] for k in ('id','title','publication_date','source','fragments','source_blocks')} for n in members]
        delivered=max((n.get('completed_at') or '' for n in members),default='')
        sequence=max((n.get('delivery_sequence',0) for n in members if (n.get('completed_at') or '')==delivered),default=0)
        financial=representative.get('financial_identity') or identify(representative)
        note=notification(members) if financial and financial['kind'].endswith('_fx') else None
        cards.append(dict(representative,feed_id='event-'+sha256(key.encode()).hexdigest()[:24],
            completed_at=delivered,delivery_sequence=sequence,content_completed_at=representative.get('completed_at'),
            notification_revision=note,financial_identity=financial,
            related_publications=publications,updates=[p for p in publications if p['id']!=representative['id']]))
    return sorted(cards,key=lambda n:(n.get('completed_at') or '',n.get('delivery_sequence',0)),reverse=True)
