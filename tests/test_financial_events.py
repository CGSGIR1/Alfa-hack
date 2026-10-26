"""Financial reprints, real updates and completed rejection memory; no paid API."""
from copy import deepcopy
from contextlib import closing
from pathlib import Path
import json,sys,unittest,sqlite3
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT));sys.path.insert(0,str(ROOT/'company-news-analytics'))
import test_integration as fixture
from news_analytics.common import write_json
from news_analytics.database import connect
from news_analytics.importing import import_all
from src.integration.financial_events import identify,same_fact,notification,remember_source,sources
from src.integration.events import group_feed,filter_candidates
from src.integration.site import feed


def fx(nid='N1',currency='доллара',value='81,97',effective=23,day='2026-10-22',market=False):
    title=('Внебиржевой курс ' if market else 'ЦБ установил курс ')+currency+' на новом уровне'
    opening=('На торгах курс ' if market else 'Банк России установил официальный курс ')+currency+f' на {effective} октября на уровне {value} руб.'
    return dict(id=nid,document_id=nid,title=title,document_title=title,document_date=day,publication_date=day,
        source={'url':'https://example.org/news/1' if nid=='N1' else 'https://example.org/'+nid,'publisher':'Test'},source_blocks=[{'id':'B1','text':opening}],fragments=[])


def rate(nid='N1',day='2026-10-23',value='14'):
    d=dict(id=nid,document_id=nid,document_title='Банк России сохранил ключевую ставку',document_date=day)
    d['source_blocks']=[{'id':'B1','text':f'Банк России на заседании {int(day[8:])} октября сохранил ключевую ставку на уровне {value}% годовых.'}]
    return d


class IdentityTests(unittest.TestCase):
    def test_official_market_and_currencies_are_distinct_forecasts_are_not_quotes(self):
        identities=[identify(fx()),identify(fx(currency='евро')),identify(fx(currency='юаня')),identify(fx(market=True))]
        self.assertEqual(len({f['thread_key'] for f in identities}),4)
        forecast=fx();forecast['document_title']='Прогноз курса доллара: аналитики ожидают укрепления'
        self.assertIsNone(identify(forecast))
        comment=rate();comment['document_title']='Набиуллина: ЦБ обсудил сохранение ключевой ставки'
        self.assertIsNone(identify(comment))
        ambiguous=fx();ambiguous['document_title']='После решения ЦБ внебиржевой курс доллара снизился'
        ambiguous['source_blocks'][0]['text']='На торгах курс доллара снизился до 81,97 руб.'
        self.assertEqual(identify(ambiguous)['kind'],'market_fx')

    def test_unknown_original_evidence_cannot_suppress_assessment(self):
        doc=fx();doc['source_blocks']=[]
        self.assertFalse(identify(doc)['verified'])
        self.assertFalse(same_fact(identify(doc),identify(fx())))

    def test_rounding_of_same_official_fixing_is_reprint_but_correction_is_update(self):
        self.assertTrue(same_fact(identify(fx(value='81,97')),identify(fx(value='81,9'))))
        self.assertFalse(same_fact(identify(fx(value='81,97')),identify(fx(value='81,96'))))
        self.assertFalse(same_fact(identify(fx(value='81,97')),identify(fx(value='81,97',effective=26))))
        self.assertFalse(same_fact(identify(fx(value='81,97',market=True)),identify(fx(value='81,9',market=True))))

    def test_rate_decision_date_changes_event_even_if_rate_does_not_change(self):
        self.assertTrue(same_fact(identify(rate()),identify(rate('N2'))))
        self.assertNotEqual(identify(rate())['thread_key'],identify(rate(day='2026-10-24'))['thread_key'])
        self.assertFalse(same_fact(identify(rate()),identify(rate(value='13'))))

    def test_effective_date_crosses_calendar_year(self):
        doc=fx(day='2026-12-31');doc['source_blocks'][0]['text']='Банк России установил официальный курс доллара на 1 января на уровне 81,97 руб.'
        self.assertEqual(identify(doc)['period'],'2027-01-01')

    def test_latest_eligible_fixing_wins_over_older_high_score_backlog(self):
        docs=[fx('old',effective=22),fx('new',effective=23)]
        data={'news':[dict(id=d['id'],source_json=json.dumps(d)) for d in docs]}
        selected=filter_candidates(data,'C1',[dict(news_id='old',_score=.9),dict(news_id='new',_score=.6)])
        self.assertEqual([c['news_id'] for c in selected],['new'])

    def test_card_updates_immediately_but_unread_version_is_limited_to_12_hours(self):
        a,b,c=[fx(nid=n,effective=day,value=value) for n,day,value in [('a',22,'82'),('b',23,'81'),('c',26,'80')]]
        for doc,stamp in zip((a,b,c),('2026-10-23T00:00:00Z','2026-10-23T06:00:00Z','2026-10-23T13:00:00Z')):doc['completed_at']=stamp
        initial=group_feed([a])[0];within=group_feed([a,b])[0];later=group_feed([a,b,c])[0]
        self.assertEqual(initial['feed_id'],within['feed_id']);self.assertEqual(within['id'],'b')
        self.assertEqual(initial['notification_revision'],within['notification_revision'])
        self.assertNotEqual(initial['notification_revision'],later['notification_revision'])
        repeat=deepcopy(a);repeat.update(id='copy',completed_at='2026-10-24T20:00:00Z')
        self.assertEqual(notification([a]),notification([a,repeat]))


