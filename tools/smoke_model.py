"""Real offline Gemma/CSV/import/cache smoke test in a temporary database."""
import csv
import json
import os
from pathlib import Path
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import app
from src.integration.service import Service
from src.integration.settings import load
from src.integration.onboarding import csv_directory
from news_analytics.database import connect, check
from news_analytics.embedding import vector


def main():
    config = load()
    directory = csv_directory(config)
    for name in ('df_Uid_data_hack.csv', 'df_Uid_payment_hack.csv', 'df_Fid_Uid_link_hack.csv'):
        with (directory/name).open(encoding='utf-8-sig') as stream:
            assert next(csv.DictReader(stream, delimiter=';')), 'CSV has no records: '+name
    # This test must not consume the real API even if credentials are present.
    os.environ.pop('LLM_API_KEY', None)
    with tempfile.TemporaryDirectory(prefix='alfa-smoke-') as temp:
        folder = Path(temp)
        config.update(database=folder/'main.db', reports=folder/'reports',
                      parser_directory=folder/'parser', prepared_news=folder/'prepared')
        config['upload_folders'] = {kind:folder/'uploads'/kind for kind in ('companies','news','analytics')}
        service = Service(config)
        service.bootstrap()
        with connect(service.db, True) as connection:
            check(connection)
            count = connection.execute('SELECT count(*) FROM companies').fetchone()[0]
            analytical = connection.execute('SELECT count(*) FROM analytics').fetchone()[0]
        assert count == 10 and analytical == 22, 'Unexpected prepared corpus'
        first = service.add_company('U_9250')
        with connect(service.db, True) as connection:
            inputs = list(connection.execute('''SELECT * FROM active_embedding_inputs WHERE id IN
                (SELECT input_id FROM company_queries WHERE company_id='U_9250' AND is_active=1)'''))
        assert inputs
        for row in inputs:
            value, error = vector(row, service.model())
            assert error is None and value.shape == (768,), error
        restarted = Service(config)
        second = restarted.add_company('U_9250')
        assert second['embedding']['computed'] == 0, 'Vectors were recalculated after restart'
        result = dict(passed=True, companies=count, analytics=analytical, csv_files=3,
                      real_model=service.model()['model_id'], dimensions=768,
                      computed=first['embedding']['computed'], reused_after_restart=second['embedding']['reused'],
                      paid_api_calls=0, database_integrity='ok')
        print(json.dumps(result, ensure_ascii=False))


if __name__ == '__main__':
    main()
