from __future__ import annotations
import argparse,json,os,sqlite3,sys
from dataclasses import fields
from datetime import datetime,timezone,timedelta
from pathlib import Path
from .common import ROOT,require,read_json,write_json
from .database import initialize,connect,check
from .importing import import_all
from .embedding import identity,embed,reuse_legacy
from .retrieval import Settings
from .workflow import process_company,complete_batch,fail_batch,history

def path_value(value):
    path=Path(value).expanduser();return path.resolve() if path.is_absolute() else (ROOT/path).resolve()

def main(argv=None):
    if hasattr(sys.stdout,'reconfigure'):sys.stdout.reconfigure(encoding='utf-8')
    ap=argparse.ArgumentParser(description='Prepared JSON -> SQLite -> local embeddings -> two-index intersection report')
    sub=ap.add_subparsers(dest='command',required=True)
    for command in ('init','import','embed','compare','run','status','reuse-cache','complete','fail','history'):
        p=sub.add_parser(command);p.add_argument('--config',type=Path,default=ROOT/'config.json')
        p.add_argument('--db',type=Path);p.add_argument('--model',type=Path);p.add_argument('--output',type=Path)
        p.add_argument('--companies',type=Path);p.add_argument('--news',type=Path);p.add_argument('--analytics',type=Path)
        p.add_argument('--company');p.add_argument('--update-existing',action='store_true');p.add_argument('--legacy-db',type=Path)
        p.add_argument('--batch-size',type=int);p.add_argument('--threads',type=int)
        if command in {'complete','fail'}:p.add_argument('--run-id',required=True)
        if command=='complete':p.add_argument('--result',type=Path,required=True)
        if command=='fail':p.add_argument('--error',required=True)
        for field in fields(Settings):p.add_argument('--'+field.name.replace('_','-'),type=str if field.name=='as_of' else float if field.name.startswith('min_') else int)
    args=ap.parse_args(argv)
    try:
        cfg=read_json(args.config);db=path_value(args.db or cfg['database'])
        output=path_value(args.output or cfg['reports'])
        modelpath=path_value(args.model or os.environ.get('EMBEDDING_MODEL_PATH') or cfg['model_path'])
        if args.command=='init':initialize(db);print('Initialized: '+str(db));return 0
        if args.command in {'complete','fail','history'}:
            initialize(db)
            if args.command=='history':result=history(db,args.company)
            else:
                require(args.company,'--company required')
                result=complete_batch(db,args.run_id,args.company,read_json(args.result)) if args.command=='complete' else fail_batch(db,args.run_id,args.company,args.error)
            print(json.dumps(result,ensure_ascii=False,indent=2));return 0
        if args.command=='status':
            initialize(db)
            with connect(db,True) as c:
                check(c);counts={t:c.execute('SELECT count(*) FROM '+t).fetchone()[0] for t in ('companies','news','article_chunks','analytics','analytics_chunks','analytics_mechanisms','comparison_runs')}
                counts['embedding_roles']={r['role']:dict(total=r['total'],with_vectors=r['ready']) for r in c.execute('SELECT role,count(*) total,sum(embedding IS NOT NULL) ready FROM active_embedding_inputs GROUP BY role')}
                counts['workflow']={r['status']:r['n'] for r in c.execute('SELECT status,count(*) n FROM company_report_batches GROUP BY status')}
                counts['company_news_checks']=c.execute('SELECT count(*) FROM company_news_checks').fetchone()[0]
            print(json.dumps(counts,ensure_ascii=False,indent=2));return 0
        initialize(db);stages={}
        if args.command in {'import','run'}:
            locations={kind:path_value(getattr(args,kind) or cfg['inputs'][kind]) for kind in ('companies','news','analytics')}
            # An explicit input flag on import selects only the provided kinds.
            if args.command=='import' and any(getattr(args,k) for k in locations):locations={k:v for k,v in locations.items() if getattr(args,k)}
            stages['import']=import_all(db,locations,update_existing=args.update_existing,skip_invalid_news=cfg.get('import',{}).get('skip_invalid_news',False))
            print('Import: '+json.dumps(stages['import'],ensure_ascii=False),flush=True)
            if args.command=='import':return 0
        model=identity(modelpath)
        if args.command=='reuse-cache':
            require(args.legacy_db is not None,'--legacy-db required')
            print(json.dumps(reuse_legacy(db,args.legacy_db,model),indent=2));return 0
        if args.command in {'embed','run'}:
            options=cfg.get('embedding',{});stages['embedding']=embed(db,modelpath,model,
              batch_size=args.batch_size if args.batch_size is not None else options.get('batch_size',4),
              threads=args.threads if args.threads is not None else options.get('threads',4))
            print('Embedding: '+json.dumps(stages['embedding'],ensure_ascii=False),flush=True)
            if args.command=='embed':return 0
        configured=cfg.get('retrieval',{})
        setting_values={field.name:getattr(args,field.name) if getattr(args,field.name) is not None else configured[field.name]
                        for field in fields(Settings) if getattr(args,field.name) is not None or field.name in configured}
        setting_values.setdefault('as_of',datetime.now(timezone(timedelta(hours=3))).date().isoformat())
        settings=Settings(**setting_values).validate()
        with connect(db,True) as c:known_ids={r[0] for r in c.execute('SELECT id FROM companies')}
        ids=[args.company] if args.company else sorted(known_ids)
        require(ids,'No companies imported');require(set(ids)<=known_ids,'Unknown company ID')
        results=[]
        for cid in ids:
            item=process_company(db,output,model,cid,settings,stages)
            results.append({**(item['saved'] or {}),'status':item['status'],'company_id':cid,'workflow':item['workflow']})
        result=results[0] if len(results)==1 else dict(results=results)
        print(json.dumps(result,ensure_ascii=False,indent=2));return 0
    except (ValueError,KeyError,TypeError,IndexError,OSError,sqlite3.Error,ImportError,RuntimeError) as exc:
        print('ERROR: '+str(exc),file=sys.stderr);return 1

if __name__=='__main__':raise SystemExit(main())
