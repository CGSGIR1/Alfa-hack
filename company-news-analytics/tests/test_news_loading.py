import copy,json,tempfile,unittest
from pathlib import Path
from news_analytics.database import initialize,connect,check
from news_analytics.importing import import_all
from news_analytics.common import write_json
from news_analytics.web import install_uploads
from news_analytics.retrieval import Settings,select_company_news
from news_analytics.packet_selection import select_analytics
from tests.helpers import news
from tests import test_packet_context

class NewsLoadingTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name);self.db=initialize(self.root/'data.db');self.folder=self.root/'news';self.folder.mkdir()
    def tearDown(self):self.tmp.cleanup()
    def test_bad_news_skipped_and_valid_sibling_imported(self):
        write_json(self.folder/'good.json',news());bad=news();bad['fragments'][0]['context']='';write_json(self.folder/'bad.json',bad)
        r=import_all(self.db,{'news':self.folder},skip_invalid_news=True)
        self.assertEqual(r['changed_documents'],1);self.assertEqual(r['rejected_count'],1)
        with connect(self.db,True) as c:self.assertEqual(c.execute('SELECT count(*) FROM news').fetchone()[0],1)
    def test_same_url_new_preparation_id_updates_stable_article(self):
        value=news();write_json(self.folder/'old.json',value);import_all(self.db,{'news':self.folder})
        new=copy.deepcopy(value);new['document_id']='new-preparation';new['document_title']='Updated title';path=self.root/'replacement.json';write_json(path,new)
        r=import_all(self.db,{'news':path},update_existing=True,skip_invalid_news=True)
        self.assertEqual(len(r['news_identity_aliases']),1);self.assertEqual(r['kinds']['news']['updated'],1)
        with connect(self.db,True) as c:
            row=c.execute('SELECT id,title FROM news').fetchall();self.assertEqual(len(row),1);self.assertEqual(row[0]['id'],value['document_id']);self.assertEqual(row[0]['title'],'Updated title');check(c)
    def test_whitespace_and_anchor_do_not_create_second_article(self):
        value=news();write_json(self.folder/'old.json',value);import_all(self.db,{'news':self.folder})
        new=copy.deepcopy(value);new['document_id']='alias';new['source']['url']='  '+value['source']['url']+'#section  ';path=self.root/'replacement.json';write_json(path,new)
        r=import_all(self.db,{'news':path},skip_invalid_news=True);self.assertEqual(r['changed_documents'],0);self.assertEqual(len(r['news_identity_aliases']),1)
    def test_url_identity_conflict_isolated_and_does_not_change_original(self):
        value=news();write_json(self.folder/'old.json',value);import_all(self.db,{'news':self.folder})
        bad=copy.deepcopy(value);bad['source']['url']='https://example.org/another';good=copy.deepcopy(value);good['document_id']='other';good['source']['url']='https://example.org/third'
        staged=self.root/'staged';write_json(staged/'bad.json',bad);write_json(staged/'good.json',good)
        r=import_all(self.db,{'news':staged},update_existing=True,skip_invalid_news=True);self.assertEqual(r['rejected_count'],1);self.assertEqual(r['changed_documents'],1)
        with connect(self.db,True) as c:self.assertEqual(c.execute('SELECT url FROM news WHERE id=?',(value['document_id'],)).fetchone()[0],value['source']['url']);check(c)
    def test_web_upload_bad_sibling_does_not_abort_or_persist(self):
        good=news();bad=copy.deepcopy(good);bad['document_id']='bad';bad['fragments']=[]
        r=install_uploads(self.db,{'news':self.folder},[dict(name='good.json',content=json.dumps(good)),dict(name='bad.json',content=json.dumps(bad))],skip_invalid_news=True)
        self.assertEqual(r['changed_documents'],1);self.assertEqual(r['rejected_count'],1);self.assertEqual(len(list(self.folder.glob('*.json'))),1)
    def test_bad_url_rejected_without_weakening_other_validation(self):
        value=news();value['source']['url']='https://';write_json(self.folder/'bad.json',value)
        r=import_all(self.db,{'news':self.folder},skip_invalid_news=True);self.assertEqual(r['rejected_count'],1);self.assertEqual(r['changed_documents'],0)
    def test_duplicate_url_in_upload_skipped_file_is_not_persisted(self):
        value=news();other=copy.deepcopy(value);other['document_id']='alias'
        r=install_uploads(self.db,{'news':self.folder},[dict(name='one.json',content=json.dumps(value)),dict(name='two.json',content=json.dumps(other))],skip_invalid_news=True)
        self.assertEqual(r['changed_documents'],1);self.assertEqual(r['rejected_count'],1);self.assertEqual(len(list(self.folder.glob('*.json'))),1)
    def test_web_conflicting_existing_news_restores_file_and_imports_good_sibling(self):
        value=news();install_uploads(self.db,{'news':self.folder},[dict(name='old.json',content=json.dumps(value))],skip_invalid_news=True)
        saved={p:p.read_bytes() for p in self.folder.glob('*.json')};bad=copy.deepcopy(value);bad['source']['url']='https://example.org/changed'
        good=copy.deepcopy(value);good['document_id']='second';good['source']['url']='https://example.org/new'
        r=install_uploads(self.db,{'news':self.folder},[dict(name='bad.json',content=json.dumps(bad)),dict(name='good.json',content=json.dumps(good))],skip_invalid_news=True)
        self.assertEqual(r['changed_documents'],1);self.assertEqual(r['rejected_count'],1)
        for p,data in saved.items():self.assertEqual(p.read_bytes(),data)
    def test_empty_news_schema_is_skipped_without_aborting_valid_news(self):
        bad=news();bad['document_id']='bad';bad['schema_version']=None
        r=install_uploads(self.db,{'news':self.folder},[dict(name='bad.json',content=json.dumps(bad)),dict(name='good.json',content=json.dumps(news()))],skip_invalid_news=True)
        self.assertEqual(r['changed_documents'],1);self.assertEqual(r['rejected_count'],1)
    def test_empty_and_null_fields_skip_only_invalid_news(self):
        cases=[('document_title',''),('document_title',None),('source',None),('fragments',None),('fragment',None),('context',''),('context',None),('mechanism',None),('mechanism_context',None),('conditions',None)]
        for i,(field,value) in enumerate(cases):
            with self.subTest(field=field,value=value):
                bad=news();bad['document_id']='bad'+str(i)
                if field in ('document_title','source','fragments'):bad[field]=value
                elif field=='fragment':bad['fragments'][0]=value
                elif field=='mechanism_context':bad['fragments'][0]['mechanism']['context']=value
                elif field=='conditions':bad['fragments'][0]['mechanism']['context']['conditions']=value
                else:bad['fragments'][0][field]=value
                good=news();good['document_id']='valid'+str(i);good['source']['url']='https://example.org/valid'+str(i)
                result=install_uploads(self.db,{'news':self.folder},[dict(name='bad.json',content=json.dumps(bad)),dict(name='good.json',content=json.dumps(good))],skip_invalid_news=True)
                self.assertEqual(result['rejected_count'],1);self.assertEqual(result['changed_documents'],1)
    def test_single_financial_news_budget_includes_base_selection(self):
        data=test_packet_context.PacketContextTests().data();rows,coverage=select_company_news(data,'C',Settings('2026-10-22',top_news=4,financial_topic_coverage=1),True)
        self.assertEqual(len([r for r in rows if r['financial_topic_matches']]),1)
        self.assertTrue(any(t['status']=='omitted_financial_news_limit' for t in coverage['topics']))

if __name__=='__main__':unittest.main()
