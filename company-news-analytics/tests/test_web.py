import copy,json,sqlite3,tempfile,threading,unittest
from pathlib import Path
from unittest.mock import patch
from urllib.request import Request,urlopen
from urllib.error import HTTPError
from http.server import ThreadingHTTPServer
import numpy as np
from news_analytics.common import write_json,read_json,sha
from news_analytics.database import connect,check
from news_analytics.web import Application,Handler,install_uploads
from news_analytics.retrieval import Settings
from tests.helpers import company,news,analytics
from tests.test_pipeline import MODEL

class WebTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name)
        self.folders={k:self.root/k for k in ('companies','news','analytics')}
        for p in self.folders.values():p.mkdir()
        config=self.root/'config.json'
        write_json(config,dict(database=str(self.root/'data.db'),model_path=str(self.root/'model'),
          inputs={k:str(v) for k,v in self.folders.items()},reports=str(self.root/'reports'),
          retrieval=dict(analytics_per_news=10),embedding=dict(batch_size=4,threads=4)))
        self.app=Application(config);self.db=self.app.db
    def tearDown(self):self.app.pool.shutdown(wait=True);self.temp.cleanup()
    def upload(self,values):
        return install_uploads(self.db,self.folders,[{'name':str(i)+'.json','content':json.dumps(v,ensure_ascii=False)} for i,v in enumerate(values)])
    def seed(self):
        with connect(self.db) as c:
            with c:
                for row in c.execute('SELECT * FROM embedding_inputs').fetchall():
                    values=np.zeros(768,dtype='<f4');values[0]=1
                    c.execute('UPDATE embedding_inputs SET embedding=?,model_id=?,model_revision=?,dimensions=768,normalized=1,embedded_input_hash=text_hash,prompt=?,token_count=20,embedded_at=? WHERE id=?',
                      (values.tobytes(),MODEL['model_id'],MODEL['model_revision'],MODEL['prompts']['query' if row['encoding_method']=='encode_query' else 'document'],'2026-10-22',row['id']))
    def test_mixed_upload_persists_canonical_files_and_reimport_is_idempotent(self):
        first=self.upload([company(),news(),analytics()]);self.assertEqual(first['changed_documents'],3)
        second=self.upload([company(),news(),analytics()]);self.assertEqual(second['changed_documents'],0)
        for kind in self.folders:
            files=list(self.folders[kind].glob('*.json'));self.assertEqual(len(files),1)
            with connect(self.db,True) as c:self.assertTrue(Path(c.execute('SELECT input_file FROM '+kind).fetchone()[0]).samefile(files[0]))
    def test_fresh_start_imports_included_json_without_loading_model(self):
        write_json(self.folders['companies']/'company.json',company())
        self.app.pool.shutdown(wait=True)
        self.app=Application(self.root/'config.json')
        self.assertEqual(self.app.state()['counts']['companies'],1)
        self.assertEqual(self.app.state()['counts']['pending_vectors'],1)
    def test_existing_nested_files_with_same_name_do_not_overwrite_staged_documents(self):
        first=company();second=company();second['subject_id']='C2'
        write_json(self.folders['companies']/'a/profile.json',first)
        write_json(self.folders['companies']/'b/profile.json',second)
        result=self.upload([first,second])
        self.assertEqual(result['changed_documents'],2)
        self.assertEqual({c['id'] for c in self.app.state()['companies']},{'C1','C2'})
    def test_invalid_file_rejects_entire_batch_before_writes(self):
        invalid=news();invalid['fragments'][0]['company_embedding_text']='Missing original thesis'
        with self.assertRaises(ValueError):self.upload([company(),invalid])
        self.assertFalse(list(self.root.rglob('uploaded-*.json')))
        self.assertEqual(self.app.state()['counts']['companies'],0)
    def test_conflicting_archived_source_rolls_back_files_and_other_kinds(self):
        self.upload([company(),analytics()]);original={p:p.read_bytes() for p in self.root.rglob('uploaded-*.json')}
        changed=analytics();c=changed['chunks'][0];c['text']='Changed source';c['embedding_text']=c['text'];c['content_sha256']=sha(c['text'])
        changed['mechanisms'][0].update(source_text=c['text'],evidence_refs=[dict(fragment_id=c['id'],quote=c['text'],pages=[1])])
        profile=company();profile['profile_text']='Changed profile'
        with self.assertRaisesRegex(ValueError,'archived analytical source'):self.upload([profile,changed])
        for path,body in original.items():self.assertEqual(path.read_bytes(),body)
        with connect(self.db,True) as c:
            self.assertEqual(c.execute('SELECT profile_text FROM companies').fetchone()[0],company()['profile_text']);check(c)
    def test_browser_filename_is_never_used_as_path_and_duplicate_keys_rejected(self):
        install_uploads(self.db,self.folders,[dict(name='../../outside.json',content=json.dumps(company()))])
        self.assertFalse((self.root/'outside.json').exists())
        with self.assertRaisesRegex(ValueError,'Duplicate JSON key'):install_uploads(self.db,self.folders,[dict(name='bad.json',content='{"x":1,"x":2}')])
    def test_background_search_uses_ten_candidates_and_saves_report(self):
        self.upload([company(),news(),analytics()]);self.seed()
        with patch('news_analytics.web.identity',return_value=MODEL),patch('news_analytics.web.embed',return_value={'computed':0}):
            self.app.jobs['test']={'id':'test','status':'running'}
            self.app._work('test','search',{'company_id':'C1','as_of':'2026-10-22','top_news':3})
        job=self.app.get_job('test');self.assertEqual(job['status'],'complete');self.assertEqual(job['report']['settings']['analytics_per_news'],10)
        self.assertEqual(len(job['report']['news']),1)
        self.assertEqual(job['report']['settings']['min_analytics_score'],.375)
        self.assertTrue((Path(job['saved']['output'])/'report.html').exists())
        packet=read_json(Path(job['saved']['output'])/'llm-input-C1.json')
        self.assertEqual(packet['schema_version'],'report-generation-input-1.1')
        self.assertLessEqual(len(packet['analytical_mechanisms']),5)
        self.assertEqual(Settings('2026-10-22').analytics_per_news,10)
    def test_search_resume_complete_and_history_survive_application_restart(self):
        self.upload([company(),news(),analytics()]);self.seed()
        def search(jid):
            self.app.jobs[jid]={'id':jid,'status':'running'}
            self.app._work(jid,'search',{'company_id':'C1','as_of':'2026-10-22','top_news':3})
            return self.app.get_job(jid)
        with patch('news_analytics.web.identity',return_value=MODEL),patch('news_analytics.web.embed',return_value={'computed':0}):
            first=search('first');second=search('second')
            self.assertEqual(second['workflow_status'],'resumed');self.assertEqual(first['saved']['run_id'],second['saved']['run_id'])
            packet=read_json(Path(first['saved']['output'])/'llm-input-C1.json')
            result=dict(schema_version='company-analysis-report-1.1',request_id=packet['request_id'],company_id='C1',as_of=packet['as_of'],
                summary='Технический тест ответа.',items=[dict(news_id=n['id'],news_fragment_ids=[],assessments=[]) for n in packet['news']],company_context=[],overall_limitations=[])
            self.app.jobs['ack']={'id':'ack','status':'running'}
            self.app._work('ack','complete',dict(run_id=first['saved']['run_id'],company_id='C1',result=result))
            self.assertEqual(self.app.get_job('ack')['status'],'complete')
            third=search('third');self.assertEqual(third['workflow_status'],'idle');self.assertIsNone(third['saved'])
        self.app.pool.shutdown(wait=True);self.app=Application(self.root/'config.json')
        server=ThreadingHTTPServer(('127.0.0.1',0),Handler);server.app=self.app
        thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        try:
            base='http://127.0.0.1:'+str(server.server_port)
            with urlopen(base+'/api/history?company=C1') as r:rows=json.load(r)
            self.assertEqual(rows[0]['status'],'completed');self.assertEqual(rows[0]['result']['request_id'],packet['request_id'])
            with urlopen(base+'/api/batch?run_id='+first['saved']['run_id']+'&company_id=C1&format=llm') as r:restored=json.load(r)
            self.assertEqual(restored['request_id'],packet['request_id'])
        finally:server.shutdown();server.server_close();thread.join()

    def test_model_failure_after_import_reports_saved_materials_for_resume(self):
        self.app.jobs['test']={'id':'test','status':'running'}
        with patch('news_analytics.web.identity',side_effect=ValueError('Model missing')),patch('news_analytics.web.traceback.print_exc'):
            self.app._work('test','import',{'files':[dict(name='company.json',content=json.dumps(company()))]})
        job=self.app.get_job('test');self.assertEqual(job['status'],'failed');self.assertIn('import_result',job)
        self.assertEqual(self.app.state()['counts']['companies'],1)
    def test_only_one_background_operation_at_a_time(self):
        with patch.object(self.app.pool,'submit'):
            self.app.start('search',{'company_id':'C1'})
            with self.assertRaises(BlockingIOError):self.app.start('import',{'files':[]})
    def test_http_state_assets_and_cross_origin_write_rejection(self):
        server=ThreadingHTTPServer(('127.0.0.1',0),Handler);server.app=self.app
        thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start();base='http://127.0.0.1:'+str(server.server_port)
        try:
            with urlopen(base+'/api/state') as r:self.assertEqual(json.load(r)['settings']['analytics_per_news'],10)
            with urlopen(base+'/app.js') as r:self.assertIn('text/javascript',r.headers['Content-Type'])
            req=Request(base+'/api/import',data=b'{"files":[]}',headers={'Content-Type':'application/json'})
            with self.assertRaises(HTTPError) as caught:urlopen(req)
            self.assertEqual(caught.exception.code,403)
        finally:server.shutdown();server.server_close();thread.join()

if __name__=='__main__':unittest.main()
