"""A failed preparation must not loop, starve the queue or spend during an outage."""
from datetime import datetime,timedelta,timezone
from contextlib import closing
from pathlib import Path
import io
import sqlite3
import sys
import tempfile
import unittest
import urllib.error
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT));sys.path.insert(0,str(ROOT/'company-news-analytics'))
import test_integration as fixture
from news_analytics.database import connect
from src.integration.service import Service
from src.integration import preparation_retry as retry
from src.llm import LLMClient,LLMError,LLMResponseError,LLMServiceError
from src.llm.client import _content
from src.news_prep.pipeline import PROMPT_VERSION,prepare_news


class ResponseClassificationTests(unittest.TestCase):
    def test_refusal_empty_and_malformed_response_are_permanent(self):
        for response in (
            {'choices':[{'message':{'refusal':'Cannot process','content':None}}]},
            {'choices':[{'message':{'content':None},'finish_reason':'length'}]},
            {'choices':[{'message':None}]},
            {'choices':[]},
        ):
            with self.subTest(response=response),self.assertRaises(LLMResponseError):_content(response)

    def test_invalid_json_has_one_internal_repair_then_permanent_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            client=LLMClient(cache_dir=tmp)
            with patch.object(client,'chat',return_value='not JSON') as chat:
                with self.assertRaises(LLMResponseError):client.chat_json('fake',[],'test')
                self.assertEqual(chat.call_count,2)

    def test_http_errors_distinguish_access_from_temporary_failures(self):
        for code,retryable,calls in ((401,False,1),(402,False,1),(429,True,2),(503,True,2)):
            with self.subTest(code=code),tempfile.TemporaryDirectory() as tmp:
                client=LLMClient(base_url='https://example.invalid/v1',api_key='test',cache_dir=tmp)
                def failure(*args,**kwargs):
                    raise urllib.error.HTTPError('https://example.invalid',code,'test',{},io.BytesIO(b'test error'))
                with patch('urllib.request.urlopen',side_effect=failure) as network,patch('src.llm.client.time.sleep'):
                    with self.assertRaises(LLMServiceError) as raised:client.chat('fake',[],'test')
                    self.assertEqual(raised.exception.retryable,retryable)
                    self.assertEqual(raised.exception.status_code,code)
                    self.assertEqual(network.call_count,calls)


