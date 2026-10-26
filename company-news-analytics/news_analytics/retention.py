"""Age-based removal of working news; saved report snapshots remain readable."""
from datetime import date, datetime, timedelta, timezone
import json

from .common import require
from .database import backup, check, connect
from .workflow import workflow_lock


def news_cutoff(days, as_of=None):
    if days is None:
        return None
    require(type(days) is int and days > 0, 'news_retention_days must be a positive integer')
    today=date.fromisoformat(as_of) if as_of else datetime.now(timezone(timedelta(hours=3))).date()
    return (today-timedelta(days=days)).isoformat()


ORPHAN_NEWS_INPUTS="""role IN ('news_context','news_mechanism')
 AND id NOT IN (SELECT context_input_id FROM article_chunks
               UNION SELECT mechanism_input_id FROM article_chunks WHERE mechanism_input_id IS NOT NULL
               UNION SELECT input_id FROM company_queries
               UNION SELECT source_input_id FROM analytics_chunks
               UNION SELECT input_id FROM analytics_mechanisms)"""


def cleanup_news(db, days=10, as_of=None, dry_run=False, expired_source_ids=()):
    cutoff=news_cutoff(days,as_of)
    with workflow_lock(db),connect(db) as c:
        # A temporary ID table avoids parameter limits and never enters the DB.
        c.execute('CREATE TEMP TABLE retention_ids(id TEXT PRIMARY KEY)')
        expired=list(c.execute('SELECT id,source_json FROM news WHERE publication_date<?',(cutoff,)))
        c.executemany('INSERT INTO retention_ids VALUES(?)',[(r['id'],) for r in expired])
        source_ids=set(expired_source_ids)|{r['id'] for r in expired}
        for row in expired:
            incoming=json.loads(row['source_json']).get('import_provenance',{}).get('incoming_document_id')
            if incoming:source_ids.add(incoming)
        c.execute('CREATE TEMP TABLE retention_source_ids(id TEXT PRIMARY KEY)')
        c.executemany('INSERT INTO retention_source_ids VALUES(?)',[(x,) for x in source_ids])
        jobs=bool(c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='news_preparation_jobs'").fetchone())
        counts=dict(news=len(expired),
            chunks=c.execute('SELECT count(*) FROM article_chunks WHERE news_id IN (SELECT id FROM retention_ids)').fetchone()[0],
            company_checks=c.execute('SELECT count(*) FROM company_news_checks WHERE news_id IN (SELECT id FROM retention_ids)').fetchone()[0],
            preparation_jobs=c.execute('SELECT count(*) FROM news_preparation_jobs WHERE source_id IN (SELECT id FROM retention_source_ids)').fetchone()[0] if jobs else 0)
        pending=[]
        for row in c.execute("SELECT run_id,company_id,report_json FROM company_reports JOIN company_report_batches USING(run_id,company_id) WHERE status='prepared'"):
            report=json.loads(row['report_json'])
            if any(n.get('publication_date','9999-12-31')<cutoff for n in report.get('news',[])):
                pending.append((row['run_id'],row['company_id']))
        # Predict both newly orphaned and already unreferenced news vectors.
        referenced={r[0] for r in c.execute('SELECT context_input_id FROM article_chunks WHERE news_id NOT IN (SELECT id FROM retention_ids) UNION SELECT mechanism_input_id FROM article_chunks WHERE news_id NOT IN (SELECT id FROM retention_ids) AND mechanism_input_id IS NOT NULL UNION SELECT input_id FROM company_queries UNION SELECT source_input_id FROM analytics_chunks UNION SELECT input_id FROM analytics_mechanisms')}
        counts['vectors']=sum(r[0] not in referenced for r in c.execute("SELECT id FROM embedding_inputs WHERE role IN ('news_context','news_mechanism')"))
        result=dict(cutoff=cutoff,retention_days=days,dry_run=dry_run,counts=counts,cancelled_pending_batches=len(pending),backup=None)
        c.commit()
        if dry_run or not (any(counts.values()) or pending):
            return result
        result['backup']=backup(db)
        with c:
            c.execute('BEGIN IMMEDIATE')
            for run_id,company_id in pending:
                c.execute("UPDATE company_report_batches SET status='cancelled',last_error=? WHERE run_id=? AND company_id=?",('News expired under retention policy: '+cutoff,run_id,company_id))
                # Fresh news from a mixed pending packet must remain eligible.
                c.execute('UPDATE company_news_checks SET assigned_run_id=NULL WHERE assigned_run_id=? AND company_id=?',(run_id,company_id))
            c.execute('DELETE FROM company_news_checks WHERE news_id IN (SELECT id FROM retention_ids)')
            c.execute('DELETE FROM article_chunks WHERE news_id IN (SELECT id FROM retention_ids)')
            c.execute('DELETE FROM news WHERE id IN (SELECT id FROM retention_ids)')
            if jobs:c.execute('DELETE FROM news_preparation_jobs WHERE source_id IN (SELECT id FROM retention_source_ids)')
            c.execute('DELETE FROM embedding_inputs WHERE '+ORPHAN_NEWS_INPUTS)
            check(c)
        return result
