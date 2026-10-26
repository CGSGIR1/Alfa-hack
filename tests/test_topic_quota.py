"""Regression coverage for repeated rate commentary, without a paid API."""
from datetime import datetime, timedelta, timezone
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT/'company-news-analytics'))
sys.path.insert(0, str(ROOT))
import test_integration as fixture
from news_analytics.common import write_json
from news_analytics.database import connect
from news_analytics.importing import import_all
from news_analytics.retention import cleanup_news
from news_analytics.workflow import history
from src.integration.events import group_feed
from src.integration.site import feed
from src.integration.service import Service
from src.integration import topic_quota as quota


def card(nid, hour, title=None, topic=quota.TOPIC, day='2026-10-25'):
    return dict(id=nid, title=title or nid, publication_date=day,
                completed_at=f'2026-10-25T{hour:02}:00:00+00:00',
                narrative=dict(topic=topic), topic=topic, source={}, fragments=[], source_blocks=[])


class TopicQuotaTests(unittest.TestCase):
    def test_forecast_commentary_and_original_sector_are_recognized(self):
        self.assertTrue(quota.is_rate_news(card('N1',0,'Минфин объяснил прогноз')))
        self.assertTrue(quota.is_rate_news(dict(document_title='За неизменную',fragments=[dict(sectors=[quota.TOPIC])])))
        self.assertTrue(quota.is_rate_news(dict(title='Шохин: бизнес не ждёт снижения ключевой ставки')))
        self.assertTrue(quota.is_rate_news(dict(title='ЦБ сохранил ставку и повысил прогноз')))

    def test_unrelated_taxes_and_fx_with_secondary_rate_sector_stay_separate(self):
        self.assertFalse(quota.is_rate_news(dict(title='Ставка НДС изменится')))
        self.assertFalse(quota.is_rate_news(dict(title='Курс доллара',fragments=[dict(sectors=['Валютный рынок',quota.TOPIC])])))
        self.assertFalse(quota.is_rate_news(card('N2',0,topic='Валютный рынок')))

    def test_legacy_spam_has_one_latest_card_and_stable_id_in_twelve_hours(self):
        first=card('N1',0)
        original=group_feed(quota.feed_windows([first]))[0]
        legacy=[first]+[card('N'+str(n),n) for n in range(2,8)]
        grouped=group_feed(quota.feed_windows(legacy))
        self.assertEqual(len(grouped),1)
        self.assertEqual(grouped[0]['id'],'N7')
        self.assertEqual(grouped[0]['feed_id'],original['feed_id'])
        self.assertEqual(len(grouped[0]['related_publications']),7)

    def test_exact_twelve_hours_opens_a_new_slot_and_other_topics_are_untouched(self):
        grouped=group_feed(quota.feed_windows([card('N1',0),card('N2',11),card('N3',12),card('fish',7,topic='Рыболовство')]))
        self.assertEqual(len(grouped),3)
        self.assertEqual({n['id'] for n in grouped},{'N2','N3','fish'})

    def test_replacement_uses_delivery_time_not_article_date(self):
        grouped=group_feed(quota.feed_windows([card('N1',0),card('N2',2,day='2026-10-24')]))
        self.assertEqual(grouped[0]['id'],'N2')


