"""Incremental company/news checks, durable queues and acknowledged LLM batches.

No LLM API is called here. A prepared batch is an immutable outbox item until an
external consumer supplies its result. Failed downstream work reuses that item.
"""
from __future__ import annotations
from contextlib import contextmanager
from datetime import date
from pathlib import Path
import json,os

from .common import dumps,sha,now,require,write_json,read_json
from .database import connect
from .retrieval import snapshot,score_company_news,choose_company_news,retrieve,supported_news
from .reporting import save,report_name,markdown,html_reports
from .llm_io import report_input
from .analytics_sources import source_provenance

MAX_REPORT_ATTEMPTS = 3


@contextmanager
def workflow_lock(db):
    """OS releases the lock on process death; no stale time-based leases."""
    path=Path(str(Path(db).resolve())+'.workflow.lock')
    with path.open('a+b') as stream:
        if path.stat().st_size==0:stream.write(b'0');stream.flush()
        stream.seek(0)
        try:
            if os.name=='nt':
                import msvcrt
                msvcrt.locking(stream.fileno(),msvcrt.LK_NBLCK,1)
            else:
                import fcntl
                fcntl.flock(stream.fileno(),fcntl.LOCK_EX|fcntl.LOCK_NB)
        except OSError as exc:
            raise RuntimeError('Company workflow is busy; retry after the current operation finishes.') from exc
        try:yield
        finally:
            stream.seek(0)
            if os.name=='nt':msvcrt.locking(stream.fileno(),msvcrt.LK_UNLCK,1)
            else:fcntl.flock(stream.fileno(),fcntl.LOCK_UN)


def scoring_revision(model,settings):
    # Counts/dates only choose from cached scores. Query/fragment construction
    # and admission thresholds affect comparisons and intentionally invalidate them.
    return sha(dumps(dict(algorithm='incremental-company-news-v1',model=model,
        news_fragments=settings.news_fragments,min_news_score=settings.min_news_score,
        financial_topic_coverage=settings.financial_topic_coverage)))


def _current(c,company_id,company_revision,news_revisions):
    row=c.execute('SELECT input_hash FROM companies WHERE id=?',(company_id,)).fetchone()
    if not row or row[0]!=company_revision:return False
    for nid,revision in news_revisions.items():
        row=c.execute('SELECT input_hash FROM news WHERE id=?',(nid,)).fetchone()
        if not row or row[0]!=revision:return False
    return True


def _batch(c,run_id,company_id):
    row=c.execute('''SELECT b.*,r.report_json FROM company_report_batches b
      JOIN company_reports r ON r.run_id=b.run_id AND r.company_id=b.company_id
      WHERE b.run_id=? AND b.company_id=?''',(run_id,company_id)).fetchone()
    require(row is not None,'Batch not found for this run/company')
    return dict(row)


def _restore(batch):
    """Repair missing outbox files from the committed report snapshot."""
    report=json.loads(batch['report_json']);saved=json.loads(batch['saved_json'])
    folder=Path(saved['output']);name=report_name(batch['company_id'])
    paths={name+'.json':report,'llm-input-'+name.removeprefix('company_')+'.json':report_input(report)}
    for filename,body in paths.items():
        target=folder/filename
        try:valid=target.is_file() and read_json(target)==body
        except (OSError,ValueError):valid=False
        if not valid:write_json(target,body)
    folder.mkdir(parents=True,exist_ok=True)
    (folder/'report.html').write_text(html_reports([report],batch['run_id']),encoding='utf-8')
    (folder/(name+'.md')).write_text(markdown(report),encoding='utf-8')
    return dict(status='resumed',company_id=batch['company_id'],report=report,saved=saved,
                workflow=dict(new_comparisons=0,reused_batch=batch['run_id'],awaiting_llm=True))


