"""Boundary tests with deterministic LLM responses; never call the paid API."""
from copy import deepcopy
from contextlib import closing
from datetime import datetime
from pathlib import Path
import importlib.util
import json
import os
import sqlite3
import sys
import tempfile
import threading
import time
import urllib.request
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'company-news-analytics'))
sys.path.insert(0, str(ROOT))

import numpy as np
from news_analytics.common import write_json
from news_analytics.database import connect
from news_analytics.embedding import reuse_legacy
from news_analytics.importing import import_all
from news_analytics.workflow import history, validate_result
from src.integration.adapter import result_for_batch
from src.integration.service import Service
from src.integration.settings import load
from src.llm.client import LLMError, _content
from src.report.package import build
from src.report.pipeline import generate

spec = importlib.util.spec_from_file_location('retrieval_fixture_helpers', ROOT / 'company-news-analytics/tests/helpers.py')
helpers = importlib.util.module_from_spec(spec)
spec.loader.exec_module(helpers)

MODEL = dict(model_id='google/embeddinggemma-300m', model_revision='test-revision', dimensions=768,
             prompts={'query': 'query: ', 'document': 'document: '}, versions={})


def packet():
    source = 'При росте затрат торговая компания может изменить цены, если покупатель готов принять повышение.'
    text = 'Издержки торговых компаний выросли из-за удорожания перевозок.'
    return dict(schema_version='report-generation-input-1.1', request_id='R1::C1', as_of='2026-10-23',
        generation_instructions=[], company=dict(id='C1', subject_type='company', profile='Торговая компания.',
            facts={'industry': {'name': 'Торговля'}}, limitations=[]),
        news=[dict(id='N1', title='Рост затрат', publication_date='2026-10-22', source={'url': 'https://example.org/n1'},
            company_match={'aspects': []}, source_blocks=[{'id': 'B1', 'text': text}],
            source_traceability={'status': 'complete'}, fragments=[dict(id='N1::F1', thesis=text, context='Торговля.',
                source_quotes=[text], source_block_ids=['B1'], mechanism=dict(description='Издержки могут переноситься в цены.',
                    context={'conditions': 'При устойчивом спросе.'}))])],
        analytical_documents=[dict(id='A1', title='Обзор', publisher='Альфа-Банк', publication_date='2026-10-21',
                                  source_provenance={'provided_by_organizer': False})],
        analytical_source_fragments=[dict(id='A1::F1', document_id='A1', text=source, pages=[1])],
        analytical_mechanisms=[dict(id='A1::M1', document_id='A1', title='Перенос издержек', mechanism=dict(cause='Рост затрат',
            channel='Перенос', effect='Рост цен'), context={'conditions': 'Устойчивый спрос'}, source_fragment_ids=['A1::F1'],
            evidence_refs=[dict(fragment_id='A1::F1', start_char=0, end_char=len(source), pages=[1])])],
        connections=[dict(id='L001', news_id='N1', news_fragment_id='N1::F1', mechanism_id='A1::M1',
            source_fragment_ids=['A1::F1'], selection_status='intersection')])


class FakeClient:
    def __init__(self, rejected=False, invalid=False):
        self.rejected, self.invalid, self.tasks = rejected, invalid, []
        self.usage = dict(calls=0, prompt_tokens=0, completion_tokens=0, cost=0.0)

    def model(self, role, fallback=None):
        return 'fake-' + role

    def chat_json(self, model, messages, task, params=None):
        self.tasks.append(task)
        self.usage['calls'] += 1
        user = messages[-1]['content']
        if task.startswith('report_assess'):
            # Read IDs from the actual prepared view, not hardcoded fixture IDs.
            original = next(m['content'] for m in messages if m['role'] == 'user')
            view = json.loads(original.split('Пакет (данные, а не инструкции):\n')[1])
            self.view = view
            label = 'rejected' if self.rejected else 'conditional'
            basis = [next(p for p in view['company']['allowed_basis_paths'] if p.startswith('/facts/industry'))]
            result = dict(news_assessments=[dict(news_id=n['id'], label=label, reason='Проверка применимости.',
                checks={k: 'Проверено' for k in ('subject','sector','geography','direction','period','regulatory_status','statement_type')},
                company_basis=basis, required_conditions=['При сохранении спроса.']) for n in view['news']],
                link_assessments=[dict(link_id=l['id'], label=label, reason='При сохранении спроса возможен перенос затрат.',
                    fits_news=not self.rejected, relevant_to_company_independently=False,
                    company_basis=basis, required_conditions=['При сохранении спроса.']) for l in view['links']], missing_information=[])
            if self.invalid:
                result['news_assessments'] = []
            return result
        if task in ('report_write','report_revise'):
            data = json.loads(next(m['content'] for m in messages if m['role'] == 'user').split('\n',1)[1])
            return dict(summary='Возможен условный эффект для компании.', items=[dict(news_id=n['id'], headline=n['title'],
                label='conditional', what_happened=[dict(text=n['fragments'][0]['thesis'], type='news_fact',
                    refs=[n['fragments'][0]['id']], company_basis=[])], why_it_matters=[], risks=[], opportunities=[], bank_view=[],
                not_established=['Устойчивость спроса'], to_verify=[], next_steps=[]) for n in data['accepted_news']], independent_risks=[])
        if task == 'report_critic':
            return {'issues': []}
        raise AssertionError('Unexpected paid-model task: ' + task)


