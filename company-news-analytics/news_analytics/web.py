"""Local browser UI around the existing importer, embeddings and retrieval.

No additional dependencies. A single background job serializes writes/model access.
Uploads persist in configured input folders; searches save ordinary project reports.
"""
from __future__ import annotations
import argparse,copy,json,mimetypes,os,sqlite3,tempfile,threading,traceback,webbrowser
from concurrent.futures import ThreadPoolExecutor
from dataclasses import fields
from datetime import datetime,timezone,timedelta
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit,parse_qs
from uuid import uuid4
from .common import ROOT,read_json,write_json,dumps,sha,require
from .database import initialize,connect
from .validation import VALIDATORS
from .importing import import_all
from .embedding import identity,embed
from .retrieval import Settings,snapshot,retrieve
from .reporting import save,report_name,html_reports
from .llm_io import report_input
from .workflow import process_company,complete_batch,history,batch_report

STATIC=Path(__file__).with_name('web_static')
MAX_BODY=32*1024*1024

def resolve(value):
    p=Path(value).expanduser();return p.resolve() if p.is_absolute() else (ROOT/p).resolve()

def detect(data):
    require(isinstance(data,dict),'Ожидается JSON-объект, один документ в файле.')
    version=data.get('schema_version','')
    require(isinstance(version,str),'schema_version должен быть строкой.')
    if version.startswith('company-profile-'):return 'companies'
    if version in {'news-two-views-1.0','news-dual-embeddings-2.0'}:return 'news'
    if version=='analytics-contextual-mechanisms-2.0':return 'analytics'
    raise ValueError('Неизвестная схема JSON. Нужен подготовленный JSON компании, новости или аналитики.')

def install_uploads(db,folders,files,skip_invalid_news=False):
    """Validate before replacing canonical files; optionally skip invalid news.

    Restore canonical files if the transactional importer rejects the batch. The
    importer backs up SQLite and rejects rewriting archived source text under old IDs.
    Filenames supplied by a browser are never used as filesystem paths.
    """
    require(isinstance(files,list) and 0<len(files)<=100,'Выберите от 1 до 100 JSON-файлов.')
    parsed=[];seen=set();rejected=[]
    with tempfile.TemporaryDirectory(prefix='news-ui-') as temp:
        for index,f in enumerate(files):
            require(isinstance(f,dict) and isinstance(f.get('content'),str),'Файл должен содержать текст JSON.')
            name=str(f.get('name','file.json'))[:200]
            raw=Path(temp)/f'{index}.json';raw.write_text(f['content'],encoding='utf-8')
            kind=None
            try:
                data=read_json(raw)
                if isinstance(data,dict) and 'document_id' in data and ('source' in data or 'fragments' in data):kind='news'
                kind=detect(data);data=VALIDATORS[kind](data)
                uid=data['subject_id'] if kind=='companies' else data['document_id'] if kind=='news' else data['document']['id']
                require((kind,uid) not in seen,'В пакете повторяется идентификатор: '+uid);seen.add((kind,uid))
                folder=Path(folders[kind]).resolve();folder.mkdir(parents=True,exist_ok=True)
                require(folder.is_dir(),'Путь входных материалов должен быть папкой: '+str(folder))
                target=folder/('uploaded-'+sha(uid)[:24]+'.json')
                # Reuse the canonical file for this ID, so CLI scans never see duplicates.
                matches=[]
                for p in folder.rglob('*.json'):
                    try:existing=read_json(p)
                    except (ValueError,UnicodeError):continue
                    if not isinstance(existing,dict):continue
                    eid=existing.get('subject_id') if kind=='companies' else existing.get('document_id') if kind=='news' else existing.get('document',{}).get('id')
                    if eid==uid:matches.append(p.resolve())
                require(len(matches)<=1,'В папке уже несколько файлов с ID '+uid)
                if matches:target=matches[0]
                require(target.is_relative_to(folder),'Неверный путь файла.')
                parsed.append((kind,uid,target,data))
            except (ValueError,KeyError,TypeError,IndexError) as exc:
                if not (skip_invalid_news and kind=='news'):raise ValueError(name+': '+str(exc)) from exc
                rejected.append(dict(file=name,reason=str(exc),stage='validation'))
        previous={p:p.read_bytes() if p.exists() else None for _,_,p,_ in parsed}
        committed=False
        try:
            for _,_,p,data in parsed:write_json(p,data)
            # Import only this batch, not unrelated JSON lying in those folders.
            locations={}
            for kind,uid,p,data in parsed:
                staged=Path(temp)/kind;staged.mkdir(exist_ok=True)
                write_json(staged/(sha(uid)+'.json'),data);locations[kind]=staged
            summary=import_all(db,locations,update_existing=True,skip_invalid_news=skip_invalid_news)
            summary['rejected_news'].extend(rejected);summary['rejected_count']=len(summary['rejected_news'])
            # Rejected documents must not become canonical input files.
            failed={Path(r['file']).stem for r in summary['rejected_news'] if Path(r['file']).parent==Path(temp)/'news'}
            for kind,uid,p,_ in parsed:
                if sha(uid) in failed:
                    if previous[p] is None:p.unlink(missing_ok=True)
                    else:p.write_bytes(previous[p])
            committed=True
        finally:
            if not committed:
                for p,original in previous.items():
                    if original is None:p.unlink(missing_ok=True)
                    else:p.write_bytes(original)
        with connect(db) as c:
            with c:
                for kind,uid,p,_ in parsed:
                    if sha(uid) in failed:continue
                    effective=next((a['database_id'] for a in summary['news_identity_aliases'] if a['incoming_id']==uid),uid) if kind=='news' else uid
                    c.execute('UPDATE '+kind+' SET input_file=? WHERE id=?',(str(p),effective))
    if summary['rejected_news']:
        log=Path(db).parent/'import-logs'/('upload-'+uuid4().hex+'.json');write_json(log,summary);summary['rejection_log']=str(log)
    return summary