def process_company(db,output,model,company_id,settings,stages=None,candidate_filter=None,candidate_admission=None):
    """Prepare one outbox batch, resume it, or return idle; never auto-ack it."""
    settings.validate()
    with workflow_lock(db):
        data=snapshot(db,model)
        company=next((c for c in data['companies'] if c['id']==company_id),None)
        require(company is not None,'Unknown company ID: '+company_id)
        crev=company['input_hash'];srev=scoring_revision(model,settings)
        with connect(db) as c:
            prepared=c.execute("SELECT run_id FROM company_report_batches WHERE company_id=? AND status='prepared'",(company_id,)).fetchone()
            if prepared:
                batch=_batch(c,prepared[0],company_id)
                refs={r['news_id']:r['news_revision'] for r in c.execute(
                    'SELECT news_id,news_revision FROM company_news_checks WHERE assigned_run_id=? AND company_id=?',(prepared[0],company_id))}
                saved_report=json.loads(batch['report_json']);saved_news=saved_report['news']
                within_limit=settings.max_total_news is None or len(saved_news)<=settings.max_total_news
                # An unpaid batch packed under a smaller news limit is repacked while
                # more news waits; an answer already received from the LLM is kept.
                limits=saved_report.get('settings') or {}
                saved_limit=min(limits.get('top_news') or 0,limits.get('max_total_news') or limits.get('top_news') or 0)
                current_limit=min(settings.top_news,settings.max_total_news or settings.top_news)
                paid=(Path(json.loads(batch['saved_json'])['output'])/('llm-result-'+report_name(company_id).removeprefix('company_')+'.json')).is_file()
                more_waiting=c.execute('''SELECT 1 FROM company_news_checks WHERE company_id=? AND company_revision=?
                  AND scoring_revision=? AND disposition='queued' AND assigned_run_id IS NULL LIMIT 1''',(company_id,crev,srev)).fetchone()
                fully_packed=paid or not saved_limit or len(saved_news)>=current_limit or saved_limit>=current_limit or not more_waiting
                within_admission=candidate_admission is None or len(candidate_admission(data,company_id,saved_news))==len(saved_news)
                within_event_policy=candidate_filter is None or len(candidate_filter(data,company_id,
                    [dict(news_id=n['news_id'],_score=n['company_score']) for n in saved_news]))==len(saved_news)
                with_analytics={n['id'] for n in report_input(saved_report)['news']}=={n['news_id'] for n in saved_news}
                exhausted=batch['failed_attempts']>=MAX_REPORT_ATTEMPTS
                if not exhausted and within_limit and fully_packed and within_admission and within_event_policy and with_analytics and batch['company_revision']==crev and batch['scoring_revision']==srev and _current(c,company_id,crev,refs):
                    return _restore(batch)
                # Updated input must not be delivered as if it were the current one.
                with c:
                    reason=('Report retry limit reached: '+(batch['last_error'] or 'unknown error')) if exhausted else 'Event already processed under current policy' if not within_event_policy else 'No selected analytical support under current policy' if not with_analytics else 'News limit increased; repacking' if not fully_packed else 'Input revision changed'
                    c.execute("UPDATE company_report_batches SET status='cancelled',last_error=? WHERE run_id=? AND company_id=?",(reason,prepared[0],company_id))
                    # A failed report remains attached to its news revisions so
                    # the next selection cannot immediately recreate it.
                    if not exhausted:
                        c.execute('UPDATE company_news_checks SET assigned_run_id=NULL WHERE assigned_run_id=? AND company_id=?',(prepared[0],company_id))
            previous=[dict(r) for r in c.execute('''SELECT * FROM company_news_checks
              WHERE company_id=? AND company_revision=? AND scoring_revision=?''',(company_id,crev,srev))]
        today=date.fromisoformat(settings.as_of)
        eligible={n['id']:n for n in data['news'] if 0<=(today-date.fromisoformat(n['publication_date'])).days<settings.news_days}
        known={(r['news_id'],r['news_revision']):r for r in previous}
        unseen={nid:n for nid,n in eligible.items() if (nid,n['input_hash']) not in known}
        fresh_data={**data,'news':list(unseen.values())}
        scored={n['news_id']:n for n in score_company_news(fresh_data,company_id,settings)} if unseen else {}
        with connect(db) as c:
            with c:
                c.execute('BEGIN IMMEDIATE')
                require(_current(c,company_id,crev,{nid:n['input_hash'] for nid,n in unseen.items()}),'Inputs changed during comparison; retry')
                for nid,n in unseen.items():
                    candidate=scored.get(nid)
                    relevant=bool(candidate and (candidate['_score']>=settings.min_news_score or candidate['financial_topic_matches']))
                    c.execute('''INSERT INTO company_news_checks(company_id,company_revision,news_id,news_revision,
                      scoring_revision,compared_at,company_score,disposition,candidate_json) VALUES(?,?,?,?,?,?,?,?,?)''',
                      (company_id,crev,nid,n['input_hash'],srev,now(),candidate['_score'] if candidate else None,
                       'queued' if relevant else 'not_relevant',dumps(candidate) if relevant else None))
                waiting=[dict(r) for r in c.execute('''SELECT * FROM company_news_checks WHERE company_id=?
                  AND company_revision=? AND scoring_revision=? AND disposition='queued' AND assigned_run_id IS NULL''',(company_id,crev,srev))]
        waiting=[r for r in waiting if r['news_id'] in eligible and eligible[r['news_id']]['input_hash']==r['news_revision']]
        candidates=[json.loads(r['candidate_json']) for r in waiting]
        admission_skipped=0
        if candidate_admission is not None:
            admitted=candidate_admission(data,company_id,candidates)
            admission_skipped=len(candidates)-len(admitted)
            candidates=admitted
        candidates,without_analytics=supported_news(data,candidates,settings)
        duplicate_count=0
        selection_stats={}
        if candidate_filter is not None:
            with connect(db,True) as c:
                completed_rows=list(c.execute('''SELECT k.news_id,k.news_revision FROM company_news_checks k
                  JOIN company_report_batches b ON b.run_id=k.assigned_run_id AND b.company_id=k.company_id
                  WHERE k.company_id=? AND k.company_revision=? AND b.status='completed' ''',(company_id,crev)))
            completed={r[0] for r in completed_rows}
            revisions={(r[0],r[1]) for r in completed_rows}
            filter_context={**data,'completed_event_news':completed,'completed_news_revisions':revisions}
            filtered=candidate_filter(filter_context,company_id,candidates)
            selection_stats=filter_context.get('selection_stats',{})
            duplicate_count=len(candidates)-len(filtered)-selection_stats.get('interest_rate_quota_skipped',0)
            candidates=filtered
        chosen,coverage=choose_company_news(data,company_id,settings,candidates,True)
        stats=dict(new_comparisons=len(unseen),not_relevant=sum(nid not in scored or not (scored[nid]['_score']>=settings.min_news_score or scored[nid]['financial_topic_matches']) for nid in unseen),
                   queued_before_selection=len(waiting),selected_news=len(chosen),queued_after_selection=len(candidates)-len(chosen),
                   duplicate_events_skipped=duplicate_count,relevance_gate_skipped=admission_skipped,
                   without_analytics_skipped=len(without_analytics),**selection_stats)
        if not chosen:
            return dict(status='idle',company_id=company_id,report=None,saved=None,workflow=stats)
        report=retrieve(data,company_id,settings,selection=(chosen,coverage))
        # Packet-wide evidence limits can omit links even for supported news.
        # Such news must remain unassigned, rather than being acknowledged unseen.
        selected_ids={n['id'] for n in report_input(report)['news']}
        report['news']=[n for n in report['news'] if n['news_id'] in selected_ids]
        report['totals'].update(selected_news=len(report['news']),
            intersection_links=sum(len(n['analytics']) for n in report['news']),
            mechanism_candidates=sum(len(n['mechanism_candidates']) for n in report['news']))
        for topic in report.get('financial_topic_coverage',{}).get('topics',[]):
            previous_ids=topic.get('news_ids',[])
            topic['news_ids']=[nid for nid in previous_ids if nid in selected_ids]
            if previous_ids and not topic['news_ids']:topic['status']='omitted_no_selected_analytics'
        stats.update(selected_news=len(selected_ids),queued_after_selection=len(candidates)-len(selected_ids))
        if not selected_ids:
            return dict(status='idle',company_id=company_id,report=None,saved=None,workflow=stats)
        report['workflow']={**stats,'status':'prepared','company_revision':crev,'scoring_revision':srev,
                            'completion_required':True}
        refs={nid:eligible[nid]['input_hash'] for nid in selected_ids}
        def assign(c,run_id,folder,summary):
            require(_current(c,company_id,crev,refs),'Inputs changed while building report; retry')
            c.execute('''INSERT INTO company_report_batches(run_id,company_id,company_revision,scoring_revision,
              status,created_at,saved_json) VALUES(?,?,?,?,?,?,?)''',
              (run_id,company_id,crev,srev,'prepared',now(),dumps(dict(run_id=run_id,output=str(folder.resolve()),summary=summary))))
            for nid in selected_ids:
                cursor=c.execute('''UPDATE company_news_checks SET assigned_run_id=? WHERE company_id=? AND company_revision=?
                  AND news_id=? AND news_revision=? AND scoring_revision=? AND assigned_run_id IS NULL AND disposition='queued' ''',
                  (run_id,company_id,crev,nid,refs[nid],srev))
                require(cursor.rowcount==1,'News already assigned; retry')
        saved=save(db,output,[report],settings,model,stages or {},on_save=assign)
        return dict(status='prepared',company_id=company_id,report=report,saved=saved,workflow=stats)


