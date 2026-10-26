"""One stateful service for CLI, UI and scheduled batches; real embeddings only."""
from dataclasses import asdict, replace
from functools import partial
from datetime import date
from pathlib import Path
import json
import os
import sqlite3
import subprocess
import sys
import time

from news_analytics.common import dumps, now, read_json, require, sha, write_json
from news_analytics.database import connect, initialize
from news_analytics.embedding import embed, identity, reuse_legacy
from news_analytics.importing import import_all
from news_analytics.llm_io import report_input
from news_analytics.reporting import report_name
from news_analytics.validation import VALIDATORS
from news_analytics.retention import cleanup_news,news_cutoff
from news_analytics.workflow import (batch_report, complete_batch, fail_batch, history,
                                     process_company, workflow_lock, retry_batch)
from src.report.errors import ReportValidationError
from .adapter import result_for_batch
from .settings import ROOT, llm_ready, retrieval
from .events import filter_candidates, event_key, story_key
from .financial_events import (identify as financial_identity, completed as completed_financial,
                               same_fact, remember_source, sources as financial_sources,
                               initialize as initialize_financial)
from .admission import filter_admitted, policy
from . import topic_quota


class Service:
    def __init__(self, config):
        self.config = config
        self.db = initialize(config['database'])
        self.output = config['reports']
        self._identity = None
        self._model_signature = None
        self._tokenizer = None
        self._embedding_runtime = {}
        self._cleanup_at = 0
        self._cleanup_cutoff = None
        for name in ('reports', 'parser_directory', 'prepared_news'):
            config[name].mkdir(parents=True, exist_ok=True)
        os.environ.setdefault('LLM_CACHE_DIR', str(self.db.parent / 'llm-cache'))
        with connect(self.db) as c:
            with c:
                c.execute('''CREATE TABLE IF NOT EXISTS news_preparation_jobs (
                    source_id TEXT NOT NULL, source_hash TEXT NOT NULL, prompt_version TEXT NOT NULL,
                    status TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0,
                    prepared_path TEXT, last_error TEXT, updated_at TEXT NOT NULL,
                    PRIMARY KEY(source_id,source_hash,prompt_version))''')
            from .preparation_retry import initialize as initialize_retry
            with c:
                initialize_retry(c)
                initialize_financial(c)

    def lock(self):
        # Distinct from the outbox lock, which is acquired by process/complete.
        return workflow_lock(self.db.parent / 'integration')

    def news_min_date(self,as_of=None):
        return news_cutoff(self.config.get('pipeline',{}).get('news_retention_days',10),as_of)

    def _cleanup(self,as_of=None,dry_run=False,force=False):
        cutoff=self.news_min_date(as_of)
        interval=self.config.get('pipeline',{}).get('cleanup_interval_seconds',3600)
        require(type(interval) is int and interval>=60,'cleanup_interval_seconds must be at least 60')
        if not (force or dry_run or cutoff!=self._cleanup_cutoff or time.monotonic()-self._cleanup_at>=interval):
            return dict(status='not_due',cutoff=cutoff)
        from src.parser.retention import cleanup_parser
        parser=cleanup_parser(self.config['parser_directory']/'news.db',cutoff,dry_run)
        result=cleanup_news(self.db,self.config.get('pipeline',{}).get('news_retention_days',10),as_of,dry_run,parser.get('source_ids',[]))
        result['parser']={k:v for k,v in parser.items() if k!='source_ids'}
        if not dry_run:
            write_json(self.db.parent/'news-retention.json',result)
            # Retry a busy parser on the next cycle; never hold its DB lock while waiting.
            if not parser.get('deferred'):
                self._cleanup_at=time.monotonic();self._cleanup_cutoff=cutoff
        return result

    def cleanup(self,as_of=None,dry_run=False):
        with self.lock():return self._cleanup(as_of,dry_run,force=True)

    def bootstrap(self):
        with self.lock():
            results = []
            for key in ('inputs', 'upload_folders'):
                locations = {k: p for k, p in self.config[key].items()
                             if p.is_dir() and any(p.rglob('*.json'))}
                if locations:
                    results.append(import_all(self.db, locations, update_existing=key == 'upload_folders',
                                              skip_invalid_news=True,min_news_date=self.news_min_date()))
            self._cleanup()
            return results

    def status(self):
        with connect(self.db, True) as c:
            counts = {t: c.execute('SELECT count(*) FROM ' + t).fetchone()[0]
                      for t in ('companies', 'news', 'analytics', 'company_news_checks')}
            counts['pending_vectors'] = c.execute('SELECT count(*) FROM active_embedding_inputs WHERE embedding IS NULL').fetchone()[0]
            jobs = {r['status']: r['n'] for r in c.execute('SELECT status,count(*) n FROM news_preparation_jobs GROUP BY status')}
        model = self.config['model_path']
        from .preparation_retry import pause
        return dict(database=str(self.db), counts=counts, news_preparation=jobs,news_preparation_pause=pause(self.db),
                    report_mode=self.config.get('pipeline',{}).get('report_mode','standard'),selected_company_id=self.selected_company(),
                    model_available=model.is_dir() and all((model / name).is_file() for name in
                        ('model.safetensors','config.json','config_sentence_transformers.json','modules.json','tokenizer.json')),
                    llm_configured=llm_ready(), llm_connection_verified=False,
                    parser_db=str(self.config['parser_directory'] / 'news.db'))

    def selected_company(self):
        target = self.db.parent / 'selected-company.json'
        if not target.is_file():
            return None
        selected = read_json(target).get('company_id')
        with connect(self.db, True) as c:
            return selected if c.execute('SELECT 1 FROM companies WHERE id=?', (selected,)).fetchone() else None

    def select_company(self, company_id):
        require(isinstance(company_id, str) and company_id.strip(), 'Выберите компанию')
        with connect(self.db, True) as c:
            require(c.execute('SELECT 1 FROM companies WHERE id=?', (company_id,)).fetchone(), 'Компания не найдена')
        with workflow_lock(self.db.parent / 'company-selection'):
            write_json(self.db.parent / 'selected-company.json', dict(company_id=company_id, updated_at=now()))
        return dict(selected_company_id=company_id)

    def import_inputs(self, locations):
        with self.lock():
            return import_all(self.db, locations, update_existing=True, skip_invalid_news=True,min_news_date=self.news_min_date())

    def add_company(self, identifier, progress=lambda phase: None):
        from .onboarding import company_id, prepared_profile, build_profile
        from news_analytics.web import install_uploads
        identifier = company_id(identifier)
        # The web worker is serialized with tick/import/inference; CLI callers
        # also take the same OS lock. Recheck here, after any queued work.
        with self.lock():
            with connect(self.db, True) as c:
                exists = c.execute('SELECT 1 FROM companies WHERE id=?', (identifier,)).fetchone()
            source = 'database'
            if not exists:
                doc = prepared_profile(self.config, identifier)
                source = 'json' if doc else 'csv'
                if doc is None:
                    progress('Собираем сведения из CSV и готовим профиль через LLM…')
                    doc = build_profile(self.config, identifier)
                require(doc['subject_id'] == identifier, 'ID подготовленного профиля не совпадает с запрошенной компанией.')
                progress('Проверяем и сохраняем профиль…')
                # The existing uploader validates, atomically installs the JSON
                # and rolls it back if the transactional import fails.
                install_uploads(self.db, self.config['upload_folders'],
                                [dict(name=identifier+'.json', content=dumps(doc))])
            progress('Подготавливаем компанию к подбору новостей…')
            options = self.config.get('embedding', {})
            vectors = embed(self.db, self.config['model_path'], self.model(),
                            batch_size=options.get('batch_size', 4), threads=options.get('threads', 4),
                            runtime=self._embedding_runtime, company_id=identifier)
            return dict(company_id=identifier, source=source, embedding=vectors)

    def model(self):
        directory = self.config['model_path']
        require(directory.is_dir(), 'Укажите папку EmbeddingGemma в EMBEDDING_MODEL_PATH')
        signature = tuple((str(p.relative_to(directory)), p.stat().st_size, p.stat().st_mtime_ns)
                          for p in sorted(directory.rglob('*')) if p.is_file() and not any(x.startswith('.') for x in p.relative_to(directory).parts))
        if signature != self._model_signature:
            self._identity = identity(directory)
            self._model_signature = signature
            self._tokenizer = None
        return self._identity

    def _embed(self):
        options = self.config.get('embedding', {})
        return embed(self.db, self.config['model_path'], self.model(),
                     batch_size=options.get('batch_size', 4), threads=options.get('threads', 4),
                     runtime=self._embedding_runtime)

    def embed(self):
        with self.lock():
            self._cleanup()
            return self._embed()

    def reuse_cache(self, legacy_db):
        with self.lock():
            return reuse_legacy(self.db, legacy_db, self.model())

    def _candidate_filter(self,data,company_id,candidates):
        company=next(c for c in data['companies'] if c['id']==company_id)
        previous=completed_financial(self.db,company_id,company['input_hash'])
        candidates=filter_candidates({**data,'completed_financial_events':previous},company_id,candidates)
        docs={n['id']:json.loads(n['source_json']) for n in data['news']}
        candidates,stats=topic_quota.filter_candidates(self.db,company_id,docs,candidates,topic_quota.hours(self.config))
        data.setdefault('selection_stats',{}).update(stats)
        return candidates

    def _compare(self, company_id, as_of=None, top_news=None, feed=False):
        require(isinstance(company_id, str) and company_id.strip(), 'Выберите компанию')
        require(top_news is None or type(top_news) is int and 1 <= top_news <= 50, 'Можно выбрать от 1 до 50 новостей')
        with connect(self.db, True) as c:
            require(c.execute('SELECT 1 FROM companies WHERE id=?', (company_id,)).fetchone(), 'Компания не найдена')
        stages = {'cleanup':self._cleanup(as_of),'embedding': self._embed()}
        options=retrieval(self.config, as_of, top_news)
        if feed:
            # Several news per report: one false retrieval hit must not empty it.
            limit=min(options.top_news,options.max_total_news or options.top_news)
            options=replace(options,top_news=limit,max_total_news=limit)
        return process_company(self.db, self.output, self.model(), company_id,
                               options, stages, candidate_filter=self._candidate_filter,
                               candidate_admission=partial(filter_admitted, **policy(self.config)))

    def compare(self, company_id, as_of=None, top_news=None):
        with self.lock():
            return self._compare(company_id, as_of, top_news)

    def _generate(self, company_id, as_of=None, top_news=None, client=None, prepared=None, feed=False):
        item = prepared or self._compare(company_id, as_of, top_news, feed)
        if item['status'] == 'idle':
            return item
        saved = item['saved']
        run_id = saved['run_id']
        packet = report_input(item['report'])
        permitted,_=topic_quota.filter_candidates(self.db,company_id,
            {n['news_id']:n for n in item['report']['news']},item['report']['news'],topic_quota.hours(self.config))
        if (len(permitted)!=len(item['report']['news']) or
                {n['id'] for n in packet['news']} != {n['news_id'] for n in item['report']['news']}):
            # Explicitly finishing a saved old batch must also respect the current gate.
            return self._generate(company_id,as_of or item['report']['as_of'],top_news,client,feed=feed)
        folder = Path(saved['output'])
        result_path = folder / ('llm-result-' + report_name(company_id).removeprefix('company_') + '.json')
        try:
            # A crash after saving but before acknowledging never triggers another paid call.
            if result_path.is_file():
                result = read_json(result_path)
            else:
                require(client is not None or llm_ready(), 'Настройте LLM_BASE_URL, LLM_API_KEY и LLM_MODEL_MAIN в .env')
                options=self.config.get('pipeline',{})
                if options.get('report_mode','standard')=='deep':
                    from src.report.deep import generate
                    from .report_context import extras_for_report,used_catalog
                    extras=extras_for_report(self.db,item['report'])
                    packet['market_indicators']=extras['market_indicators']
                    document,trace=generate(packet,extras,client=client,critic=options.get('critic',True),
                                            budget=options.get('report_budget_rub',20.0))
                    document['evidence_catalog']=used_catalog(document,extras)
                else:
                    from src.report.pipeline import generate
                    document, trace = generate(packet, client=client, critic=options.get('critic', True))
                result = result_for_batch(packet, document)
                write_json(folder / 'agent-report.json', document)
                write_json(folder / 'agent-trace.json', trace)
                from src.report.render import to_markdown
                (folder / 'agent-report.md').write_text(to_markdown(document), encoding='utf-8')
            # Keep the existing grouping identity in the permanent report snapshot.
            # Removing an expired source must not create a new-looking feed card.
            references=financial_sources(self.db)
            with connect(self.db,True) as c:
                for entry in result.get('items',[]):
                    row=c.execute('SELECT source_json FROM news WHERE id=?',(entry['news_id'],)).fetchone()
                    if row:
                        original=json.loads(row['source_json'])
                        financial=financial_identity(original)
                        entry['feed_identity']=dict(event_key=event_key(original),story_key=story_key(original),financial=financial,
                            quota_topic=topic_quota.TOPIC if topic_quota.is_rate_news({**original,**entry}) else None,
                            financial_sources=[ref for identity,ref in references if same_fact(financial,identity)])
            completed = complete_batch(self.db, run_id, company_id, result)
            return {**item, 'status': 'completed', 'result': result, 'completion': completed}
        except Exception as error:
            failure=fail_batch(self.db, run_id, company_id, str(error),permanent=isinstance(error,ReportValidationError))
            write_json(folder/'report-failure.json',dict(**failure,error=str(error),updated_at=now()))
            if not failure['retry_same_packet']:
                return {**item,'status':'skipped','error':str(error),'failure':failure}
            raise

    def generate(self, company_id, as_of=None, top_news=None, client=None, feed=False):
        with self.lock():
            return self._generate(company_id, as_of, top_news, client, feed=feed)

    def retry_report(self,company_id,run_id):
        with self.lock():
            return retry_batch(self.db,run_id,company_id)

    def finish(self, company_id, run_id, client=None):
        with self.lock():
            self._cleanup()
            report, result = batch_report(self.db, run_id, company_id)
            if result is not None:
                return dict(status='completed', result=result, completion=dict(idempotent=True, run_id=run_id, company_id=company_id))
            record = next(r for r in history(self.db, company_id) if r['run_id'] == run_id)
            require(record['status'] == 'prepared', 'Эта партия отменена')
            prepared = dict(status='resumed', report=report,
                            saved=dict(run_id=run_id, output=record['output']), workflow={})
            return self._generate(company_id, client=client, prepared=prepared)

    def _parse(self, limit=None):
        value = limit if limit is not None else self.config['pipeline']['parser_limit_per_source']
        require(type(value) is int and value > 0, 'Лимит парсера должен быть положительным')
        command = [sys.executable, str(ROOT / 'src/parser/parser.py'), '--data-dir',
                   str(self.config['parser_directory']), '--limit', str(value),
                   '--retention-days',str(self.config.get('pipeline',{}).get('news_retention_days',10))]
        result = subprocess.run(command, cwd=ROOT, timeout=900, capture_output=True, text=True, encoding='utf-8', errors='replace')
        log = self.config['parser_directory'] / 'last-run.log'
        log.write_text(result.stdout + '\n' + result.stderr, encoding='utf-8')
        require(result.returncode == 0, 'Парсер завершился с ошибкой; смотрите ' + str(log))
        return dict(status='finished', log=str(log), database=str(self.config['parser_directory'] / 'news.db'))

    def parse(self, limit=None):
        with self.lock():
            return self._parse(limit)

    def _check_tokens(self, doc):
        model = self.model()
        if self._tokenizer is None:
            from transformers import AutoTokenizer
            self._tokenizer = AutoTokenizer.from_pretrained(str(self.config['model_path']), local_files_only=True)
        for fragment in doc['fragments']:
            for field, role in (('company_embedding_text', 'document'), ('analytics_embedding_text', 'query')):
                if not fragment.get(field):
                    continue
                count = len(self._tokenizer.encode(model['prompts'][role] + fragment[field], truncation=False))
                require(0 < count <= 2048, f"Фрагмент {fragment['id']}: {count} токенов; требуется более короткая разметка, обрезание запрещено")

    def _record(self, key, status, prepared_path=None, error=None):
        with connect(self.db) as c:
            with c:
                c.execute('''INSERT INTO news_preparation_jobs
                    (source_id,source_hash,prompt_version,status,attempts,prepared_path,last_error,updated_at)
                    VALUES(?,?,?,?,1,?,?,?) ON CONFLICT(source_id,source_hash,prompt_version) DO UPDATE SET
                    status=excluded.status,attempts=attempts+1,prepared_path=excluded.prepared_path,
                    last_error=excluded.last_error,updated_at=excluded.updated_at''',
                    (*key, status, str(prepared_path) if prepared_path else None, error, now()))
                if status in ('prepared','duplicate'):
                    c.execute('DELETE FROM news_preparation_retry_state WHERE source_id=? AND source_hash=? AND prompt_version=?',key)

    def _prepare(self, parser_db=None, limit=None, as_of=None, retry_failed=False, client=None, progress=None, on_prepared=None, should_continue=None):
        from src.llm import LLMError
        from src.news_prep.batch import load_events, to_request
        from src.news_prep.pipeline import PROMPT_VERSION, prepare_news
        from . import preparation_retry
        db = Path(parser_db or self.config['parser_directory'] / 'news.db')
        if not db.is_file():
            return dict(prepared=0, skipped=0, rejected=[], reason='База парсера пока не создана')
        require(client is not None or llm_ready(), 'Не настроен доступ к LLM для разметки новостей')
        limit = self.config['pipeline']['prepare_limit'] if limit is None else limit
        require(type(limit) is int and limit > 0, 'Лимит подготовки должен быть положительным')
        settings = retrieval(self.config, as_of)
        today = date.fromisoformat(settings.as_of)
        stats = dict(prepared=0, skipped=0, rejected=[], attempts=0)
        options=self.config['pipeline']
        max_attempts=options['preparation_max_attempts']
        base_delay=options.get('preparation_retry_delay_seconds',60)
        max_delay=options.get('preparation_retry_max_delay_seconds',600)
        require(type(max_attempts) is int and max_attempts>0,'preparation_max_attempts must be positive')
        require(type(base_delay) is int and base_delay>0 and type(max_delay) is int and max_delay>=base_delay,'Invalid preparation retry delays')
        if retry_failed:preparation_retry.reset(self.db,PROMPT_VERSION)
        paused=preparation_retry.pause(self.db)
        if paused:
            stats['pause']=paused
            write_json(self.config['prepared_news']/'last-run-summary.json',stats)
            return stats
        with connect(self.db, True) as c:
            known_docs = [json.loads(r[0]) for r in c.execute('SELECT source_json FROM news')]
        known_events = {event_key(d) for d in known_docs}
        known_financial = [f for d in known_docs if (f:=financial_identity(d))]
        for event in load_events(db):
            if should_continue and not should_continue():
                break
            row = event['row']
            key = None
            try:
                published = date.fromisoformat((row.get('published_utc') or '')[:10])
                if not 0 <= (today - published).days < settings.news_days:
                    stats['skipped'] += 1
                    continue
                request = to_request(row)
                key = (row['id'], sha(dumps(request)), PROMPT_VERSION)
                with connect(self.db, True) as c:
                    previous = c.execute('SELECT * FROM news_preparation_jobs WHERE source_id=? AND source_hash=? AND prompt_version=?', key).fetchone()
                readiness=preparation_retry.eligibility(self.db,key,previous,max_attempts)
                if readiness!='ready':
                    stats['skipped'] += 1
                    if readiness=='held':stats['held']=stats.get('held',0)+1
                    if readiness=='cooldown':stats['cooldown']=stats.get('cooldown',0)+1
                    continue
                raw_event = dict(document_id=row['id'],document_title=row['title'], document_date=published.isoformat(),
                                 source=request['metadata']['source'],source_blocks=request['source_blocks'], event={'id': event['event_id']})
                financial=financial_identity(raw_event)
                duplicate_financial=financial and financial.get('verified') and any(same_fact(financial,f) for f in known_financial)
                duplicate_other=not (financial and financial.get('verified')) and event_key(raw_event) in known_events
                if duplicate_financial or duplicate_other:
                    if duplicate_financial:remember_source(self.db,raw_event)
                    self._record(key, 'duplicate')
                    stats['skipped'] += 1
                    continue
                if stats['attempts'] >= limit:
                    break
                stats['attempts'] += 1
                if progress:
                    progress(dict(stage='preparation', phase=f"Размечаем статью {stats['attempts']} из {limit}: {row['title']}"))
                require(client is not None or llm_ready(), 'Не настроен доступ к LLM для разметки новостей')
                doc, trace = prepare_news(request, client=client)
                doc = VALIDATORS['news'](doc)
                doc['event'] = dict(id=event['event_id'], article_ids=event['article_ids'])
                self._check_tokens(doc)
                target = self.config['prepared_news'] / (sha(row['id']) + '.json')
                write_json(target, doc)
                imported = import_all(self.db, {'news': target}, update_existing=True, skip_invalid_news=True,min_news_date=self.news_min_date(as_of))
                require(not imported['rejected_count'], '; '.join(r['reason'] for r in imported['rejected_news']))
                write_json(self.config['prepared_news'] / 'traces' / (sha(row['id']) + '.json'), trace)
                self._record(key, 'prepared', target)
                known_events.add(event_key(doc))
                if (financial:=financial_identity(doc)):known_financial.append(financial)
                stats['prepared'] += 1
            except LLMError as error:
                failure=preparation_retry.record_failure(self.db,key,error,self._record,max_attempts,base_delay,max_delay)
                stats['rejected'].append(failure)
                if failure['shared_pause']:
                    stats['pause']=preparation_retry.pause(self.db)
                    break
            except (ValueError, KeyError, TypeError, IndexError, RuntimeError, OSError) as error:
                # A bad article never stops the other articles in this batch.
                if key is not None:
                    failure=preparation_retry.record_failure(self.db,key,error,self._record,max_attempts,base_delay,max_delay)
                    stats['rejected'].append(failure)
                    continue
                stats['rejected'].append(dict(source_id=row.get('id'), reason=str(error)))
            else:
                # Publication happens after this article's transaction, before
                # preparing the next article. Downstream failures are not bad news.
                if on_prepared:
                    on_prepared(doc)
        write_json(self.config['prepared_news'] / 'last-run-summary.json', stats)
        return stats

    def prepare(self, parser_db=None, limit=None, as_of=None, retry_failed=False, client=None):
        with self.lock():
            self._cleanup(as_of)
            return self._prepare(parser_db, limit, as_of, retry_failed, client)

    def collect_indicators(self,days=400):
        require(type(days) is int and 1<=days<=730,'Период показателей должен быть от 1 до 730 дней')
        from src.pipeline import indicators
        with self.lock(),indicators.using_database(self.db):
            return dict(indicator_rows_collected=indicators.collect(days=days),database=str(self.db))

    def tick(self, company_ids=None, as_of=None, parse=True, prepare=True, generate=True, client=None, progress=None,
             continuous=False, should_continue=None):
        with self.lock():
            stages, errors = {'cleanup':self._cleanup(as_of)}, []
            results, report_blocked = [], set()

            def publish_one():
                if should_continue and not should_continue():return
                from .preparation_retry import pause
                if pause(self.db):return
                selected = self.selected_company() if company_ids is None else None
                ids = company_ids if company_ids is not None else [selected] if selected else []
                for cid in ids:
                    if cid in report_blocked:
                        continue
                    try:
                        if progress:progress(dict(stage='report', phase='Проверяем новую новость для компании ' + cid, company_id=cid))
                        prepared = self._compare(cid, as_of, None, feed=continuous)
                        result = self._generate(cid, client=client, prepared=prepared) if generate else prepared
                        results.append({k: result[k] for k in ('status', 'saved', 'workflow','error','failure') if k in result} | {'company_id': cid})
                        if progress and result['status'] == 'completed':
                            progress(dict(stage='published', phase='Новая карточка опубликована в ленте ' + cid,
                                          published_run_id=result['saved']['run_id']))
                    except Exception as error:
                        report_blocked.add(cid)
                        errors.append(dict(stage='report', company_id=cid, reason=str(error)))

            # Already prepared/retrieved news must not wait for slow source downloads.
            if continuous and generate:
                publish_one()
            if parse:
                if progress:progress(dict(stage='parser', phase='Проверяем источники и собираем новые статьи…'))
                try:
                    stages['parser'] = self._parse()
                except (ValueError, OSError, subprocess.TimeoutExpired) as error:
                    errors.append(dict(stage='parser', reason=str(error)))
            if prepare and not report_blocked and (not should_continue or should_continue()):
                stages['preparation'] = self._prepare(as_of=as_of, client=client, progress=progress,
                    on_prepared=(lambda doc: publish_one()) if continuous and generate else None,
                    should_continue=lambda: not report_blocked and (not should_continue or should_continue()))
            if progress:progress(dict(stage='embedding', phase='Вычисляем векторы новых фрагментов…'))
            stages['embedding'] = self._embed()
            if not should_continue or should_continue():
                publish_one()
            pending = any(r.get('workflow', {}).get('queued_after_selection', 0) > 0 or r['status']=='skipped' for r in results[-1:])
            prep = stages.get('preparation', {})
            retry = bool(errors or any(r.get('retry') for r in prep.get('rejected', [])))
            return dict(stages=stages, results=results, errors=errors,
                        continue_soon=not retry and (pending or prep.get('attempts', 0) >= self.config['pipeline']['prepare_limit']))
