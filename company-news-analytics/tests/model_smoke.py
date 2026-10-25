"""Explicit real CPU inference test; separate from fast unit tests."""
from pathlib import Path
import json,tempfile
from news_analytics.common import ROOT,read_json,write_json
from news_analytics.database import initialize
from news_analytics.importing import import_all
from news_analytics.embedding import identity,embed
from news_analytics.retrieval import snapshot
from tests.helpers import company,news,analytics

def main():
    import argparse
    ap=argparse.ArgumentParser();ap.add_argument('--model',type=Path,default=Path(read_json(ROOT/'config.json')['model_path']));args=ap.parse_args()
    model=identity(args.model)
    with tempfile.TemporaryDirectory() as temp:
        root=Path(temp);db=initialize(root/'smoke.db');paths={}
        for kind,data in [('companies',company()),('news',news()),('analytics',analytics())]:
            paths[kind]=root/(kind+'.json');write_json(paths[kind],data)
        import_all(db,paths);first=embed(db,args.model,model,threads=4,batch_size=4)
        assert first['computed']==5 and len(snapshot(db,model)['vectors'])==5
        again=embed(db,args.model,model);assert again['computed']==0
        # Verify refusal before model loading/inference for an oversized input.
        changed=company();changed['queries'][0]['text']='экономика '*5000;write_json(paths['companies'],changed)
        import_all(db,{'companies':paths['companies']},True)
        try:embed(db,args.model,model)
        except ValueError as exc:assert 'truncation refused' in str(exc)
        else:raise AssertionError('Long input was not rejected')
    result=dict(passed=True,real_vectors=5,cached_repeat=0,overlength_rejected=True,embedding=first,model=model)
    write_json(ROOT/'reports/validation/model-smoke.json',result);print(json.dumps(dict(passed=True,real_vectors=5,cached_repeat=0,overlength_rejected=True)))

if __name__=='__main__':main()
