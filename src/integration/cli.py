from pathlib import Path
import argparse
import json
import os
import sys
import time

from .settings import ROOT, load, load_env, path


def brief(item):
    return {k: v for k, v in item.items() if k not in ('report', 'result')}


def main(argv=None):
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8')
    parser = argparse.ArgumentParser(description='Альфа Хак: единая система новостной аналитики')
    parser.add_argument('--config', type=Path, default=ROOT / 'config.json')
    parser.add_argument('--env-file', type=Path, help='локальный файл подключения к API')
    commands = parser.add_subparsers(dest='command', required=True)
    for name in ('init', 'status', 'embed', 'history', 'check'):
        p = commands.add_parser(name)
        if name == 'history':
            p.add_argument('--company')
        if name == 'check':
            p.add_argument('--llm', action='store_true', help='один короткий реальный запрос к API')
    p = commands.add_parser('import')
    for kind in ('companies', 'news', 'analytics'):
        p.add_argument('--' + kind, type=Path)
    p = commands.add_parser('parse')
    p.add_argument('--limit', type=int)
    p = commands.add_parser('prepare-news')
    p.add_argument('--parser-db', type=Path)
    p.add_argument('--limit', type=int)
    p.add_argument('--as-of')
    p.add_argument('--retry-failed', action='store_true')
    p = commands.add_parser('cleanup')
    p.add_argument('--as-of',help='дата для проверки границы хранения, YYYY-MM-DD')
    p.add_argument('--dry-run',action='store_true',help='показать объём очистки без удаления')
    p = commands.add_parser('reuse-cache')
    p.add_argument('--legacy-db', type=Path, required=True)
    p=commands.add_parser('collect-indicators',help='обновить рыночные показатели в основной базе')
    p.add_argument('--days',type=int,default=400)
    p = commands.add_parser('company-from-csv')
    p.add_argument('--company', required=True)
    p.add_argument('--csv-dir', type=Path, required=True)
    p.add_argument('--refine', action='store_true', help='дополнительно уточнить запросы через LLM')
    for name in ('compare', 'generate'):
        p = commands.add_parser(name)
        p.add_argument('--company', required=True)
        p.add_argument('--as-of')
        p.add_argument('--top-news', type=int)
    for name in ('finish','retry-report'):
        p = commands.add_parser(name)
        p.add_argument('--company', required=True)
        p.add_argument('--run-id', required=True)
    for name in ('tick', 'worker'):
        p = commands.add_parser(name)
        p.add_argument('--company', action='append', help='повторите флаг для нескольких компаний')
        p.add_argument('--as-of')
        p.add_argument('--no-parse', action='store_true')
        p.add_argument('--no-prepare', action='store_true')
        p.add_argument('--search-only', action='store_true')
        if name == 'worker':
            p.add_argument('--interval', type=int)
    p = commands.add_parser('serve')
    p.add_argument('--host', default='127.0.0.1')
    p.add_argument('--port', type=int)
    p.add_argument('--no-browser', action='store_true')
    args = parser.parse_args(argv)
    try:
        load_env(args.env_file)
        config = load(args.config)
        if args.command == 'serve':
            from .web import serve
            serve(args.config, config, host=args.host, port=args.port or int(os.environ.get('APP_PORT', '8765')),
                  browser=not args.no_browser)
            return 0
        from .service import Service
        service = Service(config)
        if args.command == 'init':
            result = dict(imports=service.bootstrap(), status=service.status())
        elif args.command in ('status', 'check'):
            result = service.status()
            if args.command == 'check' and args.llm:
                from src.llm import LLMClient
                client = LLMClient()
                answer = client.chat_json(client.model('fast', fallback='main'), [
                    {'role': 'user', 'content': 'Проверка соединения. Ответь только JSON: {"ok":true}'}],
                    task='integration_connection_check', params={'max_tokens': 1024, 'temperature': 0,
                                                                 'reasoning': {'enabled': False}})
                result['llm_connection_verified'] = answer.get('ok') is True
                result['llm_usage'] = client.usage
                if not result['llm_connection_verified']:
                    raise ValueError('API ответил, но проверочный JSON не подтверждён')
        elif args.command == 'import':
            locations = {k: path(getattr(args, k)) for k in ('companies', 'news', 'analytics') if getattr(args, k)}
            if not locations:
                parser.error('укажите --companies, --news или --analytics')
            result = service.import_inputs(locations)
        elif args.command == 'embed':
            result = service.embed()
        elif args.command == 'reuse-cache':
            result = service.reuse_cache(path(args.legacy_db))
        elif args.command=='collect-indicators':
            result=service.collect_indicators(args.days)
        elif args.command == 'company-from-csv':
            from .profiles import company_json
            from news_analytics.common import sha, write_json
            doc = company_json(args.company, path(args.csv_dir), args.refine)
            target = config['upload_folders']['companies'] / (sha(args.company) + '.json')
            write_json(target, doc)
            result = dict(company_file=str(target), imported=service.import_inputs({'companies': target}))
        elif args.command == 'parse':
            result = service.parse(args.limit)
        elif args.command == 'prepare-news':
            result = service.prepare(path(args.parser_db) if args.parser_db else None,
                                     args.limit, args.as_of, args.retry_failed)
        elif args.command == 'cleanup':
            result=service.cleanup(args.as_of,args.dry_run)
        elif args.command in ('compare', 'generate'):
            result = brief(getattr(service, args.command)(args.company, args.as_of, args.top_news))
        elif args.command == 'finish':
            result = brief(service.finish(args.company, args.run_id))
        elif args.command == 'retry-report':
            result = service.retry_report(args.company,args.run_id)
        elif args.command == 'history':
            from news_analytics.workflow import history
            result = history(service.db, args.company)
        elif args.command in ('tick', 'worker'):
            interval = getattr(args, 'interval', None) or config['pipeline']['interval_seconds']
            if interval < 10:
                raise ValueError('Интервал должен быть не меньше 10 секунд')
            while True:
                try:
                    result = service.tick(args.company, args.as_of, not args.no_parse,
                                          not args.no_prepare, not args.search_only)
                except Exception as error:
                    if args.command == 'tick':
                        raise
                    result = {'error': str(error), 'retry_after_seconds': interval}
                if args.command == 'tick':
                    break
                print(json.dumps(result, ensure_ascii=False), flush=True)
                time.sleep(interval)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        if args.command == 'tick' and result.get('errors'):
            return 1
        return 0
    except KeyboardInterrupt:
        return 0
    except Exception as error:
        print('ERROR: ' + str(error), file=sys.stderr)
        return 1