class SavedFinancialTests(unittest.TestCase):
    def setUp(self):
        self.fixture=fixture.IntegratedWorkflowTests();self.fixture.setUp();self.service=self.fixture.service
    def tearDown(self):self.fixture.tearDown()
    def add(self,source):
        doc=fixture.helpers.news()
        for key in ('document_id','document_title','document_date','source','source_blocks'):doc[key]=source[key]
        doc['fragments'][0].update(source_block_ids=['B1'],source_quotes=[source['source_blocks'][0]['text']])
        path=self.fixture.root/(doc['document_id']+'.json');write_json(path,doc)
        import_all(self.service.db,{'news':path},update_existing=True);self.fixture.fill_vectors(self.service.db)
        return doc

    def test_reprint_after_source_deletion_never_repeats_paid_report(self):
        self.add(fx());client=fixture.FakeClient()
        self.service.generate('C1','2026-10-23',client=client);calls=len(client.tasks)
        with connect(self.service.db) as c:
            with c:
                c.execute("DELETE FROM company_news_checks WHERE news_id='N1'")
                c.execute("DELETE FROM article_chunks WHERE news_id='N1'")
                c.execute("DELETE FROM news WHERE id='N1'")
        self.add(fx('N2',value='81,9'))
        for _ in range(2):self.assertEqual(self.service.generate('C1','2026-10-23',client=client)['status'],'idle')
        self.assertEqual(len(client.tasks),calls)

    def test_completed_llm_rejection_also_blocks_republication(self):
        self.add(fx());client=fixture.FakeClient(rejected=True)
        self.service.generate('C1','2026-10-23',client=client);calls=len(client.tasks)
        self.add(fx('N2'))
        self.assertEqual(self.service.generate('C1','2026-10-23',client=client)['status'],'idle')
        self.assertEqual(len(client.tasks),calls)

    def test_new_value_gets_new_report_but_replaces_existing_card(self):
        self.add(fx());client=fixture.FakeClient()
        self.service.generate('C1','2026-10-23',client=client)
        first=feed(self.service.db,'C1',7,'2026-10-23')['news'][0]
        self.add(fx('N2',value='80,53',effective=26,day='2026-10-23'))
        self.assertEqual(self.service.generate('C1','2026-10-23',client=client)['status'],'completed')
        cards=feed(self.service.db,'C1',7,'2026-10-23')['news']
        self.assertEqual(len(cards),1);self.assertEqual(cards[0]['feed_id'],first['feed_id'])
        self.assertEqual(cards[0]['id'],'N2')

    def test_skipped_reprint_keeps_additional_source_without_article_text(self):
        self.add(fx());self.service.generate('C1','2026-10-23',client=fixture.FakeClient())
        remember_source(self.service.db,fx('copy'))
        card=feed(self.service.db,'C1',7,'2026-10-23')['news'][0]
        ref=next(p for p in card['related_publications'] if p['id']=='copy')
        self.assertEqual(ref['source']['url'],'https://example.org/copy')
        self.assertEqual(ref['source_blocks'],[])

    def test_old_prepared_republication_is_cancelled_before_llm(self):
        self.add(fx());client=fixture.FakeClient()
        self.service.generate('C1','2026-10-23',client=client);calls=len(client.tasks)
        self.add(fx('N2'))
        with patch.object(self.service,'_candidate_filter',side_effect=lambda data,cid,candidates:candidates):
            self.assertEqual(self.service.compare('C1','2026-10-23')['status'],'prepared')
        self.assertEqual(self.service.generate('C1','2026-10-23',client=client)['status'],'idle')
        self.assertEqual(len(client.tasks),calls)

    def test_exact_financial_reprint_is_skipped_before_paid_fragmentation(self):
        self.add(fx());parser=self.fixture.root/'parser.db';doc=fx('copy')
        with closing(sqlite3.connect(parser)) as c,c:
            c.execute('CREATE TABLE news(id,event_id,outlet,url,title,subtitle,published_utc,body_clean,is_full)')
            c.execute('INSERT INTO news VALUES(?,?,?,?,?,?,?,?,1)',('copy','copy','kommersant','https://example.org/copy',
                doc['document_title'],'','2026-10-22T10:00:00Z',doc['source_blocks'][0]['text']))
        with patch('src.news_prep.pipeline.prepare_news',side_effect=AssertionError('Duplicate must not call LLM')):
            result=self.service.prepare(parser,as_of='2026-10-23',client=fixture.FakeClient())
        self.assertEqual(result['attempts'],0);self.assertEqual(result['skipped'],1)
        self.assertTrue(any(ref['id']=='copy' for _,ref in sources(self.service.db)))


if __name__=='__main__':unittest.main()