def validate_result(result,packet):
    """Validate identity/coverage/references, not the LLM's business reasoning."""
    require(isinstance(result,dict) and result.get('schema_version')=='company-analysis-report-1.1','Expected company-analysis-report-1.1')
    require(result.get('request_id')==packet['request_id'],'Result request_id does not match this batch')
    require(result.get('company_id')==packet['company']['id'],'Result company_id does not match this batch')
    require(result.get('as_of')==packet['as_of'],'Result as_of does not match this batch')
    require(isinstance(result.get('summary'),str) and result['summary'].strip(),'Result summary is required')
    require(isinstance(result.get('items'),list),'Result items must be an array')
    items=result['items'];ids=[i.get('news_id') for i in items if isinstance(i,dict)]
    expected={n['id'] for n in packet['news']}
    require(len(ids)==len(items) and len(set(ids))==len(ids) and set(ids)==expected,'Result must cover every batch news_id exactly once, including rejected items')
    connections={c['id']:c for c in packet['connections']}
    sources={s['id']:s for s in packet['analytical_source_fragments']}
    fragments={n['id']:{f['id'] for f in n['fragments']} for n in packet['news']}
    for item in items:
        require('event_summary' not in item or isinstance(item['event_summary'],str),'event_summary must be a string')
        require(isinstance(item.get('news_fragment_ids'),list) and set(item['news_fragment_ids'])<=fragments[item['news_id']],'Unknown news fragment in result')
        require(isinstance(item.get('assessments'),list),'Result assessments must be an array')
        for assessment in item['assessments']:
            require(isinstance(assessment,dict),'Invalid assessment')
            link=connections.get(assessment.get('connection_id'))
            require(link and link['news_id']==item['news_id'],'Assessment refers to an unknown/different-news connection')
            require(assessment.get('news_mechanism_fit') in {'applicable','conditional','background','rejected'},'Invalid news_mechanism_fit')
            impact=assessment.get('company_impact',{})
            require(isinstance(impact,dict) and ('statement' not in impact or isinstance(impact['statement'],str)),'Invalid company_impact')
            evidence=assessment.get('evidence',[]);require(isinstance(evidence,list),'Assessment evidence must be an array')
            for ref in evidence:
                require(isinstance(ref,dict),'Invalid evidence reference')
                source=sources.get(ref.get('source_fragment_id'))
                require(source and source['id'] in link['source_fragment_ids'] and ref.get('mechanism_id')==link['mechanism_id'],'Evidence outside the selected connection')
                require(isinstance(ref.get('quote'),str) and ref['quote'].strip() and ref['quote'] in source['text'],'Evidence quote absent from source')
                require(isinstance(ref.get('pages'),list) and set(ref['pages'])<=set(source['pages']),'Evidence pages differ from source')
                require(ref.get('source_label')==source_provenance(source['document_id'])['label'],'Evidence source label differs from source')
    require(isinstance(result.get('company_context'),list) and isinstance(result.get('overall_limitations'),list),'Result context/limitations must be arrays')
    return result


