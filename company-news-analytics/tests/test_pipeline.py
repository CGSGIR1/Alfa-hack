import copy,json,sqlite3,tempfile,unittest
from pathlib import Path
import numpy as np
from news_analytics.common import write_json,read_json,sha
from news_analytics.database import initialize,connect,check
from news_analytics.importing import import_all
from news_analytics.validation import news as prepare_news
from news_analytics.embedding import vector
from news_analytics.retrieval import Settings,snapshot,retrieve,agree
from news_analytics.reporting import save,html_reports
from tests.helpers import company,news,analytics

MODEL={'model_id':'google/embeddinggemma-300m','model_revision':'unit-fixture','dimensions':768,
       'prompts':{'query':'query: ','document':'document: '}}

class PipelineTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name);self.db=initialize(self.root/'test.db')
        self.paths={kind:self.root/(kind+'.json') for kind in ('companies','news','analytics')}
        for kind,value in [('companies',company()),('news',news()),('analytics',analytics())]:write_json(self.paths[kind],value)
        import_all(self.db,self.paths)
    def tearDown(self):self.temp.cleanup()
    def seed(self):
        with connect(self.db) as c:
            with c:
                for row in c.execute('SELECT * FROM embedding_inputs').fetchall():
                    # News context is deliberately ORTHOGONAL to analytical source/mechanism.
                    values=np.zeros(768,dtype='<f4');values[0 if row['role'] in {'news_context','company_query'} else 1]=1
                    c.execute('UPDATE embedding_inputs SET embedding=?,model_id=?,model_revision=?,dimensions=768,normalized=1,embedded_input_hash=text_hash,prompt=?,token_count=20,embedded_at=? WHERE id=?',
                      (values.tobytes(),MODEL['model_id'],MODEL['model_revision'],MODEL['prompts']['query' if row['encoding_method']=='encode_query' else 'document'],'2026-10-22',row['id']))
    def test_selected_scheme_uses_same_mechanism_query_twice(self):
        self.seed();data=snapshot(self.db,MODEL);result=retrieve(data,'C1',Settings('2026-10-22'))
        n=result['news'][0];self.assertEqual(len(n['analytics']),1)
        self.assertAlmostEqual(n['analytics'][0]['mechanism_similarity'],1)
        self.assertAlmostEqual(n['source_candidates'][0]['similarity'],1)
        self.assertEqual(n['selected_fragments'][0]['analytics_query_version'],'news-two-views-1.0')
    def test_analytics_threshold_requires_both_views_and_keeps_boundary(self):
        self.seed();data=snapshot(self.db,MODEL)
        ids={r['role']:r['id'] for r in data['inputs'].values()}
        def set_cosine(role,score):
            v=np.zeros(768,dtype=np.float32);v[1]=score;v[2]=np.sqrt(1-score*score)
            data['vectors'][ids[role]]=v
        def matches(**kwargs):
            return retrieve(data,'C1',Settings('2026-10-22',financial_topic_coverage=0,**kwargs))['news'][0]['analytics']
        set_cosine('analytics_mechanism',.5);set_cosine('analytics_source',.36)
        self.assertEqual(len(matches(min_analytics_score=.35)),1)
        self.assertEqual(matches(),[])
        set_cosine('analytics_source',.375)
        self.assertEqual(len(matches()),1)
        set_cosine('analytics_mechanism',.36);set_cosine('analytics_source',.5)
        self.assertEqual(matches(),[])
    def test_only_five_supported_roles_no_third_news_vector(self):
        with connect(self.db,True) as c:
            self.assertEqual({r[0] for r in c.execute('SELECT role FROM embedding_inputs')},
              {'company_query','news_context','news_mechanism','analytics_source','analytics_mechanism'})
    def test_idempotent_import(self):
        self.seed();result=import_all(self.db,self.paths,True);self.assertEqual(result['changed_documents'],0);self.assertIsNone(result['backup'])
        self.assertEqual(len(snapshot(self.db,MODEL)['inputs']),5)
    def test_group_retains_type_and_known_members(self):
        value=company();value['subject_id']='group_1';value['subject_type']='group';value['facts']['members']=[{'subject_id':'C1'}]
        write_json(self.paths['companies'],value);import_all(self.db,{'companies':self.paths['companies']});self.seed()
        result=retrieve(snapshot(self.db,MODEL),'group_1',Settings('2026-10-22'))
        self.assertEqual(result['subject_type'],'group');self.assertEqual(result['company_facts']['members'][0]['subject_id'],'C1')
    def test_explicit_updates_and_cache_invalidation(self):
        self.seed();value=company();value['queries'][0]['weight']=.5;write_json(self.paths['companies'],value)
        skipped=import_all(self.db,self.paths);self.assertEqual(skipped['kinds']['companies']['changed_skipped'],1)
        import_all(self.db,self.paths,True);self.assertEqual(len(snapshot(self.db,MODEL)['inputs']),5)
        value['queries'][0]['text']='Оптовая торговля';write_json(self.paths['companies'],value);import_all(self.db,self.paths,True)
        with self.assertRaisesRegex(ValueError,'Missing or incompatible'):snapshot(self.db,MODEL)
    def test_no_mechanism_needs_only_context_vector(self):
        value=news();value['fragments'][0]['mechanism']['status']='not_identified';value['fragments'][0]['analytics_embedding_text']=None
        write_json(self.paths['news'],value);import_all(self.db,self.paths,True)
        with connect(self.db,True) as c:self.assertIsNone(c.execute('SELECT mechanism_input_id FROM article_chunks').fetchone()[0])
    def test_no_cross_fragment_agreement_or_document_only_agreement(self):
        m={'source_fragment_ids':['A1_F1','A1_F2'],'news_chunk_id':'N1_F1'}
        self.assertFalse(agree(m,[{'source_chunk_id':'A1_F1','news_chunk_id':'N1_F2'},{'source_chunk_id':'A1_F2','news_chunk_id':'N1_F1'}])[0])
        self.assertFalse(agree(m,[{'source_chunk_id':'A1_OTHER','news_chunk_id':'N1_F1'}])[0])
        self.assertTrue(agree(m,[{'source_chunk_id':x,'news_chunk_id':'N1_F1'} for x in m['source_fragment_ids']])[0])
    def test_future_analytics_excluded(self):
        self.seed();value=analytics();value['document']['date']='2026-10-23';write_json(self.paths['analytics'],value);import_all(self.db,self.paths,True)
        n=retrieve(snapshot(self.db,MODEL),'C1',Settings('2026-10-22'))['news'][0]
        self.assertFalse(n['analytics']);self.assertEqual(n['diagnostics']['excluded_analytics_documents']['future_analytics'],1)
    def test_all_kinds_atomic_on_conflicting_source(self):
        value=company();value['profile_text']='Новый профиль';write_json(self.paths['companies'],value)
        value=analytics();value['document']['source_sha256']='b'*64;write_json(self.paths['analytics'],value)
        with self.assertRaisesRegex(ValueError,'Changed source PDF'):import_all(self.db,self.paths,True)
        with connect(self.db,True) as c:self.assertEqual(c.execute('SELECT profile_text FROM companies').fetchone()[0],company()['profile_text'])
    def test_reject_fabricated_evidence_before_import(self):
        value=analytics();value['mechanisms'][0]['evidence_refs'][0]['quote']='Отсутствующая цитата';write_json(self.paths['analytics'],value)
        with self.assertRaisesRegex(ValueError,'Evidence quote absent'):import_all(self.db,self.paths,True)
    def test_retired_queries_not_embedded(self):
        value=company();value['queries'].append({'id':'other','channel':'finance','text':'Финансирование','weight':1});write_json(self.paths['companies'],value)
        import_all(self.db,self.paths,True);write_json(self.paths['companies'],company());import_all(self.db,self.paths,True)
        with connect(self.db,True) as c:
            self.assertEqual(c.execute('SELECT is_active FROM company_queries WHERE query_id=?',('other',)).fetchone()[0],0)
            self.assertEqual(c.execute("SELECT count(*) FROM active_embedding_inputs WHERE text='Финансирование'").fetchone()[0],0)
    def test_source_context_and_normalization(self):
        value=news();value['fragments'][0]['analytics_source_embedding_text']='Experimental extra role';prepared=prepare_news(value)
        self.assertNotIn('analytics_source_embedding_text',prepared['fragments'][0]);self.assertEqual(prepare_news(prepared),prepared)
        self.assertIn(value['fragments'][0]['mechanism']['context']['conditions'],prepared['fragments'][0]['analytics_embedding_text'])
    def test_reject_duplicate_keys_and_nan(self):
        path=self.root/'invalid.json';path.write_text('{"x":1,"x":2}')
        with self.assertRaises(ValueError):read_json(path)
        path.write_text('{"x":NaN}')
        with self.assertRaises(ValueError):read_json(path)
    def test_sql_rejects_null_provenance_and_immutable_text(self):
        with connect(self.db) as c:
            with self.assertRaises(sqlite3.IntegrityError):c.execute('UPDATE embedding_inputs SET embedding=? WHERE id=1',(b'0'*3072,))
            with self.assertRaises(sqlite3.IntegrityError):c.execute("UPDATE embedding_inputs SET text='changed' WHERE id=1")
    def test_reports_persist_snapshots_and_escape_html(self):
        self.seed();settings=Settings('2026-10-22');report=retrieve(snapshot(self.db,MODEL),'C1',settings)
        report['news'][0]['title']='<script>alert(1)</script>'
        result=save(self.db,self.root/'reports',[report],settings,MODEL,{})
        self.assertNotIn('<script>',(Path(result['output'])/'report.html').read_text(encoding='utf-8'))
        self.assertTrue((self.root/'reports/latest.html').is_file())
        with connect(self.db,True) as c:
            self.assertEqual(c.execute('SELECT count(*) FROM company_reports').fetchone()[0],1);check(c)
    def test_model_mismatch_rejected(self):
        self.seed();bad=copy.deepcopy(MODEL);bad['model_revision']='another-model'
        with self.assertRaisesRegex(ValueError,'different_model'):snapshot(self.db,bad)
    def test_weight_does_not_allow_negative_or_boolean(self):
        value=company();value['queries'][0]['weight']=True;write_json(self.paths['companies'],value)
        with self.assertRaisesRegex(ValueError,'invalid number'):import_all(self.db,self.paths,True)

if __name__=='__main__':unittest.main()
