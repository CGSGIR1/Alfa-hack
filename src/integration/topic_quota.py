"""A company-wide rate-news quota, independent of exact-event deduplication.

Completed report snapshots are the durable clock. Reading old reports neither
deletes them nor needs the original news rows to remain in the database.
"""
from datetime import datetime, timedelta, timezone
import json
import re

from news_analytics.common import require
from news_analytics.database import connect

TOPIC = 'Процентные ставки'
VERSION = 'interest-rate-quota-v1'
THREAD_PREFIX = 'topic-quota:interest-rates:'


def hours(config):
    value = config.get('admission', {}).get('interest_rate_interval_hours', 12)
    require(type(value) is int and 1 <= value <= 168,
            'interest_rate_interval_hours must be between 1 and 168')
    return value


def clock():
    return datetime.now(timezone.utc)


def timestamp(value):
    try:
        stamp = datetime.fromisoformat(value.replace('Z', '+00:00'))
        return stamp.replace(tzinfo=timezone.utc) if stamp.tzinfo is None else stamp.astimezone(timezone.utc)
    except (ValueError, TypeError, AttributeError):
        return None


def _normal(value):
    return ' '.join(re.findall(r'[а-яa-z0-9]+', str(value or '').lower().replace('ё', 'е')))


def is_rate_news(doc):
    identity = doc.get('feed_identity') or {}
    if identity.get('quota_topic') == TOPIC:
        return True
    # Match the label actually used by the frontend when it is available.
    label = (doc.get('narrative') or {}).get('topic') or doc.get('topic')
    if label:
        return _normal(label) == _normal(TOPIC)
    title = _normal(doc.get('document_title') or doc.get('title'))
    if re.search(r'ключев\w* ставк|процентн\w* ставк', title):
        return True
    if re.search(r'\bцб\b|центробанк|банк россии', title) and re.search(r'\bставк', title):
        return True
    fragments = doc.get('fragments') or doc.get('selected_fragments') or []
    # The first sector is the fallback label used by site.feed, rather than
    # a secondary mention of rates in otherwise unrelated currency news.
    sectors = [s for f in fragments for s in f.get('sectors', []) if isinstance(s, str) and len(s) <= 45]
    return bool(sectors and _normal(sectors[0]) == _normal(TOPIC))


def last_processed(db, company_id):
    """Include rejected reports and archived snapshots, across profile revisions."""
    latest = None
    with connect(db, True) as c:
        rows = c.execute('''SELECT b.completed_at,b.result_json,r.report_json
          FROM company_report_batches b JOIN company_reports r USING(run_id,company_id)
          WHERE b.company_id=? AND b.status='completed' ORDER BY b.completed_at DESC''', (company_id,))
        for row in rows:
            stamp = timestamp(row['completed_at'])
            if stamp is None:
                continue
            if latest is not None and stamp <= latest:
                break
            report = json.loads(row['report_json'])
            original = {n['news_id']: n for n in report.get('news', [])}
            result = json.loads(row['result_json'])
            narratives = {n['news_id']: n for n in (result.get('narrative') or {}).get('items', [])}
            if any(is_rate_news(n) for n in (result.get('narrative') or {}).get('independent_risks', [])):
                latest = stamp
            for item in result.get('items', []):
                doc = {**original.get(item['news_id'], {}), **item}
                doc['narrative'] = item.get('narrative') or narratives.get(item['news_id'], {})
                if is_rate_news(doc):
                    latest = stamp
                    break
    return latest


def filter_candidates(db, company_id, docs, candidates, interval_hours=12, at=None):
    latest = last_processed(db, company_id)
    at = at or clock()
    blocked = latest is not None and at < latest + timedelta(hours=interval_hours)
    rate = [c for c in candidates if is_rate_news(docs.get(c['news_id'], {}))]
    # At most one rate article in a manually requested multi-news batch too.
    chosen = None if blocked else max(rate, key=lambda c: (
        docs.get(c['news_id'], {}).get('document_date') or docs.get(c['news_id'], {}).get('publication_date') or '',
        c.get('_score', c.get('company_score', 0)), c['news_id']), default=None)
    allowed = [c for c in candidates if c not in rate or c is chosen]
    return allowed, dict(interest_rate_quota_skipped=len(candidates)-len(allowed),
                        interest_rate_interval_hours=interval_hours,
                        interest_rate_next_at=(latest+timedelta(hours=interval_hours)).isoformat() if blocked else None)


def feed_windows(items, interval_hours=12):
    """Collapse legacy over-delivery into rolling 12h slots, with latest content.

This is an explicit topic quota, not a claim that all these articles are copies.
Each slot has a stable identity anchored at its first delivered report.
"""
    result = []
    anchor = None
    for item in sorted(items, key=lambda n: (timestamp(n.get('completed_at')) or datetime.min.replace(tzinfo=timezone.utc), n.get('delivery_sequence', 0))):
        if not is_rate_news(item):
            result.append(item)
            continue
        stamp = timestamp(item.get('completed_at'))
        if stamp is None:
            stamp = timestamp((item.get('report_as_of') or item['publication_date'])+'T00:00:00+03:00')
        if anchor is None or stamp >= anchor + timedelta(hours=interval_hours):
            anchor = stamp
        result.append(dict(item, story_key=THREAD_PREFIX+anchor.isoformat(),
                           topic_quota=dict(topic=TOPIC, interval_hours=interval_hours,
                                            window_start=anchor.isoformat())))
    return result
