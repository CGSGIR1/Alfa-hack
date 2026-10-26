"""Stateful integration tests: batch boundaries, retries, backlog and revisions."""
import copy,json,unittest
from pathlib import Path
from unittest.mock import patch
import numpy as np

from news_analytics.common import read_json,write_json
from news_analytics.database import connect,initialize,check
from news_analytics.importing import import_all
from news_analytics.retrieval import Settings
from news_analytics import workflow,cli
from tests import test_pipeline
from tests.helpers import company,news


class WorkflowTests(unittest.TestCase):
    def setUp(self):
        self.fixture=test_pipeline.PipelineTests('test_idempotent_import');self.fixture.setUp();self.fixture.seed()
        self.db=self.fixture.db;self.root=self.fixture.root;self.output=self.root/'incremental'
        self.model=test_pipeline.MODEL;self.settings=Settings('2026-10-22',financial_topic_coverage=0)
    def tearDown(self):self.fixture.tearDown()
    def run_batch(self,company_id='C1',settings=None):
        return workflow.process_company(self.db,self.output,self.model,company_id,settings or self.settings)
    def add_news(self,nid,**updates):
        d=news();d.update(document_id=nid,source=dict(url='https://example.org/'+nid,publisher='Test'),**updates)
        p=self.root/(nid+'.json');write_json(p,d);import_all(self.db,{'news':p},True);self.fixture.seed();return d
    def result(self,batch):
        packet=read_json(Path(batch['saved']['output'])/'llm-input-C1.json')
        return dict(schema_version='company-analysis-report-1.1',request_id=packet['request_id'],company_id='C1',as_of=packet['as_of'],
            summary='Тестовый ответ: влияние не установлено.',items=[dict(news_id=n['id'],event_summary='Тестовая проверка',
            news_fragment_ids=[f['id'] for f in n['fragments']],assessments=[],limitations=['Искусственный тест, не вывод LLM.']) for n in packet['news']],
            company_context=[],overall_limitations=['Искусственный тест.'])
    def finish(self,batch):return workflow.complete_batch(self.db,batch['saved']['run_id'],'C1',self.result(batch))
    def counts(self):
        with connect(self.db,True) as c:
            return {t:c.execute('SELECT count(*) FROM '+t).fetchone()[0] for t in ('company_news_checks','company_report_batches','comparison_runs')}

    def test_prepared_batch_is_resumed_without_recomparisons_or_new_run(self):
        first=self.run_batch();self.assertEqual(first['workflow']['new_comparisons'],1)
        with patch('news_analytics.workflow.score_company_news',side_effect=AssertionError('recompared')),patch('news_analytics.workflow.retrieve',side_effect=AssertionError('analytics repeated')):
            second=self.run_batch()
        self.assertEqual(second['status'],'resumed');self.assertEqual(first['saved']['run_id'],second['saved']['run_id'])
        self.assertEqual(self.counts(),dict(company_news_checks=1,company_report_batches=1,comparison_runs=1))

    def test_completion_then_idle_does_not_overwrite_latest_or_create_empty_report(self):
        first=self.run_batch();self.finish(first);before=(self.output/'latest.json').read_bytes()
        with patch('news_analytics.workflow.score_company_news',side_effect=AssertionError('recompared')):second=self.run_batch()
        self.assertEqual(second['status'],'idle');self.assertEqual(second['workflow']['new_comparisons'],0)
        self.assertEqual((self.output/'latest.json').read_bytes(),before);self.assertEqual(self.counts()['comparison_runs'],1)

    def test_second_party_only_compares_new_news(self):
        first=self.run_batch();self.finish(first);self.add_news('N2')
        original=workflow.score_company_news;seen=[]
        def spy(data,*args):seen.extend(n['id'] for n in data['news']);return original(data,*args)
        with patch('news_analytics.workflow.score_company_news',side_effect=spy):second=self.run_batch()
        self.assertEqual(seen,['N2']);self.assertEqual(second['workflow']['new_comparisons'],1)
        self.assertEqual([n['news_id'] for n in second['report']['news']],['N2'])

    def test_completed_llm_rejection_does_not_repeat(self):
        first=self.run_batch();result=self.result(first)
        packet=read_json(Path(first['saved']['output'])/'llm-input-C1.json')
        if packet['connections']:
            result['items'][0]['assessments']=[dict(connection_id=packet['connections'][0]['id'],news_mechanism_fit='rejected')]
        workflow.complete_batch(self.db,first['saved']['run_id'],'C1',result)
        self.assertEqual(self.run_batch()['status'],'idle')

    def test_each_company_has_its_own_checks(self):
        self.run_batch();d=company();d['subject_id']='C2';p=self.root/'C2.json';write_json(p,d)
        import_all(self.db,{'companies':p})
        second=self.run_batch('C2');self.assertEqual(second['workflow']['new_comparisons'],1)
        self.assertEqual(self.counts()['company_news_checks'],2)

    def test_negative_comparisons_are_persisted(self):
        with connect(self.db) as c:
            with c:
                v=np.zeros(768,dtype='<f4');v[3]=1
                c.execute("UPDATE embedding_inputs SET embedding=? WHERE role='news_context'",(v.tobytes(),))
        first=self.run_batch();self.assertEqual(first['status'],'idle');self.assertEqual(first['workflow']['not_relevant'],1)
        with patch('news_analytics.workflow.score_company_news',side_effect=AssertionError('recompared')):second=self.run_batch()
        self.assertEqual(second['workflow']['new_comparisons'],0)
        with connect(self.db,True) as c:self.assertEqual(c.execute('SELECT disposition FROM company_news_checks').fetchone()[0],'not_relevant')

    def test_over_limit_news_stays_queued_and_uses_cached_scores(self):
        self.add_news('N2');self.add_news('N3');settings=Settings('2026-10-22',top_news=1,financial_topic_coverage=0)
        first=self.run_batch(settings=settings);self.assertEqual(first['workflow']['queued_after_selection'],2);self.finish(first)
        with patch('news_analytics.workflow.score_company_news',side_effect=AssertionError('recompared')):second=self.run_batch(settings=settings)
        self.assertEqual(second['workflow']['new_comparisons'],0);self.assertEqual(second['workflow']['queued_after_selection'],1)
        self.assertNotEqual(first['report']['news'][0]['news_id'],second['report']['news'][0]['news_id'])

    def test_unpaid_batch_is_repacked_after_news_limit_increase(self):
        self.add_news('N2');first=self.run_batch(settings=Settings('2026-10-22',top_news=1,financial_topic_coverage=0))
        self.assertEqual(len(first['report']['news']),1)
        with patch('news_analytics.workflow.score_company_news',side_effect=AssertionError('recompared')):
            second=self.run_batch(settings=Settings('2026-10-22',top_news=5,financial_topic_coverage=0))
        self.assertEqual(len(second['report']['news']),2)
        statuses={r['run_id']:r['status'] for r in workflow.history(self.db,'C1')}
        self.assertEqual(statuses[first['saved']['run_id']],'cancelled')
        self.assertEqual(statuses[second['saved']['run_id']],'prepared')

    def test_batch_with_llm_answer_is_not_repacked(self):
        self.add_news('N2');first=self.run_batch(settings=Settings('2026-10-22',top_news=1,financial_topic_coverage=0))
        write_json(Path(first['saved']['output'])/'llm-result-C1.json',self.result(first))
        second=self.run_batch(settings=Settings('2026-10-22',top_news=5,financial_topic_coverage=0))
        self.assertEqual(second['saved']['run_id'],first['saved']['run_id'])
        self.assertEqual(len(second['report']['news']),1)

    def test_report_write_failure_keeps_cached_queue_for_retry(self):
        with patch('news_analytics.workflow.save',side_effect=OSError('disk full')):
            with self.assertRaisesRegex(OSError,'disk full'):self.run_batch()
        self.assertEqual(self.counts(),dict(company_news_checks=1,company_report_batches=0,comparison_runs=0))
        with patch('news_analytics.workflow.score_company_news',side_effect=AssertionError('recompared')):second=self.run_batch()
        self.assertEqual(second['status'],'prepared')

    def test_analytical_failure_does_not_consume_pending_news(self):
        with patch('news_analytics.workflow.retrieve',side_effect=RuntimeError('interrupted')):
            with self.assertRaises(RuntimeError):self.run_batch()
        with patch('news_analytics.workflow.score_company_news',side_effect=AssertionError('recompared')):second=self.run_batch()
        self.assertEqual(second['status'],'prepared');self.assertEqual(second['workflow']['new_comparisons'],0)

    def test_failed_llm_and_missing_packet_resume_same_request_after_restart(self):
        first=self.run_batch();packet=Path(first['saved']['output'])/'llm-input-C1.json';original=read_json(packet);packet.unlink()
        workflow.fail_batch(self.db,first['saved']['run_id'],'C1','LLM timeout');initialize(self.db)
        second=self.run_batch();self.assertEqual(second['status'],'resumed');self.assertEqual(read_json(packet),original)
        self.assertEqual(workflow.history(self.db,'C1')[0]['failed_attempts'],1)

    def test_bad_result_cannot_ack_batch_and_completion_is_idempotent(self):
        batch=self.run_batch();result=self.result(batch);bad=copy.deepcopy(result);bad['request_id']='wrong'
        with self.assertRaisesRegex(ValueError,'request_id'):workflow.complete_batch(self.db,batch['saved']['run_id'],'C1',bad)
        self.assertEqual(workflow.history(self.db,'C1')[0]['status'],'prepared')
        self.finish(batch);again=self.finish(batch);self.assertTrue(again['idempotent'])
        bad=copy.deepcopy(result);bad['summary']='Другой ответ'
        with self.assertRaisesRegex(ValueError,'different result'):workflow.complete_batch(self.db,batch['saved']['run_id'],'C1',bad)

    def test_result_must_cover_rejected_news_too(self):
        batch=self.run_batch();bad=self.result(batch);bad['items']=[]
        with self.assertRaisesRegex(ValueError,'every batch news_id'):workflow.complete_batch(self.db,batch['saved']['run_id'],'C1',bad)
        self.assertEqual(workflow.history(self.db,'C1')[0]['status'],'prepared')

    def test_profile_revision_rechecks_pairs_without_new_embedding_text(self):
        first=self.run_batch();self.finish(first);d=company();d['profile_text']='Компания обновила профиль.'
        write_json(self.fixture.paths['companies'],d);import_all(self.db,{'companies':self.fixture.paths['companies']},True)
        second=self.run_batch();self.assertEqual(second['workflow']['new_comparisons'],1)
        self.assertEqual(self.counts()['company_news_checks'],2)

    def test_fabricated_llm_evidence_does_not_complete_batch(self):
        batch=self.run_batch();result=self.result(batch)
        packet=read_json(Path(batch['saved']['output'])/'llm-input-C1.json');link=packet['connections'][0]
        result['items'][0]['assessments']=[dict(connection_id=link['id'],news_mechanism_fit='conditional',evidence=[dict(
            mechanism_id=link['mechanism_id'],source_fragment_id=link['source_fragment_ids'][0],quote='Fabricated quote',pages=[1],source_label=None)])]
        with self.assertRaisesRegex(ValueError,'quote absent'):workflow.complete_batch(self.db,batch['saved']['run_id'],'C1',result)
        self.assertEqual(workflow.history(self.db,'C1')[0]['status'],'prepared')

    def test_news_update_cancels_stale_prepared_batch_and_rechecks(self):
        first=self.run_batch();d=news();d['document_title']='Уточнённый заголовок'
        write_json(self.fixture.paths['news'],d);import_all(self.db,{'news':self.fixture.paths['news']},True)
        with self.assertRaisesRegex(ValueError,'inputs changed'):self.finish(first)
        second=self.run_batch();self.assertEqual(second['status'],'prepared')
        self.assertEqual(second['workflow']['new_comparisons'],1)
        statuses={r['run_id']:r['status'] for r in workflow.history(self.db,'C1')}
        self.assertEqual(statuses[first['saved']['run_id']],'cancelled')
        self.assertEqual(statuses[second['saved']['run_id']],'prepared')

    def test_expired_pending_news_is_not_published_later(self):
        self.add_news('N2');first=self.run_batch(settings=Settings('2026-10-22',top_news=1,financial_topic_coverage=0));self.finish(first)
        second=self.run_batch(settings=Settings('2026-11-22',top_news=1,financial_topic_coverage=0))
        self.assertEqual(second['status'],'idle');self.assertEqual(second['workflow']['new_comparisons'],0)

    def test_changed_limit_does_not_invalidate_comparison_cache(self):
        self.add_news('N2');first=self.run_batch(settings=Settings('2026-10-22',top_news=1,financial_topic_coverage=0));self.finish(first)
        with patch('news_analytics.workflow.score_company_news',side_effect=AssertionError('recompared')):
            second=self.run_batch(settings=Settings('2026-10-22',top_news=5,financial_topic_coverage=0))
        self.assertEqual(second['workflow']['new_comparisons'],0);self.assertEqual(len(second['report']['news']),1)

    def test_stale_snapshot_cannot_be_committed(self):
        original=workflow.save
        def change_before_save(*args,**kwargs):
            d=company();d['profile_text']='Concurrent update';write_json(self.fixture.paths['companies'],d)
            import_all(self.db,{'companies':self.fixture.paths['companies']},True)
            return original(*args,**kwargs)
        with patch('news_analytics.workflow.save',side_effect=change_before_save):
            with self.assertRaisesRegex(ValueError,'Inputs changed'):self.run_batch()
        self.assertEqual(self.counts()['comparison_runs'],0);self.assertEqual(self.counts()['company_report_batches'],0)

    def test_workflow_lock_prevents_parallel_assignment(self):
        with workflow.workflow_lock(self.db):
            with self.assertRaisesRegex(RuntimeError,'busy'):
                with workflow.workflow_lock(self.db):pass

    def test_cli_compare_complete_history_idle_cycle(self):
        cfg=self.root/'cli.json';write_json(cfg,dict(database=str(self.db),reports=str(self.output),model_path=str(self.root),
            retrieval=dict(financial_topic_coverage=0)))
        with patch('news_analytics.cli.identity',return_value=self.model),patch('builtins.print'):
            self.assertEqual(cli.main(['compare','--config',str(cfg),'--company','C1','--as-of','2026-10-22']),0)
            batch=workflow.history(self.db,'C1')[0];report=read_json(Path(batch['output'])/'company_C1.json')
            body=self.result(dict(saved=dict(output=batch['output'])));path=self.root/'llm-result.json';write_json(path,body)
            self.assertEqual(cli.main(['complete','--config',str(cfg),'--company','C1','--run-id',batch['run_id'],'--result',str(path)]),0)
            self.assertEqual(cli.main(['history','--config',str(cfg),'--company','C1']),0)
            self.assertEqual(cli.main(['compare','--config',str(cfg),'--company','C1','--as-of','2026-10-22']),0)
        self.assertEqual(self.counts()['comparison_runs'],1)


if __name__=='__main__':unittest.main()
