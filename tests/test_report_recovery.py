"""A bad report must not starve other news or silently bypass evidence checks."""
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT));sys.path.insert(0,str(ROOT/'company-news-analytics'))
import test_integration as fixture
from news_analytics.common import write_json
from news_analytics.database import connect
from news_analytics.importing import import_all
from news_analytics.workflow import history,fail_batch
from src.llm import LLMError
from src.report.errors import ReportValidationError
from src.report.package import build
from src.report.pipeline import generate
from src.report.verify import check_report
from src.integration.service import Service


class ConditionalLanguageTests(unittest.TestCase):
    def issues_for(self,text):
        packet=fixture.packet()
        report,_=generate(packet,fixture.FakeClient())
        report['items'][0]['why_it_matters']=[dict(text=text,type='ai_inference',refs=['A1::M1'],company_basis=['/facts/industry/name'])]
        _,index=build(packet)
        assessment=dict(news_assessments=[dict(news_id='N1',label='conditional')],link_assessments=[])
        return check_report(report,index,assessment,packet['company']['facts'],packet['company']['profile'])

    def test_cautious_language_and_explicit_conditions_are_recognized(self):
        for text in ('Численность персонала допускает попадание в диапазон МСП.',
                     'При ухудшении ситуации компании потребуется резерв ликвидности.'):
            with self.subTest(text=text):self.assertEqual(self.issues_for(text),[])

    def test_categorical_predictions_and_irrelevant_word_are_still_rejected(self):
        for text in ('Изменение приведёт к росту прибыли компании.',
                     'Сотрудник допускает ошибки, прибыль компании вырастет.'):
            with self.subTest(text=text):self.assertTrue(self.issues_for(text))

    def test_unrepaired_standard_report_has_terminal_content_error(self):
        class InvalidClient(fixture.FakeClient):
            def chat_json(self,model,messages,task,params=None):
                result=super().chat_json(model,messages,task,params)
                if task in ('report_write','report_revise'):
                    result['items'][0]['why_it_matters']=[dict(text='Прибыль компании обязательно вырастет.',
                        type='ai_inference',refs=['A1::M1'],company_basis=['/facts/industry/name'])]
                return result
        with self.assertRaises(ReportValidationError):generate(fixture.packet(),InvalidClient())

    def test_unrepaired_deep_report_has_terminal_content_error(self):
        from test_agent_updates import DeepClient
        from src.report.deep import generate as deep_generate
        class InvalidClient(DeepClient):
            def chat_json(self,model,messages,task,params=None):
                if task.startswith('revise_items_'):
                    import json
                    part=json.JSONDecoder().raw_decode(messages[-1]['content'].split('Раздел:\n',1)[1])[0]
                    return dict(part=part)
                result=super().chat_json(model,messages,task,params)
                if task.startswith('analyst_'):
                    result['item']['why_it_matters']=[dict(text='Прибыль компании обязательно вырастет.',
                        type='ai_inference',refs=['A1::M1'],company_basis=['/facts/industry/name'])]
                return result
        with self.assertRaises(ReportValidationError):
            deep_generate(fixture.packet(),dict(full_texts={},candidates={}),client=InvalidClient())


class QueueRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.fixture=fixture.IntegratedWorkflowTests();self.fixture.setUp()
        self.service=self.fixture.service

    def tearDown(self):self.fixture.tearDown()

    def add_second_news(self):
        doc=fixture.helpers.news();doc['document_id']='N2'
        doc['document_title']='Другое отраслевое событие';doc['source']['url']='https://example.org/n2'
        target=self.fixture.root/'n2.json';write_json(target,doc)
        import_all(self.service.db,{'news':target});self.fixture.fill_vectors(self.service.db)

    def test_validation_failure_is_held_and_next_news_is_delivered_after_restart(self):
        first=self.service.compare('C1','2026-10-23',top_news=1)
        self.add_second_news()
        with patch('src.report.pipeline.generate',side_effect=ReportValidationError(['Неподтверждённый вывод'])):
            failed=self.service.generate('C1','2026-10-23',top_news=1,client=fixture.FakeClient())
        self.assertEqual(failed['status'],'skipped')
        held=history(self.service.db,'C1')[0]
        self.assertEqual(held['status'],'cancelled');self.assertEqual(held['failed_attempts'],1)
        self.assertTrue((Path(held['output'])/'report-failure.json').is_file())
        with connect(self.service.db,True) as c:
            self.assertEqual(c.execute("SELECT assigned_run_id FROM company_news_checks WHERE news_id='N1'").fetchone()[0],first['saved']['run_id'])
        restarted=Service(self.fixture.cfg)
        with patch.object(restarted,'model',return_value=fixture.MODEL),patch.object(restarted,'_embed',return_value={'computed':0}):
            next_report=restarted.generate('C1','2026-10-23',top_news=1,client=fixture.FakeClient())
            self.assertEqual(next_report['status'],'completed')
            self.assertEqual([n['news_id'] for n in next_report['report']['news']],['N2'])
            self.assertEqual(restarted.generate('C1','2026-10-23',client=fixture.FakeClient())['status'],'idle')
        self.assertEqual(self.service.retry_report('C1',held['run_id'])['status'],'prepared')
        recovered=self.service.finish('C1',held['run_id'],client=fixture.FakeClient())
        self.assertEqual(recovered['status'],'completed')
        with self.assertRaises(ValueError):self.service.retry_report('C1',held['run_id'])

    def test_transient_errors_retry_same_packet_three_times_then_stop(self):
        with patch('src.report.pipeline.generate',side_effect=LLMError('API temporarily unavailable')) as agent:
            for _ in range(2):
                with self.assertRaises(LLMError):self.service.generate('C1','2026-10-23',client=fixture.FakeClient())
                self.assertEqual(history(self.service.db)[0]['status'],'prepared')
            self.assertEqual(self.service.generate('C1','2026-10-23',client=fixture.FakeClient())['status'],'skipped')
            self.assertEqual(self.service.generate('C1','2026-10-23',client=fixture.FakeClient())['status'],'idle')
            self.assertEqual(agent.call_count,3)
        self.assertEqual(history(self.service.db)[0]['failed_attempts'],3)

    def test_old_exhausted_batch_is_not_resumed_or_recreated(self):
        first=self.service.compare('C1','2026-10-23')
        with connect(self.service.db) as c:
            with c:c.execute('UPDATE company_report_batches SET failed_attempts=20,last_error=?',('Old cached failure',))
        self.add_second_news()
        result=self.service.generate('C1','2026-10-23',top_news=1,client=fixture.FakeClient())
        self.assertEqual(result['status'],'completed')
        self.assertEqual([n['news_id'] for n in result['report']['news']],['N2'])
        old=next(b for b in history(self.service.db) if b['run_id']==first['saved']['run_id'])
        self.assertEqual(old['status'],'cancelled')

    def test_automatic_tick_continues_after_invalid_content(self):
        self.service.compare('C1','2026-10-23',top_news=1);self.add_second_news()
        real_generate=generate
        def agent(packet,client=None,**kwargs):
            if packet['news'][0]['id']=='N1':raise ReportValidationError(['Некорректный вывод'])
            return real_generate(packet,client=client,**kwargs)
        with patch('src.report.pipeline.generate',side_effect=agent):
            result=self.service.tick(company_ids=['C1'],as_of='2026-10-23',parse=False,prepare=False,
                                     continuous=True,client=fixture.FakeClient())
        self.assertEqual([r['status'] for r in result['results']],['skipped','completed'])
        self.assertEqual(result['errors'],[])
        self.assertEqual(history(self.service.db)[0]['status'],'completed')

    def test_retry_does_not_displace_another_pending_report(self):
        first=self.service.compare('C1','2026-10-23',top_news=1)
        fail_batch(self.service.db,first['saved']['run_id'],'C1','Invalid content',permanent=True)
        self.add_second_news();self.service.compare('C1','2026-10-23',top_news=1)
        with self.assertRaises(ValueError):self.service.retry_report('C1',first['saved']['run_id'])


if __name__=='__main__':unittest.main()
