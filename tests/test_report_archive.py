"""Empty the visible report archive without paying for the same news again."""
from pathlib import Path
import sys,unittest
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT));sys.path.insert(0,str(ROOT/'company-news-analytics'))
import test_integration as fixture
from news_analytics.database import connect
from news_analytics.workflow import history
from src.integration.report_archive import archive_reports
from src.integration.site import feed,feed_revision


class ArchiveTests(unittest.TestCase):
    def setUp(self):
        self.fixture=fixture.IntegratedWorkflowTests();self.fixture.setUp();self.service=self.fixture.service
    def tearDown(self):self.fixture.tearDown()

    def test_archive_empties_feed_and_files_but_preserves_completed_memory(self):
        client=fixture.FakeClient();self.service.generate('C1','2026-10-23',client=client)
        before=feed_revision(self.service.db,'C1');calls=len(client.tasks)
        result=archive_reports(self.service.db,self.service.output)
        self.assertEqual(result['remaining_entries'],0);self.assertEqual(result['report_batches'],1)
        self.assertTrue((Path(result['archive'])/'database-before.db').is_file())
        self.assertEqual(history(self.service.db,'C1'),[])
        self.assertEqual(len(history(self.service.db,'C1',include_archived=True)),1)
        self.assertEqual(feed(self.service.db,'C1',7,'2026-10-23')['news'],[])
        self.assertNotEqual(feed_revision(self.service.db,'C1'),before)
        with connect(self.service.db,True) as c:
            for table in ('companies','news','analytics','company_news_checks','company_report_batches'):
                self.assertEqual(c.execute('SELECT count(*) FROM '+table).fetchone()[0],1,table)
        self.assertEqual(self.service.generate('C1','2026-10-23',client=client)['status'],'idle')
        self.assertEqual(len(client.tasks),calls)
        self.assertFalse(list(self.service.output.iterdir()))

    def test_pending_archived_report_does_not_recreate_outbox(self):
        self.service.compare('C1','2026-10-23')
        archive_reports(self.service.db,self.service.output)
        self.assertEqual(history(self.service.db,'C1'),[])
        self.assertEqual(history(self.service.db,'C1',include_archived=True)[0]['status'],'cancelled')
        client=fixture.FakeClient()
        self.assertEqual(self.service.generate('C1','2026-10-23',client=client)['status'],'idle')
        self.assertEqual(client.tasks,[]);self.assertFalse(list(self.service.output.iterdir()))

    def test_failed_file_move_does_not_hide_reports(self):
        self.service.generate('C1','2026-10-23',client=fixture.FakeClient())
        before=history(self.service.db,'C1')
        with patch('src.integration.report_archive.shutil.move',side_effect=OSError('Test-only locked directory')):
            with self.assertRaises(OSError):archive_reports(self.service.db,self.service.output)
        self.assertEqual(history(self.service.db,'C1'),before)
        self.assertTrue(list(self.service.output.iterdir()))


if __name__=='__main__':unittest.main()
