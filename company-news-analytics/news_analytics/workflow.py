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
from .retrieval import snapshot,score_company_news,choose_company_news,retrieve
from .reporting import save,report_name,markdown,html_reports
from .llm_io import report_input
from .analytics_sources import source_provenance


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


def process_company(db,output,model,company_id,settings,stages=None):
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
                if batch['company_revision']==crev and batch['scoring_revision']==srev and _current(c,company_id,crev,refs):
                    return _restore(batch)
                # Updated input must not be delivered as if it were the current one.
                with c:
                    c.execute("UPDATE company_report_batches SET status='cancelled',last_error='Input revision changed' WHERE run_id=? AND company_id=?",(prepared[0],company_id))
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
        chosen,coverage=choose_company_news(data,company_id,settings,candidates,True)
        stats=dict(new_comparisons=len(unseen),not_relevant=sum(nid not in scored or not (scored[nid]['_score']>=settings.min_news_score or scored[nid]['financial_topic_matches']) for nid in unseen),
                   queued_before_selection=len(waiting),selected_news=len(chosen),queued_after_selection=len(waiting)-len(chosen))
        if not chosen:
            return dict(status='idle',company_id=company_id,report=None,saved=None,workflow=stats)
        selected_ids={n['news_id'] for n in chosen}
        report=retrieve(data,company_id,settings,selection=(chosen,coverage))
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


def fail_batch(db,run_id,company_id,error):
    require(isinstance(error,str) and error.strip(),'Provide a nonempty failure reason')
    with workflow_lock(db),connect(db) as c:
        with c:
            batch=_batch(c,run_id,company_id);require(batch['status']=='prepared','Only a prepared batch can be retried')
            c.execute('''UPDATE company_report_batches SET last_error=?,failed_attempts=failed_attempts+1
              WHERE run_id=? AND company_id=?''',(error,run_id,company_id))
    return dict(status='prepared',run_id=run_id,company_id=company_id,retry_same_packet=True)


def history(db,company_id=None):
    with connect(db,True) as c:
        rows=c.execute('''SELECT run_id,company_id,status,created_at,completed_at,result_json,last_error,failed_attempts,saved_json
          FROM company_report_batches WHERE (? IS NULL OR company_id=?) ORDER BY created_at DESC''',(company_id,company_id)).fetchall()
    return [dict(run_id=r['run_id'],company_id=r['company_id'],status=r['status'],created_at=r['created_at'],completed_at=r['completed_at'],
                 result=json.loads(r['result_json']) if r['result_json'] else None,last_error=r['last_error'],failed_attempts=r['failed_attempts'],
                 output=json.loads(r['saved_json'])['output']) for r in rows]


def batch_report(db,run_id,company_id):
    with connect(db,True) as c:batch=_batch(c,run_id,company_id)
    return json.loads(batch['report_json']),json.loads(batch['result_json']) if batch['result_json'] else None
