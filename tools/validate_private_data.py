"""Check the complete private bundle and import its JSON; PDF originals are optional."""
import csv, hashlib, json, sys, tempfile
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import app
from news_analytics.database import connect, initialize, check
from news_analytics.importing import import_all
from news_analytics.llm_io import report_input
from news_analytics.workflow import validate_result
from src.integration.site import feed

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
    seed = base/'startup/news-analytics.db'
    with connect(seed, True) as connection:
        check(connection)
        assert connection.execute('SELECT count(*) FROM companies').fetchone()[0] == 10
        assert connection.execute("SELECT count(*) FROM company_report_batches WHERE status='completed'").fetchone()[0] == 3
        assert connection.execute("SELECT count(*) FROM company_report_batches WHERE status!='completed'").fetchone()[0] == 0
        assert connection.execute('SELECT count(*) FROM news').fetchone()[0] == 0
        assert connection.execute('SELECT count(*) FROM company_news_checks').fetchone()[0] == 0
        assert connection.execute('SELECT count(*) FROM embedding_inputs WHERE embedding IS NOT NULL').fetchone()[0] == 0
        for row in connection.execute('''SELECT report_json,result_json FROM company_reports
                JOIN company_report_batches USING(run_id,company_id)'''):
            validate_result(json.loads(row['result_json']), report_input(json.loads(row['report_json'])))
    for company_id, amount in [('U_106783',2),('U_9250',1)]:
        cards = feed(seed, company_id, as_of='2026-11-09', admission_policy=dict(minimum=.4,weak_minimum=.45))['news']
        assert len(cards) == amount, (company_id,len(cards))
        assert all(n['completed_at'].startswith('2026-10-25') for n in cards)
    print(json.dumps(dict(passed=True, csv_files=3, documents=counts, pdfs_required=False,
                         startup_reports=3, startup_reports_survive_news_expiry=True,
                         rejected_news=result['rejected_count']), ensure_ascii=False))

if __name__ == '__main__':main()
