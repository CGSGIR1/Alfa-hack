"""Incremental publication, duplicate events and a real SSE connection. No paid API."""
from copy import deepcopy
from datetime import datetime
from pathlib import Path
from http.server import ThreadingHTTPServer
import json
import os
import sys
import threading
import urllib.request
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'company-news-analytics'))
sys.path.insert(0, str(ROOT))
import test_integration as fixture
from news_analytics.common import write_json
from news_analytics.importing import import_all
from news_analytics.workflow import history
from src.integration.events import event_key, filter_candidates, group_feed
from src.integration.site import feed, feed_revision
from src.integration.web import Application, Handler
from src.integration.admission import admits, policy
from news_analytics.retrieval import Settings, choose_company_news


def fixing(nid, day, effective, value):
    return dict(id=nid, document_id=nid, title='ЦБ установил курс доллара ниже 82 рублей',
        document_title='ЦБ установил курс доллара ниже 82 рублей',
        document_date=day, publication_date=day,
        source=dict(url='https://example.org/'+nid), fragments=[],
        source_blocks=[dict(id='B1', text=f'Банк России установил официальный курс доллара на {effective} октября на уровне {value} руб.')])


class EventTests(unittest.TestCase):
    def test_weekend_fixing_date_and_null_event_metadata(self):
        item=fixing('N1','2026-10-23',26,'80,53')
        item['source_blocks'][0]['text']='Банк России на выходные и понедельник, 26 октября, установил официальный курс доллара на уровне 80,53 руб.'
        item['event']=None
        self.assertIn(':26:октября',event_key(item))
        previous=fixing('N2','2026-10-22',23,'81,13')
        self.assertEqual(len(group_feed([item,previous])),1)

    def test_hard_delivery_limit_includes_financial_coverage_extra(self):
        candidates=[dict(news_id='N1',title='Рыбные поставки',_score=.8,publication_date='2026-10-23',financial_topic_matches=[]),
                    dict(news_id='N2',title='Курс доллара',_score=.7,publication_date='2026-10-23',financial_topic_matches=[dict(topic='currency',cosine=.7)])]
        data=dict(company_queries=[],inputs={})
        with patch('news_analytics.retrieval.financial_queries',return_value={'currency':[]}),patch('news_analytics.retrieval.event_topic',side_effect=lambda title,topic:'доллара' in title):
            chosen=choose_company_news(data,'C1',Settings(as_of='2026-10-23',top_news=1,max_total_news=1),candidates)
        self.assertEqual([n['news_id'] for n in chosen],['N1'])

    def test_weak_profile_evidence_can_use_an_explicit_higher_threshold(self):
        weak=dict(company_score=.42,matching_queries=[dict(weighted_score=.42,evidence={'evidence_status':'partial_payment_evidence'})])
        self.assertFalse(admits(weak,minimum=.40,weak_minimum=.45))
        weak['company_score']=.46
        self.assertTrue(admits(weak,minimum=.40,weak_minimum=.45))
        grounded=dict(company_score=.418,matching_queries=[dict(weighted_score=.418,evidence={'evidence_status':'grounded_topic'})])
        self.assertTrue(admits(grounded,minimum=.40,weak_minimum=.45))
    def test_republication_is_duplicate_but_new_daily_fixing_is_new_evidence(self):
        one=fixing('N1','2026-10-21',22,'81,97')
        copy=fixing('N2','2026-10-21',22,'81,9')
        update=fixing('N3','2026-10-22',23,'81,13')
        self.assertEqual(event_key(one),event_key(copy))
        self.assertNotEqual(event_key(one),event_key(update))
        data=dict(news=[dict(id=d['id'],source_json=json.dumps(d)) for d in [one,copy,update]],completed_event_news={'N1'})
        selected=filter_candidates(data,'C1',[dict(news_id='N2',_score=.9),dict(news_id='N3',_score=.8)])
        self.assertEqual([n['news_id'] for n in selected],['N3'])
        grouped=group_feed([update,copy,one])
        self.assertEqual(len(grouped),1)
        self.assertEqual(len(grouped[0]['updates']),2)
        self.assertEqual(len(grouped[0]['related_publications']),3)
        self.assertEqual(grouped[0]['publication_date'],'2026-10-22')

    def test_same_topic_without_matching_event_is_not_suppressed(self):
        one=fixing('N1','2026-10-21',22,'81,97')
        other=deepcopy(one);other.update(id='N2',document_id='N2',title='Экспортёр изменил валютную выручку',document_title='Экспортёр изменил валютную выручку')
        other['source']['url']='https://example.org/N2'
        data=dict(news=[dict(id=d['id'],source_json=json.dumps(d)) for d in [one,other]],completed_event_news={'N1'})
        self.assertEqual(len(filter_candidates(data,'C1',[dict(news_id='N2',_score=.7)])),1)
        self.assertEqual(len(group_feed([one,other])),2)


