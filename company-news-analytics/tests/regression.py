"""Standalone historical regression with frozen inputs/vectors, no live database.

Requires the supplied model/runtime identity, but performs no inference. Expected
rankings and judgments are unchanged from the original reviewed 12-news sample.
"""
from pathlib import Path
import argparse,collections,json,tempfile
import numpy as np
from news_analytics.common import ROOT,read_json,write_json,dumps,sha
from news_analytics.database import initialize,connect
from news_analytics.importing import import_all
from news_analytics.embedding import identity,vector
from news_analytics.retrieval import Settings,snapshot,retrieve,compare_analytics
from news_analytics.analytics_sources import source_provenance


def main():
    p=argparse.ArgumentParser();p.add_argument('--model',type=Path,default=Path(read_json(ROOT/'config.json')['model_path']))
    args=p.parse_args();model=identity(args.model);fixture=ROOT/'tests/fixtures/regression'
    manifest=read_json(fixture/'expectations.json');baseline_m=read_json(fixture/'baseline-mechanisms.json')
    baseline_s=read_json(fixture/'baseline-sources.json');fusion=read_json(fixture/'baseline-fusion.json')['results']['intersection']
    settings=Settings('2026-10-22',analytics_per_news=3,min_analytics_score=.35,financial_topic_coverage=0)
    labels=collections.Counter();judgments={(j['case'],j['analytics_id']):j['label'] for j in read_json(fixture/'analytics-review.json')['judgments'] if j['variant']=='analytics-contextual'}
    with tempfile.TemporaryDirectory(prefix='frozen-regression-') as temp:
        db=initialize(Path(temp)/'test.db')
        import_all(db,{k:fixture/k for k in ('companies','news','analytics')})
        with np.load(fixture/'cached-vectors.npz',allow_pickle=False) as arrays,connect(db) as c:
            with c:
                for row in read_json(fixture/'cached-vectors.json'):
                    cached=dict(row,embedding=arrays[row['key']].astype('<f4').tobytes())
                    assert vector(cached,model)[1] is None,'Frozen vectors require the supplied model and pinned runtime'
                    c.execute('''UPDATE embedding_inputs SET embedding=?,model_id=?,model_revision=?,dimensions=768,normalized=1,
                      embedded_input_hash=text_hash,prompt=?,token_count=?,embedded_at=? WHERE role=? AND text_hash=? AND text=?''',
                      (cached['embedding'],cached['model_id'],cached['model_revision'],cached['prompt'],cached['token_count'],cached['embedded_at'],cached['role'],cached['text_hash'],cached['text']))
        data=snapshot(db,model);corpus=read_json(fixture/'corpus.json');frozen={d['id']:d for d in corpus['analytics']}
        assert len(data['analytics'])==len(frozen)
        for doc in data['analytics']:
            assert doc['source_sha256']==frozen[doc['id']]['source_sha256']
            if doc['input_hash']!=frozen[doc['id']]['input_hash']:
                body=json.loads(doc['source_json']);annotation=body['document'].pop('source_provenance',None)
                assert annotation==source_provenance(doc['id']) and sha(dumps(body))==frozen[doc['id']]['input_hash']
        assert {c['id'] for c in data['companies']}==set(corpus['companies'])
        for case in manifest['cases']:
            chunks=[c for c in data['article_chunks'] if c['news_id']==case['news_id']]
            result=compare_analytics(data,case['date'],chunks,settings);code=case['code']
            for key,base,idkey in [('mechanism_candidates',baseline_m,'mechanism_id'),('source_candidates',baseline_s,'source_chunk_id')]:
                rows=result[key];old=base[code]['analytics']
                assert [r[idkey] for r in rows]==[r['id'] for r in old],(code,key,'ranking changed')
                scores=[r['mechanism_similarity'] if key=='mechanism_candidates' else r['similarity'] for r in rows]
                assert np.allclose(scores,[r['similarity'] for r in old],atol=2e-6,rtol=0)
            assert {a['mechanism_id'] for a in result['analytics']}=={a['id'] for a in fusion[code]}
            for row in result['analytics']:labels[judgments[(code,row['mechanism_id'])]]+=1
        for company in data['companies']:
            report=retrieve(data,company['id'],settings);old=read_json(fixture/('selection_'+company['id']+'.json'))
            assert [n['news_id'] for n in report['news']]==[n['news_id'] for n in old['news']]
            assert np.allclose([n['company_score'] for n in report['news']],[n['company_score'] for n in old['news']],atol=2e-6,rtol=0)
    result=dict(passed=True,cases=12,intersection_labels=dict(labels),standalone=True,inference_performed=False)
    write_json(ROOT/'reports/validation/regression.json',result);print(json.dumps(result,ensure_ascii=False))


if __name__=='__main__':main()