class PreparationQueueTests(unittest.TestCase):
    def setUp(self):
        self.fixture=fixture.IntegratedWorkflowTests();self.fixture.setUp()
        self.service=self.fixture.service
        self.parser_db=self.fixture.root/'parser-input.db'
        with closing(sqlite3.connect(self.parser_db)) as c,c:
            c.execute('CREATE TABLE news(id,event_id,outlet,url,title,subtitle,published_utc,body_clean,is_full)')
            for nid,hour in (('bad','12'),('good','11')):
                text=('Издержки торговых компаний выросли из-за удорожания перевозок. '
                      'Компании обсуждают повышение цен при сохранении покупательского спроса.')
                c.execute('INSERT INTO news VALUES(?,?,?,?,?,?,?,?,1)',
                    (nid,'event-'+nid,'kommersant','https://example.org/'+nid,
                     'Событие '+nid,'','2026-10-22T'+hour+':00:00Z',text))
        self.tokens=patch.object(self.service,'_check_tokens');self.tokens.start()

    def tearDown(self):
        self.tokens.stop();self.fixture.tearDown()

    def run_prepare(self,service=None,**kwargs):
        return (service or self.service).prepare(self.parser_db,limit=5,as_of='2026-10-23',
                                                client=fixture.FakeClient(),**kwargs)

    def valid(self,request,**kwargs):
        doc=fixture.helpers.news();doc['document_id']=request['metadata']['document_id']
        doc['source']['url']='https://example.org/'+doc['document_id']
        return doc,{'test':True}

    def jobs(self):
        with connect(self.service.db,True) as c:
            return {r['source_id']:dict(r) for r in c.execute('SELECT * FROM news_preparation_jobs')}

    def failing_bad(self,error):
        def agent(request,**kwargs):
            if request['metadata']['document_id']=='bad':raise error
            return self.valid(request)
        return agent

    def test_actual_refusal_is_held_and_next_article_succeeds_even_after_restart(self):
        class RefusalClient(fixture.FakeClient):
            def chat_json(self,*args,**kwargs):return {'error':'Article cannot be processed'}
        def agent(request,**kwargs):
            if request['metadata']['document_id']=='bad':
                return prepare_news(request,client=RefusalClient())
            return self.valid(request)
        with patch('src.news_prep.pipeline.prepare_news',side_effect=agent) as llm:
            result=self.run_prepare()
            self.assertEqual(result['prepared'],1)
            self.assertTrue(result['rejected'][0]['terminal'])
            self.assertEqual(self.jobs()['bad']['status'],'failed')
            self.assertEqual(self.jobs()['bad']['attempts'],1)
            self.assertEqual(self.run_prepare()['attempts'],0)
            restarted=Service(self.fixture.cfg)
            self.assertEqual(self.run_prepare(restarted)['attempts'],0)
            self.assertEqual(llm.call_count,2)

    def test_temporary_outage_waits_survives_restart_and_stops_after_three_attempts(self):
        start=datetime(2026,10,25,tzinfo=timezone.utc)
        with patch('src.news_prep.pipeline.prepare_news',side_effect=self.failing_bad(LLMServiceError('Timeout'))) as llm:
            with patch.object(retry,'clock',return_value=start):
                first=self.run_prepare()
                self.assertEqual(first['attempts'],1);self.assertEqual(self.jobs()['bad']['status'],'retry')
                self.assertEqual(self.run_prepare()['attempts'],0)
                restarted=Service(self.fixture.cfg)
                self.assertEqual(self.run_prepare(restarted)['attempts'],0)
                self.assertEqual(llm.call_count,1)
            with patch.object(retry,'clock',return_value=start+timedelta(seconds=61)):
                self.assertEqual(self.run_prepare()['attempts'],1)
            with patch.object(retry,'clock',return_value=start+timedelta(seconds=182)):
                self.assertTrue(self.run_prepare()['rejected'][0]['terminal'])
                self.assertEqual(self.jobs()['bad']['attempts'],3)
            with patch.object(retry,'clock',return_value=start+timedelta(seconds=423)):
                self.assertEqual(self.run_prepare()['prepared'],1)
                self.assertEqual(self.jobs()['bad']['attempts'],3)
                self.assertEqual(self.run_prepare()['attempts'],0)
            self.assertEqual(llm.call_count,4) # Three failed articles, one successful article.

    def test_invalid_key_pauses_all_work_until_configuration_changes(self):
        with patch('src.news_prep.pipeline.prepare_news',side_effect=LLMServiceError('HTTP 401',retryable=False)) as llm:
            result=self.run_prepare()
            self.assertEqual(result['pause']['error_kind'],'access')
            self.assertEqual(self.jobs()['bad']['status'],'blocked')
            self.assertNotIn('good',self.jobs())
            self.assertEqual(self.run_prepare(Service(self.fixture.cfg))['attempts'],0)
            with patch('src.report.pipeline.generate',side_effect=AssertionError('No paid reports during outage')):
                tick=self.service.tick(company_ids=['C1'],as_of='2026-10-23',parse=False,continuous=True,
                                       client=fixture.FakeClient())
                self.assertEqual(tick['results'],[])
            self.assertEqual(llm.call_count,1)
        with patch.object(retry,'signature',return_value='changed-settings'),patch('src.news_prep.pipeline.prepare_news',side_effect=self.valid):
            self.assertEqual(self.run_prepare()['prepared'],2)
            self.assertIsNone(retry.pause(self.service.db))

    def test_invalid_content_and_generic_errors_do_not_starve_other_articles(self):
        for error in (ValueError('Empty fragments'),IndexError('Malformed content'),LLMError('Unclassified failure')):
            with self.subTest(error=type(error).__name__):
                # Start one fresh bounded round for each failure type.
                retry.reset(self.service.db,PROMPT_VERSION)
                with patch('src.news_prep.pipeline.prepare_news',side_effect=self.failing_bad(error)) as llm:
                    self.run_prepare()
                    attempts=self.jobs()['bad']['attempts']
                    self.assertEqual(self.run_prepare()['attempts'],0)
                    self.assertEqual(self.jobs()['bad']['attempts'],attempts)
                    if not isinstance(error,LLMError):self.assertEqual(self.jobs()['bad']['status'],'failed')
                    self.assertEqual(self.jobs()['good']['status'],'prepared')

    def test_explicit_retry_grants_new_round_without_reprocessing_successes(self):
        with patch('src.news_prep.pipeline.prepare_news',side_effect=self.failing_bad(LLMResponseError('Refused'))) as llm:
            self.run_prepare()
            result=self.run_prepare(retry_failed=True)
            self.assertEqual(result['attempts'],1)
            self.assertEqual(self.jobs()['good']['attempts'],1)
            self.assertEqual(self.jobs()['bad']['attempts'],1)
            self.assertEqual(self.run_prepare()['attempts'],0)
            self.assertEqual(llm.call_count,3)

    def test_unclassified_error_is_also_bounded_without_global_pause(self):
        start=datetime(2026,10,25,tzinfo=timezone.utc)
        with patch('src.news_prep.pipeline.prepare_news',side_effect=self.failing_bad(LLMError('Unknown'))) as llm:
            for seconds in (0,61,182):
                with patch.object(retry,'clock',return_value=start+timedelta(seconds=seconds)):
                    self.run_prepare()
                    self.assertIsNone(retry.pause(self.service.db))
            self.assertEqual(self.jobs()['bad']['attempts'],3)
            self.assertEqual(self.jobs()['bad']['status'],'failed')
            with patch.object(retry,'clock',return_value=start+timedelta(days=1)):
                self.assertEqual(self.run_prepare()['attempts'],0)
            self.assertEqual(llm.call_count,4)

    def test_legacy_refusal_is_migrated_and_exhausted_errors_are_held(self):
        with patch('src.news_prep.pipeline.prepare_news',side_effect=LLMError('Old failure')):
            self.run_prepare()
        with connect(self.service.db) as c,c:
            c.execute('DELETE FROM news_preparation_retry_state')
            c.execute("UPDATE news_preparation_jobs SET status='retry',attempts=25,last_error=? WHERE source_id='bad'",
                      ('Модель отказалась обрабатывать вход: old cached refusal',))
            c.execute("UPDATE news_preparation_jobs SET status='retry',attempts=25 WHERE source_id='good'")
        restarted=Service(self.fixture.cfg)
        with patch('src.news_prep.pipeline.prepare_news',side_effect=AssertionError('No repeat paid calls')):
            self.assertEqual(self.run_prepare(restarted)['attempts'],0)
        self.assertEqual(self.jobs()['bad']['status'],'failed')
        self.assertEqual(self.jobs()['good']['status'],'failed')
        self.assertEqual(self.jobs()['bad']['attempts'],25)

    def test_retention_removes_retry_state_with_expired_job(self):
        with patch('src.news_prep.pipeline.prepare_news',side_effect=LLMResponseError('Refusal')):
            self.run_prepare()
        with connect(self.service.db) as c,c:
            c.execute('DELETE FROM news_preparation_jobs')
            self.assertEqual(c.execute('SELECT count(*) FROM news_preparation_retry_state').fetchone()[0],0)
            self.assertFalse(c.execute('PRAGMA foreign_key_check').fetchall())


if __name__=='__main__':unittest.main()
