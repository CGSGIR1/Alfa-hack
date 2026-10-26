"""Windows-friendly launcher; reopen our running instance without a port error."""
from pathlib import Path
import argparse
import json
import os
import sys
import traceback
import urllib.error
import urllib.request
import webbrowser

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import app  # Sets the retrieval module's portable root before imports.
from src.integration.settings import load, load_env


def open_browser(url):
    try:
        webbrowser.open(url)
    except Exception:
        print('Open this address in your browser: ' + url, flush=True)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--no-browser', action='store_true')
    parser.add_argument('--view',choices=('site','manage'),default='site')
    args = parser.parse_args(argv)
    load_env()
    config = load()
    port = int(os.environ.get('APP_PORT', '8765'))
    url = 'http://127.0.0.1:' + str(port) + '/'
    page_url=url+('manage' if args.view=='manage' else '')
    try:
        with urllib.request.urlopen(url + 'api/state', timeout=5) as response:
            state = json.load(response)
    except (urllib.error.URLError, TimeoutError, ValueError):
        state = None
    if state is not None:
        database = state.get('integration', {}).get('database')
        if database and Path(database).resolve() == config['database'].resolve():
            print('The application is already running: ' + url, flush=True)
            if not args.no_browser:
                open_browser(page_url)
            return 0
        raise RuntimeError(f'Port {port} is used by another application. Change APP_PORT in .env.')
    from src.integration.web import serve
    serve(ROOT / 'config.json', config, port=port, browser=not args.no_browser,
          browser_path='/manage' if args.view=='manage' else '/')
    return 0


if __name__ == '__main__':
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8')
    try:
        raise SystemExit(main())
    except Exception:
        directory = ROOT / 'runtime'
        directory.mkdir(exist_ok=True)
        target = directory / 'last-interface-error.log'
        target.write_text(traceback.format_exc(), encoding='utf-8')
        traceback.print_exc()
        print('Details saved to: ' + str(target), file=sys.stderr)
        raise SystemExit(1)
