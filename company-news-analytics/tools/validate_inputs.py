"""Validate shipped prepared JSON/PDF without loading or downloading any model."""
from pathlib import Path
import hashlib,json,sys,tempfile
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from news_analytics.common import ROOT,read_json
from news_analytics.database import initialize,connect,check
from news_analytics.importing import import_all
from news_analytics.validation import load_inputs


def main():
    cfg=read_json(ROOT/'config.json');locations={k:ROOT/v for k,v in cfg['inputs'].items()}
    counts={};documents=[]
    for kind,path in locations.items():
        loaded=load_inputs(path,kind);counts[kind]=len(loaded)
        if kind=='analytics':documents=[d['document'] for _,d in loaded]
    for doc in documents:
        source=ROOT/doc['source_path']
        if not source.is_file():raise ValueError('Source PDF missing: '+doc['source_path'])
        with source.open('rb') as stream:
            if hashlib.file_digest(stream,'sha256').hexdigest()!=doc['source_sha256']:
                raise ValueError('Source PDF hash mismatch: '+doc['id'])
    with tempfile.TemporaryDirectory(prefix='prepared-check-') as temp:
        db=initialize(Path(temp)/'test.db');result=import_all(db,locations,skip_invalid_news=False)
        with connect(db,True) as c:
            check(c)
            actual={kind:c.execute('SELECT count(*) FROM '+kind).fetchone()[0] for kind in locations}
            if counts!=actual:raise ValueError('Input/import counts differ')
    print(json.dumps(dict(passed=True,documents=counts,pdfs_verified=len(documents),rejected_news=result['rejected_count']),ensure_ascii=False))


if __name__=='__main__':main()