class IncrementalTests(unittest.TestCase):
    def setUp(self):
        self.fixture=fixture.IntegratedWorkflowTests();self.fixture.setUp()
        self.service=self.fixture.service
        self.service.select_company('C1')
    def tearDown(self):self.fixture.tearDown()

    def add_news(self,nid,title):
        doc=fixture.helpers.news();doc['document_id']=nid;doc['document_title']=title
        doc['source']['url']='https://example.org/'+nid
        target=self.fixture.root/(nid+'.json');write_json(target,doc)
        import_all(self.service.db,{'news':target})
        self.fixture.fill_vectors(self.service.db)
        return doc

    def test_duplicate_is_skipped_before_paid_assessment_and_not_repeated_forever(self):
        client=fixture.FakeClient()
        self.service.generate('C1','2026-10-23',client=client)
        calls=len(client.tasks)
        self.add_news('N2','Рост затрат')
        for _ in range(2):
            result=self.service.generate('C1','2026-10-23',client=client)
            self.assertEqual(result['status'],'idle')
            self.assertEqual(result['workflow']['duplicate_events_skipped'],1)
            self.assertEqual(result['workflow']['queued_after_selection'],0)
        self.assertEqual(len(client.tasks),calls)

    def test_feed_report_keeps_several_news_within_configured_limit(self):
        self.add_news('N2','Новые транспортные расходы')
        current=self.service.generate('C1','2026-10-23',client=fixture.FakeClient(),feed=True)
        self.assertEqual(current['status'],'completed')
        self.assertEqual(len(current['result']['items']),2)

    def test_old_packet_above_feed_limit_is_replaced_by_smaller_reports(self):
        self.add_news('N2','Новые транспортные расходы')
        old=self.service.compare('C1','2026-10-23',top_news=3)
        self.assertEqual(len(old['report']['news']),2)
        self.service.config['retrieval']['max_total_news']=1
        client=fixture.FakeClient()
        for _ in range(2):
            current=self.service.generate('C1','2026-10-23',client=client,feed=True)
            self.assertEqual(current['status'],'completed')
            self.assertEqual(len(current['result']['items']),1)
        rows=history(self.service.db,'C1')
        self.assertEqual(next(r for r in rows if r['run_id']==old['saved']['run_id'])['status'],'cancelled')
        self.assertEqual(sum(r['status']=='completed' for r in rows),2)

    def test_stricter_threshold_does_not_pay_for_unchanged_completed_news_again(self):
        client=fixture.FakeClient()
        self.service.generate('C1','2026-10-23',client=client,feed=True)
        calls=len(client.tasks)
        self.service.config['retrieval']['min_news_score']=.41
        repeated=self.service.generate('C1','2026-10-23',client=client,feed=True)
        self.assertEqual(repeated['status'],'idle')
        self.assertEqual(len(client.tasks),calls)

    def test_feed_uses_delivery_order_rather_than_article_publication_date(self):
        self.service.generate('C1','2026-10-23',client=fixture.FakeClient())
        doc=fixture.helpers.news();doc.update(document_id='N2',document_title='Более старая статья обработана позже',document_date='2026-10-21')
        doc['source']['url']='https://example.org/N2'
        target=self.fixture.root/'older.json';write_json(target,doc);import_all(self.service.db,{'news':target});self.fixture.fill_vectors(self.service.db)
        self.service.generate('C1','2026-10-23',client=fixture.FakeClient())
        items=feed(self.service.db,'C1',7,'2026-10-23')['news']
        self.assertEqual([n['id'] for n in items],['N2','N1'])

    def test_publishes_before_collection_and_between_preparing_articles(self):
        client=fixture.FakeClient()
        def parse():
            self.assertEqual(len(history(self.service.db,'C1')),1)
            return {'status':'finished'}
        def prepare(**kwargs):
            doc=self.add_news('N2','Новые транспортные расходы')
            kwargs['on_prepared'](doc)
            rows=history(self.service.db,'C1')
            self.assertEqual(sum(r['status']=='completed' for r in rows),2)
            # The second article must be visible while the next one is not ready.
            self.assertEqual(len(feed(self.service.db,'C1',7,'2026-10-23')['news']),2)
            return dict(prepared=1,attempts=1,rejected=[])
        with patch.object(self.service,'_parse',side_effect=parse),patch.object(self.service,'_prepare',side_effect=prepare):
            result=self.service.tick(as_of='2026-10-23',client=client,continuous=True)
        self.assertEqual(result['errors'],[])
        self.assertEqual(sum(r['status']=='completed' for r in result['results']),2)
        self.assertEqual(len(history(self.service.db,'C1')),2)

    def test_report_failure_preserves_pending_batch_and_does_not_repeat_in_same_cycle(self):
        with patch('src.report.pipeline.generate',side_effect=fixture.LLMError('temporary API failure')) as generate:
            result=self.service.tick(as_of='2026-10-23',parse=False,prepare=False,client=fixture.FakeClient(),continuous=True)
        self.assertEqual(generate.call_count,1)
        self.assertEqual(history(self.service.db,'C1')[0]['status'],'prepared')
        self.assertFalse(result['continue_soon'])
        self.assertEqual(result['errors'][0]['stage'],'report')

    def test_sse_delivers_completed_result_on_existing_connection_and_is_company_scoped(self):
        config_file=self.fixture.root/'config.json'
        write_json(config_file,{k:({a:str(b) for a,b in v.items()} if k in ('inputs','upload_folders') else str(v) if isinstance(v,Path) else v)
             for k,v in self.fixture.cfg.items()})
        with patch.dict(os.environ,{'PIPELINE_AUTO_POLL':'0'}):app=Application(config_file,self.fixture.cfg)
        server=ThreadingHTTPServer(('127.0.0.1',0),Handler);server.app=app
        thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        address='http://127.0.0.1:'+str(server.server_port)
        response=None
        try:
            before=feed_revision(self.service.db,'C1')
            response=urllib.request.urlopen(address+'/api/site-events?company=C1',timeout=6)
            self.assertEqual(response.headers['Content-Type'],'text/event-stream; charset=utf-8')
            self.assertEqual(response.readline(),b'event: feed\n')
            initial=json.loads(response.readline().decode()[6:]);response.readline()
            self.assertEqual(initial['revision'],before)
            self.service.generate('C1','2026-10-23',client=fixture.FakeClient())
            self.assertEqual(response.readline(),b'event: feed\n')
            updated=json.loads(response.readline().decode()[6:])
            self.assertNotEqual(updated['revision'],before)
            with self.assertRaises(urllib.error.HTTPError):
                urllib.request.urlopen(address+'/api/site-events?company=unknown')
        finally:
            app.stop.set()
            if response:response.close()
            server.shutdown();server.server_close();thread.join(timeout=3);app.pool.shutdown(wait=True)

    def test_default_is_automatic_and_collector_does_not_use_report_lock(self):
        config_file=self.fixture.root/'config.json'
        write_json(config_file,{k:str(v) if isinstance(v,Path) else v for k,v in self.fixture.cfg.items()})
        with patch.dict(os.environ,{},clear=True):app=Application(config_file,self.fixture.cfg)
        try:
            self.assertTrue(app.auto_poll)
            def parse():app.stop.set();app.collect_wake.set();return {'status':'finished'}
            with patch.object(app.service,'lock',side_effect=AssertionError('collector must not wait for LLM')),patch.object(app.service,'_parse',side_effect=parse):
                app.collect_news()
            self.assertEqual(app.collection['status'],'complete')
        finally:app.pool.shutdown(wait=True)


if __name__=='__main__':unittest.main()