class Application:
    def __init__(self,config,db=None,model=None,output=None):
        self.config=read_json(config);self.db=initialize(resolve(db or self.config['database']))
        self.model_path=resolve(model or os.environ.get('EMBEDDING_MODEL_PATH') or self.config['model_path'])
        self.output=resolve(output or self.config['reports'])
        self.folders={k:resolve(v) for k,v in self.config['inputs'].items()}
        # A fresh checkout/container includes prepared inputs but no tracked SQLite DB.
        # Import those without replacing existing versions; inference starts on demand.
        initial={k:p for k,p in self.folders.items() if p.is_dir() and any(p.rglob('*.json'))}
        self.skip_invalid_news=self.config.get('import',{}).get('skip_invalid_news',False)
        self.startup_import=import_all(self.db,initial,skip_invalid_news=self.skip_invalid_news) if initial else None
        # Source folders can be sealed datasets; uploads always go to managed folders.
        self.folders.update({k:resolve(v) for k,v in self.config.get('upload_folders',{}).items()})
        managed={k:p for k,p in self.folders.items() if p.is_dir() and p!=initial.get(k) and any(p.rglob('*.json'))}
        if managed:import_all(self.db,managed,skip_invalid_news=self.skip_invalid_news)
        if self.startup_import and self.startup_import['rejected_count']:
            write_json(Path(self.db).parent/'import-logs/startup-latest.json',self.startup_import)
        self.settings=dict(self.config.get('retrieval',{}))
        self.pool=ThreadPoolExecutor(max_workers=1);self.lock=threading.Lock();self.jobs={};self.active=None

    def state(self):
        with connect(self.db,True) as c:
            companies=[]
            for r in c.execute('SELECT id,profile_text,source_json FROM companies ORDER BY id'):
                d=json.loads(r['source_json']);companies.append(dict(id=r['id'],profile=r['profile_text'],type=d.get('subject_type','company')))
            counts={k:c.execute('SELECT count(*) FROM '+k).fetchone()[0] for k in ('companies','news','analytics')}
            counts['analytics_chunks']=c.execute('SELECT count(*) FROM analytics_chunks WHERE is_active=1').fetchone()[0]
            counts['pending_vectors']=c.execute('SELECT count(*) FROM active_embedding_inputs WHERE embedding IS NULL').fetchone()[0]
            latest=c.execute('SELECT max(publication_date) FROM news').fetchone()[0]
        with self.lock:active=copy.deepcopy(self.jobs.get(self.active))
        return dict(companies=companies,counts=counts,latest_news_date=latest,
          today=datetime.now(timezone(timedelta(hours=3))).date().isoformat(),settings=self.settings,active_job=active,startup_import=self.startup_import)

    def get_job(self,jid):
        with self.lock:return copy.deepcopy(self.jobs.get(jid))

    def start(self,kind,payload):
        require(kind in {'import','search','complete'},'Неизвестная операция.')
        with self.lock:
            if self.active and self.jobs[self.active]['status']=='running':raise BlockingIOError('Дождитесь завершения текущей операции.')
            jid=uuid4().hex
            self.jobs[jid]=dict(id=jid,kind=kind,status='running',phase='Подготовка…')
            self.active=jid
            while len(self.jobs)>20:del self.jobs[next(iter(self.jobs))]
        self.pool.submit(self._work,jid,kind,payload)
        return dict(job_id=jid)

    def _update(self,jid,**values):
        with self.lock:self.jobs[jid].update(values)

    def _work(self,jid,kind,payload):
        try:
            stages={}
            if kind=='complete':
                self._update(jid,phase='Проверяем и сохраняем результат LLM…')
                result=complete_batch(self.db,payload.get('run_id'),payload.get('company_id'),payload.get('result'))
                self._update(jid,status='complete',phase='Результат сохранён в истории компании.',completion=result)
                return
            if kind=='import':
                self._update(jid,phase='Проверяем JSON и сохраняем материалы…')
                stages['import']=install_uploads(self.db,self.folders,payload.get('files'),skip_invalid_news=self.skip_invalid_news)
                self._update(jid,phase='Материалы сохранены. Подготавливаем их к поиску…',import_result=stages['import'])
            else:
                require(isinstance(payload.get('company_id'),str),'Выберите компанию.')
                values={f.name:self.settings[f.name] for f in fields(Settings) if f.name in self.settings}
                values.update(as_of=payload.get('as_of') or datetime.now(timezone(timedelta(hours=3))).date().isoformat())
                # UI deliberately exposes only result count and date, not hidden tuning.
                if 'top_news' in payload:values['top_news']=payload['top_news']
                settings=Settings(**values).validate()
                require(settings.top_news<=50,'Можно показать не более 50 новостей за один поиск.')
                with connect(self.db,True) as c:require(c.execute('SELECT 1 FROM companies WHERE id=?',(payload['company_id'],)).fetchone(),'Компания не найдена.')
                self._update(jid,phase='Проверяем готовность материалов к поиску…')
            model=identity(self.model_path)
            options=self.config.get('embedding',{})
            stages['embedding']=embed(self.db,self.model_path,model,batch_size=options.get('batch_size',4),threads=options.get('threads',4))
            if kind=='import':
                self._update(jid,status='complete',phase='Материалы готовы к поиску.',stages=stages)
            else:
                self._update(jid,phase='Подбираем новости и связанную аналитику…')
                item=process_company(self.db,self.output,model,payload['company_id'],settings,stages)
                phase='Новых подходящих новостей нет.' if item['status']=='idle' else 'Пакет ожидает результата LLM.' if item['status']=='resumed' else 'Новая партия подготовлена для LLM.'
                self._update(jid,status='complete',phase=phase,report=item['report'],saved=item['saved'],workflow_status=item['status'],workflow=item['workflow'])
        except Exception as exc:
            traceback.print_exc()
            self._update(jid,status='failed',phase='Не удалось завершить операцию.',error=str(exc))

