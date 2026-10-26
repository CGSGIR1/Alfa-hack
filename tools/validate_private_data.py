"""Check the complete private bundle and import its JSON; PDF originals are optional."""
import csv, hashlib, json, sys, tempfile
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import app
from news_analytics.database import connect, initialize, check
from news_analytics.importing import import_all

def main():
    base = ROOT/'private-data'
    manifest = json.loads((base/'manifest.json').read_text(encoding='utf-8'))
    for row in manifest['files']:
        target = (base/row['path']).resolve()
        assert target.is_relative_to(base.resolve()), 'Unsafe manifest path'
        assert target.stat().st_size == row['bytes'], row['path']
        assert hashlib.sha256(target.read_bytes()).hexdigest() == row['sha256'], row['path']
    for target in (base/'csv').glob('*.csv'):
        with target.open(encoding='utf-8-sig') as stream:
            assert next(csv.DictReader(stream, delimiter=';')), target.name
    with tempfile.TemporaryDirectory(prefix='private-data-') as folder:
        db = initialize(Path(folder)/'check.db')
        result = import_all(db, {kind:base/kind for kind in ('companies','news','analytics')})
        with connect(db, True) as connection:
            check(connection)
            counts = {kind:connection.execute('SELECT count(*) FROM '+kind).fetchone()[0]
                      for kind in ('companies','news','analytics')}
        assert counts == dict(companies=10, news=614, analytics=22), counts
    print(json.dumps(dict(passed=True, csv_files=3, documents=counts, pdfs_required=False,
                         rejected_news=result['rejected_count']), ensure_ascii=False))

if __name__ == '__main__':main()