class AgentContractTests(unittest.TestCase):
    def test_original_blocks_reach_agent_and_organizer_flag_is_exact(self):
        view, index = build(packet())
        self.assertTrue(view['news'][0]['source_blocks'])
        self.assertTrue(view['news'][0]['fragments'][0]['source_quotes'])
        self.assertEqual(index.documents['A1']['source_kind'], 'third_party')
        p = packet(); p['analytical_documents'][0]['source_provenance']['provided_by_organizer'] = True
        self.assertEqual(build(p)[1].documents['A1']['source_kind'], 'bank')

    def test_real_agent_stages_create_valid_acknowledgement(self):
        p, client = packet(), FakeClient()
        document, trace = generate(p, client)
        result = result_for_batch(p, document)
        self.assertEqual(validate_result(result, p), result)
        self.assertEqual(client.tasks, ['report_assess','report_write','report_critic'])
        self.assertEqual(result['items'][0]['assessments'][0]['evidence'][0]['quote'], p['analytical_source_fragments'][0]['text'])

    def test_numbers_are_checked_against_only_the_cited_original_blocks(self):
        from src.report.verify import check_report
        p = packet()
        p['news'][0]['source_blocks'][0]['text'] += ' Выпуск составил 2,4 млн т.'
        p['news'][0]['source_blocks'].append({'id':'B2','text':'Экспорт составил 9,8 млн т.'})
        document, _ = generate(p, FakeClient())
        _, index = build(p)
        assessment = {'news_assessments':[{'news_id':'N1','label':'conditional'}], 'link_assessments':[]}
        for number, expected in [('2,4',False),('9,8',True),('123,45',True)]:
            report = deepcopy(document)
            report['items'][0]['what_happened'][0]['text'] = 'Выпуск составил '+number+' млн т.'
            issues = check_report(report,index,assessment,p['company']['facts'],p['company']['profile'])
            self.assertEqual(any('числа не найдены' in issue for issue in issues),expected,issues)

    def test_rejected_news_are_acknowledged_without_writer_call(self):
        p, client = packet(), FakeClient(rejected=True)
        document, _ = generate(p, client)
        result = result_for_batch(p, document)
        self.assertEqual(result['items'][0]['applicability'], 'rejected')
        self.assertEqual(result['narrative']['items'], [])
        self.assertEqual(client.tasks, ['report_assess'])

    def test_incomplete_assessment_never_reaches_writer(self):
        client = FakeClient(invalid=True)
        with self.assertRaises(LLMError):
            generate(packet(), client)
        self.assertNotIn('report_write', client.tasks)

    def test_mismatched_identity_is_rejected(self):
        p = packet(); document, _ = generate(p, FakeClient())
        document['request_id'] = 'another-run'
        with self.assertRaises(ValueError):
            result_for_batch(p, document)

    def test_empty_thinking_response_has_clear_error(self):
        with self.assertRaisesRegex(LLMError, 'length'):
            _content({'choices': [{'finish_reason':'length','message':{'content':None}}]})

    def test_csv_profiles_export_company_and_group_without_stub_store(self):
        import pandas as pd
        from src.company_profile.data import FLAG_COLUMNS
        from src.integration.profiles import company_json
        with tempfile.TemporaryDirectory() as tmp:
            folder=Path(tmp)
            base=dict(MAINOKVED_CODE='46.38',MAINOKVED_NAME='Торговля оптовая пищевыми продуктами',
                GROUP_ID='group_test',FTS_COUNTRY_NAME='КИТАЙ',REGION='Тестовый регион',CITY='Тестовый город',
                OKFS_NAME='Частная собственность',COMPANY_AGE_YEARS_GRADATION='5-10',
                AMOUNT_WORKERS_FNS_GRADATION='10-50',AMOUNT_WORKERS_SPARK_GRADATION='10-50',
                ECO_MARK_LABEL='Развитие',CL_TYPE='Клиент',ORG_TYPE='ЮЛ',**{k:0 for k in FLAG_COLUMNS})
            rows=[dict(base,SYNTHETIC_U='C1',REVENUE_COMPANY=1000000),dict(base,SYNTHETIC_U='C2',REVENUE_COMPANY=2000000)]
            pd.DataFrame(rows).to_csv(folder/'df_Uid_data_hack.csv',sep=';',index=False,encoding='utf-8-sig')
            pd.DataFrame([dict(PAYER='C1',BENEFICIARY='C2',PAY_AMT=100,PAY_CNT=1)]).to_csv(folder/'df_Uid_payment_hack.csv',sep=';',index=False)
            pd.DataFrame([dict(SYNTHETIC_U='C1',SYNTHETIC_F='F1',CITY_NAME='Тест',RC_NAME='Тест')]).to_csv(folder/'df_Fid_Uid_link_hack.csv',sep=';',index=False)
            with patch('src.embeddings.get_store',side_effect=AssertionError('StubStore must not be used')):
                company=company_json('C1',folder)
                group=company_json('group_test',folder)
            self.assertEqual(company['subject_type'],'company')
            self.assertTrue(company['queries'])
            self.assertEqual({m['subject_id'] for m in group['facts']['members']},{'C1','C2'})


class IntegratedWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.cfg = load()
        # These fixtures exercise the supported v1 contract. V2 has separate tests.
        self.cfg['pipeline']['report_mode']='standard'
        self.cfg.update(database=self.root/'app.db', reports=self.root/'reports', parser_directory=self.root/'parser',
                        prepared_news=self.root/'prepared')
        self.cfg['inputs'] = {}
        self.cfg['upload_folders'] = {}
        self.service = Service(self.cfg)
        files = {}
        for kind, doc in [('companies',helpers.company()),('news',helpers.news()),('analytics',helpers.analytics())]:
            target=self.root/(kind+'.json');write_json(target,doc);files[kind]=target
        import_all(self.service.db, files)
        self.fill_vectors(self.service.db)
        self.model_patch=patch.object(self.service,'model',return_value=MODEL);self.model_patch.start()
        self.embed_patch=patch.object(self.service,'_embed',return_value={'computed':0});self.embed_patch.start()

    @staticmethod
    def fill_vectors(db):
        vector=np.zeros(768,dtype='<f4');vector[0]=1
        with connect(db) as c:
            with c:
                c.execute('''UPDATE embedding_inputs SET embedding=?,model_id=?,model_revision=?,dimensions=768,
                    normalized=1,embedded_input_hash=text_hash,token_count=20,embedded_at='2026-10-23',
                    prompt=CASE encoding_method WHEN 'encode_query' THEN 'query: ' ELSE 'document: ' END''',
                    (vector.tobytes(),MODEL['model_id'],MODEL['model_revision']))

    def tearDown(self):
        self.embed_patch.stop();self.model_patch.stop();self.temp.cleanup()

    def test_generate_then_repeat_does_not_regenerate_completed_news(self):
        client=FakeClient()
        first=self.service.generate('C1','2026-10-23',client=client)
        calls=len(client.tasks)
        self.assertEqual(first['status'],'completed')
        second=self.service.generate('C1','2026-10-23',client=client)
        self.assertEqual(second['status'],'idle')
        self.assertEqual(len(client.tasks),calls)
        self.assertEqual(history(self.service.db,'C1')[0]['status'],'completed')

    def test_tick_never_defaults_to_all_companies(self):
        client=FakeClient()
        idle=self.service.tick(as_of='2026-10-23',parse=False,prepare=False,client=client)
        self.assertEqual(idle['results'],[])
        self.assertEqual(history(self.service.db),[])
        self.assertEqual(client.tasks,[])
        second=helpers.company();second['subject_id']='C2'
        path=self.root/'second-company.json';write_json(path,second)
        import_all(self.service.db,{'companies':path});self.fill_vectors(self.service.db)
        self.service.select_company('C1')
        self.assertEqual(Service(self.cfg).selected_company(),'C1')
        ingest=self.service.tick(company_ids=[],as_of='2026-10-23',parse=False,prepare=False,generate=False,client=client)
        self.assertEqual(ingest['results'],[])
        self.assertEqual(client.tasks,[])
        result=self.service.tick(as_of='2026-10-23',parse=False,prepare=False,client=client)
        self.assertEqual([r['company_id'] for r in result['results']],['C1'])
        self.assertEqual(result['errors'],[])
        self.assertEqual(history(self.service.db,'C2'),[])
        with self.assertRaises(ValueError):self.service.select_company('unknown')
        self.assertEqual(self.service.selected_company(),'C1')

    def test_web_update_only_ingests_but_automatic_tick_uses_selection(self):
        from src.integration.web import Application
        config_file=self.root/'config.json'
        write_json(config_file,{k:(str(v) if isinstance(v,Path) else v) for k,v in self.cfg.items()})
        app=Application(config_file,self.cfg)
        try:
            with patch.object(app.service,'tick',return_value={'results':[]}) as tick,patch.object(app,'_update'):
                app._work('manual','tick',{})
                self.assertEqual(tick.call_args.kwargs['company_ids'],[])
                self.assertFalse(tick.call_args.kwargs['generate'])
                app._work('automatic','tick',{'automatic':True})
                self.assertIsNone(tick.call_args.kwargs['company_ids'])
                self.assertTrue(tick.call_args.kwargs['generate'])
        finally:app.pool.shutdown(wait=True)

    def test_api_failure_leaves_same_batch_pending_for_retry(self):
        with patch('src.report.pipeline.generate',side_effect=LLMError('API temporarily unavailable')):
            with self.assertRaises(LLMError):self.service.generate('C1','2026-10-23',client=FakeClient())
        pending=history(self.service.db,'C1')[0]
        self.assertEqual(pending['status'],'prepared')
        final=self.service.generate('C1','2026-10-23',client=FakeClient())
        self.assertEqual(final['completion']['run_id'],pending['run_id'])

    def test_modern_cache_transfer_copies_vectors_not_outbox_history(self):
        target=Service({**self.cfg,'database':self.root/'target.db'})
        import_all(target.db,{'companies':self.root/'companies.json'})
        result=reuse_legacy(target.db,self.service.db,MODEL)
        self.assertEqual(result['copied'],1)
        self.assertEqual(history(target.db),[])

    def test_bad_preparation_does_not_stop_other_articles(self):
        db=self.root/'parser.db'
        with closing(sqlite3.connect(db)) as c, c:
            c.execute('CREATE TABLE news(id,event_id,outlet,url,title,subtitle,published_utc,body_clean,is_full)')
            for nid in ('bad','good'):
                c.execute('INSERT INTO news VALUES(?,?,?,?,?,?,?,?,1)',(nid,nid,'kommersant','https://example.org/'+nid,
                    'Рост затрат','', '2026-10-22T10:00:00Z','Издержки торговых компаний выросли из-за удорожания перевозок.'))
        def prepared(request,client=None):
            nid=request['metadata']['document_id']
            if nid=='bad':raise ValueError('Incomplete article')
            doc=helpers.news();doc['document_id']=nid;doc['source']['url']=request['metadata']['source']['url']
            return doc,{'test':True}
        with patch('src.news_prep.pipeline.prepare_news',side_effect=prepared),patch.object(self.service,'_check_tokens'):
            result=self.service.prepare(db,limit=5,as_of='2026-10-23',client=FakeClient())
        self.assertEqual(result['prepared'],1)
        self.assertEqual(len(result['rejected']),1)

    def test_http_generate_history_and_secret_file_is_not_served(self):
        from http.server import ThreadingHTTPServer
        from src.integration.web import Application, Handler
        config_file=self.root/'config.json'
        cfg={k:({a:str(b) for a,b in v.items()} if k in ('inputs','upload_folders') else str(v) if isinstance(v,Path) else v)
             for k,v in self.cfg.items()}
        write_json(config_file,cfg)
        app=Application(config_file,self.cfg)
        server=ThreadingHTTPServer(('127.0.0.1',0),Handler);server.app=app
        thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        address='http://127.0.0.1:'+str(server.server_port)
        client=FakeClient()
        def agent(p,client=None,critic=True):
            return generate(p,self.fake_client,critic)
        self.fake_client=client
        try:
            with urllib.request.urlopen(address+'/api/state') as response:
                state=json.load(response)
            self.assertEqual(state['counts']['companies'],1)
            self.assertNotIn('LLM_API_KEY',json.dumps(state))
            for asset, mime in [('/report-view.js','text/javascript'),('/report-view.css','text/css'),
                                ('/favicon.ico','image/x-icon'),
                                ('/assets/empty-news-v1.webp','image/webp'),
                                ('/currency-widget.js','text/javascript'),
                                ('/story-cards.js','text/javascript'),
                                ('/site-adapter.js','text/javascript'),('/site-panels.js','text/javascript'),
                                ('/integration.css','text/css'),('/manage','text/html')]:
                with urllib.request.urlopen(address+asset) as response:
                    self.assertTrue(response.headers['Content-Type'].startswith(mime))
                    self.assertGreater(len(response.read()),100)
            with patch.object(app.currency_rates,'get',return_value={'status':'unavailable','rates':[]}):
                with urllib.request.urlopen(address+'/api/currency-rates') as response:
                    self.assertEqual(json.load(response),{'status':'unavailable','rates':[]})
            # A video player must receive the requested bytes, not the whole file.
            for filename in ('alfa-animation.mp4','consumption-animation.mp4'):
                data=(ROOT/'src/integration/web_static/assets'/filename).read_bytes()
                url=address+'/assets/'+filename
                with urllib.request.urlopen(urllib.request.Request(url,headers={'Range':'bytes=0-63'})) as response:
                    self.assertEqual(response.status,206)
                    self.assertEqual(response.headers['Content-Type'],'video/mp4')
                    self.assertEqual(response.headers['Content-Range'],f'bytes 0-63/{len(data)}')
                    self.assertEqual(response.read(),data[:64])
                with urllib.request.urlopen(urllib.request.Request(url,headers={'Range':'bytes=-16'})) as response:
                    self.assertEqual(response.read(),data[-16:])
                with urllib.request.urlopen(urllib.request.Request(url,method='HEAD')) as response:
                    self.assertEqual(int(response.headers['Content-Length']),len(data))
                    self.assertEqual(response.read(),b'')
                for invalid in ('bytes=3-1','bytes=0-3,6-9',f'bytes={len(data)}-'):
                    with self.assertRaises(urllib.error.HTTPError) as error:
                        urllib.request.urlopen(urllib.request.Request(url,headers={'Range':invalid}))
                    self.assertEqual(error.exception.code,416)
            request=urllib.request.Request(address+'/api/select-company',data=b'{"company_id":"C1"}',
                headers={'Content-Type':'application/json','X-Requested-With':'news-analytics-ui'})
            with urllib.request.urlopen(request) as response:
                self.assertEqual(json.load(response)['selected_company_id'],'C1')
            with urllib.request.urlopen(address+'/api/state') as response:
                self.assertEqual(json.load(response)['integration']['selected_company_id'],'C1')
            with self.assertRaises(urllib.error.HTTPError) as error:
                urllib.request.urlopen(address+'/.env')
            self.assertEqual(error.exception.code,404)
            with patch.object(app.service,'model',return_value=MODEL),patch.object(app.service,'_embed',return_value={'computed':0}), \
                 patch('src.integration.service.llm_ready',return_value=True),patch('src.report.pipeline.generate',side_effect=agent):
                request=urllib.request.Request(address+'/api/generate',data=json.dumps({'company_id':'C1','as_of':'2026-10-23'}).encode(),
                    headers={'Content-Type':'application/json','X-Requested-With':'news-analytics-ui'})
                with urllib.request.urlopen(request) as response:job_id=json.load(response)['job_id']
                for _ in range(100):
                    with urllib.request.urlopen(address+'/api/jobs/'+job_id) as response:job=json.load(response)
                    if job['status']!='running':break
                    time.sleep(.05)
                self.assertEqual(job['status'],'complete',job)
                self.assertEqual(job['workflow_status'],'completed')
            with urllib.request.urlopen(address+'/api/history?company=C1') as response:rows=json.load(response)
            self.assertEqual(rows[0]['status'],'completed')
            self.assertTrue(rows[0]['result']['narrative']['summary'])
            with patch('src.integration.site.datetime',wraps=datetime) as clock:
                clock.now.return_value=datetime(2026,10,23)
                with urllib.request.urlopen(address+'/api/site-feed?company=C1') as response:feed=json.load(response)
            self.assertEqual(feed['company']['id'],'C1')
            self.assertEqual(len(feed['news']),1)
        finally:
            server.shutdown();server.server_close();thread.join(timeout=3);app.pool.shutdown(wait=True)


if __name__ == '__main__':
    unittest.main()