class PaidQuotaTests(unittest.TestCase):
    def setUp(self):
        self.fixture=fixture.IntegratedWorkflowTests();self.fixture.setUp()
        self.service=self.fixture.service
        self.at=datetime(2026,10,26,0,tzinfo=timezone.utc)
        self.clock_patch=patch('src.integration.topic_quota.clock',side_effect=lambda:self.at);self.clock_patch.start()
        self.saved_clock=patch('news_analytics.workflow.now',side_effect=lambda:self.at.isoformat());self.saved_clock.start()
        self.add('N1','ЦБ сохранил ключевую ставку')

    def tearDown(self):
        self.saved_clock.stop();self.clock_patch.stop();self.fixture.tearDown()

    def add(self,nid,title,topic=quota.TOPIC):
        doc=fixture.helpers.news();doc.update(document_id=nid,document_title=title)
        if nid!='N1':doc['source']['url']='https://example.org/'+nid
        doc['fragments'][0]['sectors']=[topic]
        path=self.fixture.root/(nid+'.json');write_json(path,doc)
        import_all(self.service.db,{'news':path},update_existing=True)
        self.fixture.fill_vectors(self.service.db)
        return doc

    def test_quota_blocks_before_llm_does_not_starve_other_topics_and_reopens(self):
        client=fixture.FakeClient()
        self.assertEqual(self.service.generate('C1','2026-10-23',client=client)['status'],'completed')
        self.add('N2','Минфин объяснил высокую ставку')
        self.add('N3','Рост транспортных затрат','Перевозки')
        self.at+=timedelta(hours=1)
        other=self.service.generate('C1','2026-10-23',client=client)
        self.assertEqual([n['news_id'] for n in other['result']['items']],['N3'])
        self.assertEqual(other['workflow']['interest_rate_quota_skipped'],1)
        calls=len(client.tasks)
        for _ in range(2):
            self.assertEqual(self.service.generate('C1','2026-10-23',client=client)['status'],'idle')
        self.assertEqual(len(client.tasks),calls)
        self.at+=timedelta(hours=11)
        resumed=self.service.generate('C1','2026-10-23',client=client)
        self.assertEqual(resumed['status'],'completed')
        self.assertEqual(resumed['result']['items'][0]['news_id'],'N2')

    def test_rejection_also_limits_paid_attempts(self):
        self.service.generate('C1','2026-10-23',client=fixture.FakeClient(rejected=True))
        self.add('N2','Прогноз ставки')
        client=fixture.FakeClient()
        self.assertEqual(self.service.generate('C1','2026-10-23',client=client)['status'],'idle')
        self.assertEqual(client.tasks,[])

    def test_quota_survives_restart_source_expiry_and_profile_revision(self):
        self.service.generate('C1','2026-10-23',client=fixture.FakeClient())
        cleanup_news(self.service.db,10,'2026-11-02')
        with connect(self.service.db) as c:
            with c:
                c.execute("UPDATE companies SET input_hash='new-profile-revision' WHERE id='C1'")
        self.add('N2','РСПП: бизнес не ждёт снижения ставки')
        restarted=Service(self.fixture.cfg)
        client=fixture.FakeClient()
        with patch.object(restarted,'model',return_value=fixture.MODEL),patch.object(restarted,'_embed',return_value={'computed':0}):
            self.assertEqual(restarted.generate('C1','2026-10-23',client=client)['status'],'idle')
        self.assertEqual(client.tasks,[])
        self.assertEqual(len(feed(self.service.db,'C1',7,'2026-10-26')['news']),1)

    def test_manual_multi_news_batch_also_has_only_one_rate_article(self):
        self.add('N2','Новый прогноз ставки')
        self.add('N3','Перевозки подорожали','Перевозки')
        report=self.service.compare('C1','2026-10-23',top_news=5)
        self.assertEqual(len(report['report']['news']),2)
        self.assertEqual(sum(quota.is_rate_news(n) for n in report['report']['news']),1)

    def test_old_pending_rate_packet_cannot_bypass_quota_with_finish(self):
        self.service.generate('C1','2026-10-23',client=fixture.FakeClient())
        self.add('N2','Новый прогноз ставки')
        with patch('src.integration.topic_quota.last_processed',return_value=None):
            old=self.service.compare('C1','2026-10-23')
        self.assertEqual(old['status'],'prepared')
        client=fixture.FakeClient()
        self.assertEqual(self.service.finish('C1',old['saved']['run_id'],client=client)['status'],'idle')
        self.assertEqual(client.tasks,[])
        self.assertEqual(next(b['status'] for b in history(self.service.db,'C1') if b['run_id']==old['saved']['run_id']),'cancelled')

    def test_other_company_has_its_own_quota(self):
        self.service.generate('C1','2026-10-23',client=fixture.FakeClient())
        doc=fixture.helpers.company();doc['subject_id']='C2'
        path=self.fixture.root/'C2.json';write_json(path,doc)
        import_all(self.service.db,{'companies':path});self.fixture.fill_vectors(self.service.db)
        self.assertEqual(self.service.generate('C2','2026-10-23',client=fixture.FakeClient())['status'],'completed')


if __name__=='__main__':unittest.main()