def complete_batch(db,run_id,company_id,result):
    with workflow_lock(db),connect(db) as c:
        with c:
            c.execute('BEGIN IMMEDIATE');batch=_batch(c,run_id,company_id)
            require(batch['status']!='cancelled','Batch was cancelled after an input change')
            report=json.loads(batch['report_json']);packet=report_input(report)
            validate_result(result,packet)
            if batch['status']=='completed':
                require(dumps(result)==batch['result_json'],'Batch already completed with a different result')
                return dict(status='completed',run_id=run_id,company_id=company_id,idempotent=True)
            refs={r['news_id']:r['news_revision'] for r in c.execute(
                'SELECT news_id,news_revision FROM company_news_checks WHERE assigned_run_id=? AND company_id=?',(run_id,company_id))}
            require(_current(c,company_id,batch['company_revision'],refs),'Batch inputs changed; run compare to prepare an updated batch')
            saved=json.loads(batch['saved_json']);target=Path(saved['output'])/('llm-result-'+report_name(company_id).removeprefix('company_')+'.json')
            write_json(target,result)
            c.execute("UPDATE company_report_batches SET status='completed',completed_at=?,result_json=?,last_error=NULL WHERE run_id=? AND company_id=?",
                      (now(),dumps(result),run_id,company_id))
        return dict(status='completed',run_id=run_id,company_id=company_id,result_path=str(target.resolve()),idempotent=False)