class Handler(BaseHTTPRequestHandler):
    server_version='NewsAnalyticsLocal/1.0'
    def send(self,status,body,content_type='application/json; charset=utf-8',attachment=None):
        if not isinstance(body,bytes):body=dumps(body).encode('utf-8')
        self.send_response(status);self.send_header('Content-Type',content_type);self.send_header('Content-Length',str(len(body)))
        self.send_header('Cache-Control','no-store');self.send_header('X-Content-Type-Options','nosniff')
        self.send_header('Content-Security-Policy',"default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'")
        if attachment:self.send_header('Content-Disposition','attachment; filename="'+attachment+'"')
        self.end_headers();self.wfile.write(body)

    def do_GET(self):
        path=urlsplit(self.path).path;app=self.server.app
        try:
            if path in ('/','/app.js','/style.css'):
                name='index.html' if path=='/' else path[1:]
                content_type={'index.html':'text/html','app.js':'text/javascript','style.css':'text/css'}[name]
                return self.send(200,(STATIC/name).read_bytes(),content_type+'; charset=utf-8')
            if path=='/api/state':return self.send(200,app.state())
            if path=='/api/history':
                company=parse_qs(urlsplit(self.path).query).get('company',[None])[0]
                return self.send(200,history(app.db,company))
            if path=='/api/batch':
                query=parse_qs(urlsplit(self.path).query)
                run_id=query.get('run_id',[''])[0];company=query.get('company_id',[''])[0];fmt=query.get('format',['json'])[0]
                require(fmt in {'json','llm','html','result'},'Неизвестный формат.')
                report,result=batch_report(app.db,run_id,company)
                if fmt=='result':require(result is not None,'Ответ LLM ещё не сохранён.')
                body=report_input(report) if fmt=='llm' else html_reports([report],run_id).encode('utf-8') if fmt=='html' else result if fmt=='result' else report
                return self.send(200,body,'text/html; charset=utf-8' if fmt=='html' else 'application/json; charset=utf-8',attachment='company-batch.'+('html' if fmt=='html' else 'json'))
            if path=='/favicon.ico':return self.send(204,b'','image/x-icon')
            if path.startswith('/api/jobs/'):
                job=app.get_job(path.removeprefix('/api/jobs/'))
                return self.send(200,job) if job else self.send(404,{'error':'Операция не найдена.'})
            if path.startswith('/api/download/'):
                _,_,_,jid,fmt=path.split('/')
                job=app.get_job(jid);require(job and job.get('saved') and fmt in {'json','html','llm'},'Отчёт не найден.')
                folder=Path(job['saved']['output'])
                name=report_name(job['report']['company_id'])
                p=folder/(name+'.json' if fmt=='json' else 'llm-input-'+name.removeprefix('company_')+'.json' if fmt=='llm' else 'report.html')
                return self.send(200,p.read_bytes(),'text/html; charset=utf-8' if fmt=='html' else 'application/json; charset=utf-8',attachment='llm-input.json' if fmt=='llm' else 'company-search.'+fmt)
            self.send(404,{'error':'Страница не найдена.'})
        except (ValueError,OSError,sqlite3.Error) as exc:self.send(400,{'error':str(exc)})

    def do_POST(self):
        # Cross-origin sites cannot trigger local imports or consume CPU inference.
        if self.headers.get('X-Requested-With')!='news-analytics-ui':return self.send(403,{'error':'Запрос должен быть отправлен из локального интерфейса.'})
        if self.headers.get('Content-Type','').split(';')[0]!='application/json':return self.send(415,{'error':'Ожидается JSON.'})
        try:
            length=int(self.headers.get('Content-Length','0'));require(0<length<=MAX_BODY,'Максимальный размер запроса — 32 МБ.')
            payload=json.loads(self.rfile.read(length));require(isinstance(payload,dict),'Ожидается JSON-объект.')
            path=urlsplit(self.path).path
            require(path in {'/api/import','/api/search','/api/complete'},'Неизвестная операция.')
            self.send(202,self.server.app.start(path.rsplit('/',1)[1],payload))
        except BlockingIOError as exc:self.send(409,{'error':str(exc)})
        except (ValueError,KeyError,TypeError) as exc:self.send(400,{'error':str(exc)})

def main(argv=None):
    import sys
    if hasattr(sys.stdout,'reconfigure'):sys.stdout.reconfigure(encoding='utf-8')
    p=argparse.ArgumentParser(description='Локальный интерфейс поиска новостей и аналитики')
    p.add_argument('--config',type=Path,default=ROOT/'config.json');p.add_argument('--db',type=Path)
    p.add_argument('--model',type=Path);p.add_argument('--output',type=Path)
    p.add_argument('--host',default='127.0.0.1');p.add_argument('--port',type=int,default=8765)
    p.add_argument('--no-browser',action='store_true');args=p.parse_args(argv)
    app=Application(args.config,args.db,args.model,args.output)
    try:server=ThreadingHTTPServer((args.host,args.port),Handler)
    except OSError as exc:p.exit(1,'Не удалось открыть адрес интерфейса: '+str(exc)+'\n')
    server.app=app
    url=f'http://127.0.0.1:{server.server_port}'
    print('Интерфейс: '+url+'\nДля остановки нажмите Ctrl+C.',flush=True)
    if not args.no_browser:webbrowser.open(url)
    try:server.serve_forever()
    except KeyboardInterrupt:pass
    finally:server.server_close();app.pool.shutdown(wait=True)

if __name__=='__main__':main()
