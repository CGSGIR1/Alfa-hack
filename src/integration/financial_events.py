"""Narrow, source-based identities for actual Russian rate decisions / FX quotes.

No embeddings, generated mechanisms, forecasts or financial-topic quotas here.
Unknown dates/values can group a card, but cannot suppress a paid assessment.
"""
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, ROUND_DOWN, ROUND_HALF_UP
import json
import re

from news_analytics.database import connect

VERSION = 1
NOTIFICATION_HOURS = 12
MONTHS = ['января','февраля','марта','апреля','мая','июня','июля',
          'августа','сентября','октября','ноября','декабря']
CURRENCIES = {'usd':r'доллар\w*|usd', 'eur':r'евро|eur', 'cny':r'юан\w*|cny'}
OPINION = re.compile(r'прогноз|ожида|может|могут|обсуд|предрек|считает|связал|объяснил|заявил|набиуллин|шохин',re.I)


def _text(s):
    return re.sub(r'банк\w*\s+россии|центробанк\w*','цб',(s or '').lower().replace('ё','е'))


def _day(text, published):
    match=re.search(r'\b(\d{1,2})\s+('+'|'.join(MONTHS)+r')(?:\s+(20\d{2}))?\b',text)
    if not match:return None
    years=[int(match[3])] if match[3] else [published.year-1,published.year,published.year+1]
    dates=[]
    for year in years:
        try:dates.append(date(year,MONTHS.index(match[2])+1,int(match[1])))
        except ValueError:pass
    if not dates:return None
    return min(dates,key=lambda d:abs((d-published).days)).isoformat()


def identify(doc):
    title=_text(doc.get('document_title') or doc.get('title'))
    if OPINION.search(title):return None
    try:published=date.fromisoformat(doc.get('document_date') or doc.get('publication_date') or '')
    except (ValueError,TypeError):return None
    opening=' '.join(b.get('text','') for b in (doc.get('source_blocks') or [])[:2])
    lead=_text(next((b.get('text','') for b in doc.get('source_blocks') or [] if b.get('text')),''))
    first=re.split(r'(?<=[.!?])\s+',lead,maxsplit=1)[0]
    if re.search(r'ключев\w*\s+ставк',title) and 'цб' in title and re.search(r'сохран|сниз|повыс|остав|установ',title):
        # Commentary about the meeting is deliberately outside this classifier.
        period=_day(first,published)
        if not period and first and not re.search(r'ранее|прошл|в сентябр|в август',first):period=published.isoformat()
        segment=re.search(r'ключев\w*\s+ставк\w*([^%]{0,120}%)',first)
        value=re.search(r'(?:до|на уровне|состав\w*)\s*(\d+(?:[,.]\d+)?)\s*%',segment[0]) if segment else None
        if not value and segment:value=re.search(r'(\d+(?:[,.]\d+)?)\s*%',segment[0])
        values={'rate':value[1].replace(',','.')} if value else {}
        # A bare headline does not prove the date of an actual decision.
        if not period:return None
        return dict(version=VERSION,kind='key_rate',currency=None,period=period,
            thread_key='key-rate:ru:'+period,values=values,verified=bool(values))
    currency=next((code for code,pattern in CURRENCIES.items() if re.search(pattern,title,re.I)),None)
    if not currency or not re.search(r'курс|торг|подешев|подорож|опуст|сниз|вырос',title):return None
    if re.search(r'официальн\w*\s+курс',title) or ('цб' in title and re.search(r'установ|пониз|повыс|сохран|опуст|сниз',title)
            and (not lead or re.search(r'официальн|установ',lead))):kind='official_fx'
    elif re.search(r'внебирж|биржев|торг|мосбирж|forex|форекс',title):kind='market_fx'
    else:return None  # Do not guess whether an unspecified quote is official.
    if kind=='official_fx':
        effective=re.search(r'\bна\s+(?:[а-я]+[\s,]+){0,6}\d{1,2}\s+(?:'+'|'.join(MONTHS)+r')\b',_text(opening))
        period=_day(effective[0],published) if effective else None
    else:
        if re.search(r'на прошл|ранее|вчера',first):return None
        period=_day(first,published) or published.isoformat()
    # Read a level, never the headline's rounded "below X" or a change by X rubles.
    other=r'евро|юан|иен|фунт|доллар'
    pattern=r'(?:'+CURRENCIES[currency]+r')(?:(?!'+other+r').){0,100}?(?:на уровне|на отметке|состав\w*|рав\w*|до|[—–])\s*(\d+(?:[,.]\d+)?)\s*(?:руб|₽)'
    value=re.search(pattern,lead,re.I)
    values={currency:value[1].replace(',','.')} if value else {}
    return dict(version=VERSION,kind=kind,currency=currency,period=period,
        thread_key=kind+':'+currency+'-rub',values=values,verified=bool(values and period))