def fail_batch(db,run_id,company_id,error,permanent=False):
    require(isinstance(error,str) and error.strip(),'Provide a nonempty failure reason')
    with workflow_lock(db),connect(db) as c:
        with c:
            batch=_batch(c,run_id,company_id);require(batch['status']=='prepared','Only a prepared batch can be retried')
            attempts=batch['failed_attempts']+1
            terminal=permanent or attempts>=MAX_REPORT_ATTEMPTS
            status='cancelled' if terminal else 'prepared'
            reason=('Report validation failed: ' if permanent else 'Report retry limit reached: ' if terminal else '')+error
            c.execute('''UPDATE company_report_batches SET status=?,last_error=?,failed_attempts=?
              WHERE run_id=? AND company_id=?''',(status,reason,attempts,run_id,company_id))
    return dict(status=status,run_id=run_id,company_id=company_id,failed_attempts=attempts,retry_same_packet=not terminal)


def retry_batch(db,run_id,company_id):
    """Explicitly release a held report; never silently recycle it in polling."""
    with workflow_lock(db),connect(db) as c:
        with c:
            c.execute('BEGIN IMMEDIATE')
            batch=_batch(c,run_id,company_id)
            require(batch['status']=='cancelled' and (batch['last_error'] or '').startswith(
                ('Report validation failed: ','Report retry limit reached: ')), 'Only a held failed report can be retried')
            require(not c.execute("SELECT 1 FROM company_report_batches WHERE company_id=? AND status='prepared'",(company_id,)).fetchone(),
                    'Finish the current prepared report before retrying this one')
            refs={r['news_id']:r['news_revision'] for r in c.execute(
                'SELECT news_id,news_revision FROM company_news_checks WHERE assigned_run_id=? AND company_id=?',(run_id,company_id))}
            require(refs and _current(c,company_id,batch['company_revision'],refs),'Inputs changed; run compare to prepare a current report')
            c.execute("UPDATE company_report_batches SET status='prepared',failed_attempts=0,last_error=NULL WHERE run_id=? AND company_id=?",(run_id,company_id))
    return dict(status='prepared',run_id=run_id,company_id=company_id)


def history(db,company_id=None,include_archived=False):
    with connect(db,True) as c:
        rows=c.execute('''SELECT run_id,company_id,status,created_at,completed_at,result_json,last_error,failed_attempts,saved_json
          FROM company_report_batches WHERE (? IS NULL OR company_id=?) ORDER BY created_at DESC''',(company_id,company_id)).fetchall()
        if not include_archived and c.execute("SELECT 1 FROM sqlite_master WHERE name='archived_report_batches'").fetchone():
            archived={tuple(r) for r in c.execute('SELECT run_id,company_id FROM archived_report_batches')}
            rows=[r for r in rows if (r['run_id'],r['company_id']) not in archived]
    return [dict(run_id=r['run_id'],company_id=r['company_id'],status=r['status'],created_at=r['created_at'],completed_at=r['completed_at'],
                 result=json.loads(r['result_json']) if r['result_json'] else None,last_error=r['last_error'],failed_attempts=r['failed_attempts'],
                 output=json.loads(r['saved_json'])['output']) for r in rows]


def batch_report(db,run_id,company_id):
    with connect(db,True) as c:batch=_batch(c,run_id,company_id)
    return json.loads(batch['report_json']),json.loads(batch['result_json']) if batch['result_json'] else None
