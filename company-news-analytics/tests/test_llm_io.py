import copy,hashlib,tempfile,unittest
from pathlib import Path
from news_analytics.common import ROOT,read_json,sha
from news_analytics.llm_io import prepare_analytics,prepare_news,report_input

FIXTURE=ROOT/'examples/llm'

class LLMContractTests(unittest.TestCase):
    def setUp(self):
        self.a=read_json(FIXTURE/'analytics-draft.example.json')
        self.n=read_json(FIXTURE/'news-draft.example.json')
        self.r=read_json(FIXTURE/'search-report.example.json')
        self.temp=tempfile.TemporaryDirectory();self.pdf=Path(self.temp.name)/'fixture.pdf'
        # A minimal one-page PDF fixture. The preparation step hashes it, not OCRs it.
        objects=[b'<< /Type /Catalog /Pages 2 0 R >>',b'<< /Type /Pages /Kids [3 0 R] /Count 1 >>',
          b'<< /Type /Page /Parent 2 0 R /MediaBox [0 0 100 100] /Resources << >> >>']
        data=b'%PDF-1.4\n';offsets=[0]
        for i,obj in enumerate(objects,1):offsets.append(len(data));data+=str(i).encode()+b' 0 obj\n'+obj+b'\nendobj\n'
        offset=len(data);data+=b'xref\n0 4\n0000000000 65535 f \n'+b''.join(f'{n:010d} 00000 n \n'.encode() for n in offsets[1:])
        data+=b'trailer\n<< /Size 4 /Root 1 0 R >>\nstartxref\n'+str(offset).encode()+b'\n%%EOF\n';self.pdf.write_bytes(data)
    def tearDown(self):self.temp.cleanup()
    def test_preparation_computes_fields_without_rewriting_source_or_mechanism(self):
        result=prepare_analytics(self.a,self.pdf,read_json(FIXTURE/'analytics-input.example.json'))
        self.assertEqual(result['document']['source_sha256'],hashlib.sha256(self.pdf.read_bytes()).hexdigest())
        self.assertEqual(result['chunks'][0]['text'],self.a['chunks'][0]['text'])
        self.assertEqual(result['mechanisms'][0]['mechanism'],self.a['mechanisms'][0]['mechanism'])
        m=result['mechanisms'][0];c=result['chunks'][0]
        self.assertEqual(c['mechanism_ids'],[m['id']]);self.assertEqual(m['source_text'],c['text'])
        self.assertIn(m['context']['conditions'],m['embedding_text'])
        self.assertEqual(c['content_sha256'],sha(c['embedding_text']))
        self.assertIsNone(self.a['document']['source_sha256'])
    def test_incorrect_hash_and_changed_metadata_are_rejected(self):
        bad=copy.deepcopy(self.a);bad['chunks'][0]['content_sha256']='a'*64
        with self.assertRaisesRegex(ValueError,'computed field'):prepare_analytics(bad,self.pdf)
        meta=read_json(FIXTURE/'analytics-input.example.json');meta['document_metadata']['date']='2026-10-21'
        with self.assertRaisesRegex(ValueError,'metadata mismatch'):prepare_analytics(self.a,self.pdf,meta)
    def test_missing_pages_and_invented_analytical_evidence_are_rejected(self):
        bad=copy.deepcopy(self.a);bad['document']['page_count']=2
        with self.assertRaisesRegex(ValueError,'Every PDF page'):prepare_analytics(bad,self.pdf)
        bad=copy.deepcopy(self.a);bad['mechanisms'][0]['evidence_refs'][0]['quote']='Invented quotation'
        with self.assertRaisesRegex(ValueError,'Evidence quote absent'):prepare_analytics(bad,self.pdf)
    def test_news_views_and_original_blocks_match_exactly(self):
        result=prepare_news(self.n,read_json(FIXTURE/'news-input.example.json'));f=result['fragments'][0]
        self.assertEqual(f['analytics_embedding_text'],f['analytics_search_question']+'\nУсловия и границы применимости: '+f['mechanism']['context']['conditions'])
        self.assertIn(f['thesis'],f['company_embedding_text'])
        bad=copy.deepcopy(self.n);bad['source_blocks'][0]['text']='Changed original'
        with self.assertRaisesRegex(ValueError,'changed the supplied'):prepare_news(bad,read_json(FIXTURE/'news-input.example.json'))
        bad=copy.deepcopy(self.n);bad['preparation']['source_access']='partial_text'
        with self.assertRaisesRegex(ValueError,'source access differs'):prepare_news(bad,read_json(FIXTURE/'news-input.example.json'))
    def test_invented_news_quote_or_attribution_is_rejected(self):
        bad=copy.deepcopy(self.n);bad['fragments'][0]['source_quotes']=['Invented']
        with self.assertRaisesRegex(ValueError,'News quote absent'):prepare_news(bad)
        bad=copy.deepcopy(self.n);bad['fragments'][0]['mechanism']['source_attributions'][0]['quote']='Invented'
        with self.assertRaisesRegex(ValueError,'Attribution needs'):prepare_news(bad)
    def test_not_identified_has_no_analytical_query(self):
        bad=copy.deepcopy(self.n);f=bad['fragments'][0]
        f['mechanism'].update(status='not_identified',description=None,source_attributions=[])
        f['analytics_search_question']=None;f['analytics_embedding_text']=None
        result=prepare_news(bad);self.assertIsNone(result['fragments'][0]['analytics_embedding_text'])
    def test_package_deduplicates_sources_and_keeps_news_specific_links(self):
        n=copy.deepcopy(self.r['news'][0]);n['news_id']='NEWS_OTHER'
        n['selected_fragments'][0]['id']='NEWS_OTHER::F001'
        n['analytics'][0]['news_chunk_id']='NEWS_OTHER::F001'
        self.r['news'].append(n);p=report_input(self.r)
        self.assertEqual(len(p['analytical_documents']),1);self.assertEqual(len(p['analytical_source_fragments']),1)
        self.assertEqual(len(p['analytical_mechanisms']),1);self.assertEqual(len(p['connections']),2)
        self.assertEqual({c['news_fragment_id'] for c in p['connections']},{'NEWS_DEMO::F001','NEWS_OTHER::F001'})
        self.assertFalse(p['coverage']['full_news_articles_included'])
    def test_extra_candidates_are_opt_in_and_never_applicable_by_score(self):
        extra=copy.deepcopy(self.r['news'][0]['mechanism_candidates'][0])
        extra['mechanism_id']='AN_DEMO__M002';extra['mechanism']['id']=extra['mechanism_id'];extra['retrieval_agreement']=False
        # An extra must contribute different source support and a different channel.
        extra['mechanism']['mechanism']['channel']='Другой условный канал'
        old=extra['source_fragment_ids'][0];new=old+'_OTHER'
        extra['source_fragment_ids']=[new];extra['source_fragments'][0]['id']=new
        extra['mechanism']['evidence_refs'][0]['fragment_id']=new
        self.r['news'][0]['mechanism_candidates'].append(extra)
        self.assertEqual(len(report_input(self.r)['connections']),1)
        p=report_input(self.r,True);self.assertEqual(len(p['connections']),2)
        self.assertEqual(p['connections'][1]['selection_status'],'single_branch_candidate')
        self.assertTrue(all(c['applicability']=='not_evaluated' for c in p['connections']))
    def test_extra_duplicate_source_support_does_not_expand_packet(self):
        extra=copy.deepcopy(self.r['news'][0]['mechanism_candidates'][0]);extra['mechanism_id']='AN_DEMO__M002'
        extra['mechanism']['mechanism']['channel']='Other';self.r['news'][0]['mechanism_candidates'].append(extra)
        self.assertEqual(len(report_input(self.r,True)['connections']),1)
    def test_packet_contains_cited_news_blocks_and_flags_legacy_evidence(self):
        self.r['news'][0]['source_blocks']=self.n['source_blocks']
        p=report_input(self.r);n=p['news'][0]
        self.assertEqual(n['source_blocks'],self.n['source_blocks']);self.assertEqual(n['source_traceability']['status'],'complete')
        del self.r['news'][0]['source_blocks']
        n=report_input(self.r)['news'][0]
        self.assertEqual(n['source_traceability']['status'],'missing');self.assertFalse(n['source_blocks'])
    def test_news_preparation_rejects_short_quotes_and_missing_blocks(self):
        bad=copy.deepcopy(self.n);bad['fragments'][0]['source_quotes']=['за квартал']
        with self.assertRaisesRegex(ValueError,'too short'):prepare_news(bad)
        del bad['source_blocks']
        with self.assertRaisesRegex(ValueError,'requires original'):prepare_news(bad)
    def test_analytical_quote_offsets_reconstruct_evidence_without_audit_metadata(self):
        p=report_input(self.r);m=p['analytical_mechanisms'][0];e=m['evidence_refs'][0]
        s=p['analytical_source_fragments'][0];original=self.r['news'][0]['analytics'][0]['mechanism']['evidence_refs'][0]['quote']
        self.assertEqual(s['text'][e['start_char']:e['end_char']],original)
        self.assertNotIn('quote',e);self.assertNotIn('source_path',p['analytical_documents'][0])
        self.assertNotIn('source_sha256',p['analytical_documents'][0]);self.assertNotIn('input_fingerprint',p['retrieval'])
    def test_compact_limits_keep_candidates_in_search_snapshot(self):
        for i in range(3):
            n=copy.deepcopy(self.r['news'][0]);n['news_id']='N'+str(i);n['analytics']=[];n['mechanism_candidates']=[]
            for j in range(4):
                a=copy.deepcopy(self.r['news'][0]['analytics'][0]);a['mechanism_id']=f'M{i}_{j}'
                a['mechanism']['mechanism']['channel']=f'Channel{i}_{j}'
                n['analytics'].append(a);n['mechanism_candidates'].append(a)
            self.r['news'].append(n)
        before=copy.deepcopy(self.r);p=report_input(self.r)
        from collections import Counter
        self.assertLessEqual(max(Counter(c['news_id'] for c in p['connections']).values()),2)
        self.assertEqual(len(p['analytical_mechanisms']),5)
        self.assertEqual(self.r,before)
    def test_broken_report_source_or_quote_is_rejected(self):
        bad=copy.deepcopy(self.r);bad['news'][0]['analytics'][0]['source_fragments']=[]
        with self.assertRaisesRegex(ValueError,'Missing analytical source'):report_input(bad)
        bad=copy.deepcopy(self.r);bad['news'][0]['analytics'][0]['mechanism']['evidence_refs'][0]['quote']='Invented'
        with self.assertRaisesRegex(ValueError,'evidence quote absent'):report_input(bad)

if __name__=='__main__':unittest.main()
