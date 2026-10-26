"""Grounded event metadata. Missing/invalid metadata never removes an article."""
from datetime import date
from decimal import Decimal, InvalidOperation
import re

from .common import dumps, sha

VERSION='news-event-identity-1.0'
STAGES={'proposed','planned','announced','adopted','effective','completed','reported','unknown'}
MONTHS=['января','февраля','марта','апреля','мая','июня','июля','августа','сентября','октября','ноября','декабря']


def content_hash(document):
    """Identity enrichment alone must not invalidate paid company/news checks."""
    return sha(dumps({k:v for k,v in document.items() if k!='event_identity'}))


def _text(value):
    return value.strip() if isinstance(value,str) else ''


def _items(value):
    return value if isinstance(value,list) else []


def normalize(raw,document):
    result=dict(schema_version=VERSION,status='missing',main_thesis='',event_type='unclear',
                actors=[],action='',object='',stage='unknown',event_date=None,date_role='unknown',
                period=None,metrics=[],main_source_block_ids=[],source_evidence=[],limitations=[],
                semantic_support_verified=False)
    if raw is None:
        return result
    if not isinstance(raw,dict):
        return {**result,'status':'invalid','limitations':['event_identity must be an object']}
    if raw.get('status')=='missing' and not raw.get('main_thesis'):
        return result
    result['status']='partial';issues=[s for s in _items(raw.get('validation_issues')) if isinstance(s,str)]
    if raw.get('schema_version',VERSION)!=VERSION:
        return {**result,'status':'invalid','limitations':['Unsupported event identity version']}
    blocks={b.get('id'):b.get('text','') for b in (document.get('source_blocks') or []) if isinstance(b,dict)}
    refs=raw.get('main_source_block_ids') or []
    if not isinstance(refs,list):refs=[]
    result['main_source_block_ids']=list(dict.fromkeys(b for b in refs if isinstance(b,str) and b in blocks))
    for evidence in _items(raw.get('source_evidence')):
        if not isinstance(evidence,dict):continue
        bid=evidence.get('block_id');quote=_text(evidence.get('quote'))
        if bid in result['main_source_block_ids'] and len(quote)>=40 and len(re.findall(r'\w+',quote))>=6 and quote in blocks[bid]:
            result['source_evidence'].append(dict(block_id=bid,quote=quote))
        else:issues.append('Unverified event evidence discarded')
    for field in ['main_thesis','action','object']:
        result[field]=_text(raw.get(field))
    result['actors']=list(dict.fromkeys(_text(a) for a in raw.get('actors',[]) if _text(a))) if isinstance(raw.get('actors'),list) else []
    result['event_type']=raw.get('event_type') if raw.get('event_type') in {'single_event','digest','unclear'} else 'unclear'
    result['stage']=raw.get('stage') if raw.get('stage') in STAGES else 'unknown'
    result['date_role']=raw.get('date_role') if raw.get('date_role') in {'effective','occurred','publication','reporting_period','unknown'} else 'unknown'
    pool=' '.join(e['quote'] for e in result['source_evidence']).lower().replace('ё','е')
    when=raw.get('event_date')
    if when:
        try:
            parsed=date.fromisoformat(when)
            russian=re.search(r'\b'+str(parsed.day)+r'\s+'+MONTHS[parsed.month-1]+r'(?:\s+(\d{4}))?',pool)
            russian_ok=bool(russian and (int(russian[1])==parsed.year if russian[1] else str(parsed.year)==str(document.get('document_date',''))[:4]))
            explicit=(when in pool or russian_ok or f'{parsed.day:02d}.{parsed.month:02d}.{parsed.year}' in pool)
            if not explicit or parsed.isoformat()!=when:raise ValueError('Date not explicitly supported')
            result['event_date']=when
        except (ValueError,TypeError):issues.append('Event date is absent from the cited text')
    period=raw.get('period')
    if isinstance(period,str) and period.strip():
        if period.strip() in pool:result['period']=period.strip()
        else:issues.append('Event period is absent from the cited text')
    for metric in _items(raw.get('metrics')):
        if not isinstance(metric,dict):continue
        bid=metric.get('source_block_id');quote=_text(metric.get('source_quote'))
        try:
            value=Decimal(str(metric.get('value')).replace(',','.'))
            if not value.is_finite():raise ValueError('nonfinite value')
            quoted={Decimal(v.replace(',','.')) for v in re.findall(r'-?\d+(?:[.,]\d+)?',quote)}
            if bid not in result['main_source_block_ids'] or not quote or quote not in blocks[bid] or value not in quoted:
                raise ValueError('unsupported metric')
            name,unit=_text(metric.get('name')),_text(metric.get('unit'))
            if not name or not unit:raise ValueError('metric name/unit required')
            result['metrics'].append(dict(name=name,value=format(value,'f'),unit=unit,
                period=_text(metric.get('period')) or None,source_block_id=bid,source_quote=quote))
        except (InvalidOperation,TypeError,ValueError,KeyError):issues.append('Unsupported event metric discarded')
    result['limitations']=list(dict.fromkeys([x for x in raw.get('limitations',[]) if isinstance(x,str)]+issues)) if isinstance(raw.get('limitations',[]),list) else issues
    result['validation_issues']=list(dict.fromkeys(issues))
    required=(result['event_type']=='single_event' and result['main_thesis'] and result['actors'] and result['action']
              and result['object'] and result['stage']!='unknown' and result['event_date'] and result['source_evidence'])
    if required and not issues:result['status']='identified'
    if not result['source_evidence']:
        result['limitations']=list(dict.fromkeys(result['limitations']+['Main event has no verified original passage; automatic event matching is unavailable']))
    return result


def save(connection,news_id,source_hash,identity,stamp):
    connection.execute('''INSERT INTO news_event_identities(news_id,source_input_hash,schema_version,status,main_thesis,identity_json,updated_at)
        VALUES(?,?,?,?,?,?,?) ON CONFLICT(news_id) DO UPDATE SET source_input_hash=excluded.source_input_hash,
        schema_version=excluded.schema_version,status=excluded.status,main_thesis=excluded.main_thesis,
        identity_json=excluded.identity_json,updated_at=excluded.updated_at''',
        (news_id,source_hash,VERSION,identity['status'],identity['main_thesis'],dumps(identity),stamp))