def same_fact(left,right):
    if not left or not right or not left.get('verified') or not right.get('verified'):return False
    if any(left.get(k)!=right.get(k) for k in ('kind','currency','period')):return False
    a,b=left.get('values',{}),right.get('values',{})
    if set(a)!=set(b):return False
    for key in a:
        x,y=Decimal(a[key]),Decimal(b[key])
        if x==y:continue
        # Official reprints often truncate the last decimals. Intraday quotes
        # and decisions must match exactly: small movements there can be real.
        if left['kind']!='official_fx':return False
        coarse,fine=(x,y) if x.as_tuple().exponent>y.as_tuple().exponent else (y,x)
        quantum=Decimal(1).scaleb(coarse.as_tuple().exponent)
        if coarse not in (fine.quantize(quantum,rounding=ROUND_DOWN),fine.quantize(quantum,rounding=ROUND_HALF_UP)):return False
    return True


def obsolete(candidate,previous):
    return bool(candidate and previous and candidate.get('verified') and previous.get('verified')
        and candidate['kind'].endswith('_fx') and candidate['thread_key']==previous['thread_key']
        and candidate['period']<previous['period'])


def completed(db,company_id,company_revision=None):
    """Completed snapshots survive source expiry and include LLM rejections."""
    identities=[]
    with connect(db,True) as c:
        rows=c.execute('''SELECT b.result_json,r.report_json FROM company_report_batches b
          JOIN company_reports r ON r.run_id=b.run_id AND r.company_id=b.company_id
          WHERE b.company_id=? AND b.status='completed' AND (? IS NULL OR b.company_revision=?)''',
          (company_id,company_revision,company_revision)).fetchall()
    for row in rows:
        result=json.loads(row['result_json']);report=json.loads(row['report_json'])
        sources={n['news_id']:n for n in report.get('news',[])}
        for item in result.get('items',[]):
            identity=(item.get('feed_identity') or {}).get('financial') or identify(sources.get(item['news_id'],{}))
            if identity:identities.append(identity)
    return identities


def initialize(c):
    # Bibliographic references only; no extra copies of expired article text.
    c.execute('''CREATE TABLE IF NOT EXISTS financial_event_sources (
      source_id TEXT PRIMARY KEY,identity_json TEXT NOT NULL,publication_json TEXT NOT NULL)''')


def publication(doc):
    return dict(id=doc.get('document_id') or doc.get('id'),
        title=doc.get('document_title') or doc.get('title'),
        publication_date=doc.get('document_date') or doc.get('publication_date'),
        source=doc.get('source') or {},fragments=[],source_blocks=[])


def remember_source(db,doc):
    identity=identify(doc);ref=publication(doc)
    if not identity or not identity.get('verified') or not ref['id']:return
    with connect(db) as c:
        with c:
            initialize(c)
            c.execute('INSERT OR REPLACE INTO financial_event_sources VALUES(?,?,?)',
                (ref['id'],json.dumps(identity,ensure_ascii=False),json.dumps(ref,ensure_ascii=False)))


def sources(db):
    references=[]
    with connect(db,True) as c:
        if c.execute("SELECT 1 FROM sqlite_master WHERE name='financial_event_sources'").fetchone():
            references.extend((json.loads(r[0]),json.loads(r[1])) for r in c.execute('SELECT identity_json,publication_json FROM financial_event_sources'))
        for row in c.execute('SELECT source_json FROM news'):
            doc=json.loads(row[0]);identity=identify(doc)
            if identity:references.append((identity,publication(doc)))
    return references


def notification(members):
    """FX updates are immediate; a renewed unread mark is limited to 12h."""
    notified=None;seen=[]
    for item in sorted(members,key=lambda n:n.get('completed_at') or ''):
        identity=item.get('financial_identity') or identify(item)
        if identity and any(same_fact(identity,old) for old in seen):continue
        if identity:seen.append(identity)
        raw=item.get('completed_at')
        if not raw:continue
        try:stamp=datetime.fromisoformat(raw.replace('Z','+00:00'))
        except ValueError:continue
        if stamp.tzinfo is None:stamp=stamp.replace(tzinfo=timezone.utc)
        if notified is None or stamp-notified>=timedelta(hours=NOTIFICATION_HOURS):notified=stamp
    return notified.isoformat() if notified else None
