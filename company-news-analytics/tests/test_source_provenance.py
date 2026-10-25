import copy
import unittest

from news_analytics.analytics_sources import ORGANIZER_ALFA_IDS, ORGANIZER_ALFA_LABEL, source_provenance
from news_analytics.common import ROOT, read_json
from news_analytics.llm_io import report_input
from news_analytics.reporting import html_reports, markdown


class SourceProvenanceTests(unittest.TestCase):
    def report(self, document_id):
        report=read_json(ROOT/'examples/llm/search-report.example.json')
        for news in report['news']:
            for key in ('analytics','mechanism_candidates','source_candidates','not_in_intersection'):
                for item in news.get(key,[]):
                    item['document_id']=document_id
                    item['document_publisher']='Альфа-Банк'
                    if isinstance(item.get('mechanism'),dict) and item['mechanism'].get('embedding_text') is None:
                        item['mechanism']['embedding_text']='Текст механизма в примере рендеринга.'
        return report

    def test_exact_organizer_set_does_not_label_an_external_publisher_match(self):
        self.assertEqual(len(ORGANIZER_ALFA_IDS),8)
        for did in ORGANIZER_ALFA_IDS:
            self.assertTrue(source_provenance(did)['provided_by_organizer'])
            self.assertEqual(source_provenance(did)['label'],ORGANIZER_ALFA_LABEL)
        for did in ('CBR_CPD_202609','NRA_ADVERTISING_2026','alfa_external_report',None):
            self.assertFalse(source_provenance(did)['provided_by_organizer'])
            self.assertIsNone(source_provenance(did)['label'])

    def test_old_saved_report_receives_marker_without_changing_selection_or_sources(self):
        report=self.report('alfa_strategy_2026_07_22')
        before=copy.deepcopy(report)
        packet=report_input(report)
        self.assertEqual(report,before)
        self.assertEqual(packet['analytical_documents'][0]['source_provenance']['label'],ORGANIZER_ALFA_LABEL)
        external=report_input(self.report('alfa_external_report'))
        self.assertIsNone(external['analytical_documents'][0]['source_provenance']['label'])
        self.assertEqual(packet['connections'],external['connections'])
        self.assertEqual(packet['analytical_source_fragments'][0]['text'],external['analytical_source_fragments'][0]['text'])

    def test_renderers_show_badge_only_for_original_documents(self):
        organizer=self.report('alfa_strategy_2026_07_22')
        external=self.report('CBR_CPD_202609')
        for render in (lambda r:html_reports([r],'test'),markdown):
            self.assertIn(ORGANIZER_ALFA_LABEL,render(organizer))
            self.assertNotIn(ORGANIZER_ALFA_LABEL,render(external))


if __name__=='__main__':unittest.main()
