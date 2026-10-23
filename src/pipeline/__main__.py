"""Предварительный пайплайн: компания → новости и показатели → пакет → отчёт.

    python -m src.pipeline company U_102920            # профиль и аспекты (кеш или расчёт), индексация
    python -m src.pipeline ingest-news [--limit N]     # новые новости из src/parser/news.db → разметка → БД
    python -m src.pipeline indicators                  # курсы, ставка, индексы → БД, события по резким движениям
    python -m src.pipeline cleanup                     # удалить новости с истёкшим сроком хранения
    python -m src.pipeline package U_102920 -o p.json  # собрать пакет report-generation-input-1.0
    python -m src.pipeline report U_102920 [--force]   # отчёт по правилам обновления (или принудительно)
    python -m src.pipeline tick                        # раз в час: новости, показатели, чистка, отчёты
    python -m src.pipeline init-analytics-stub PACKAGE # временный корпус аналитики из готового пакета

Отчёты для фронтенда: data/reports/<company_id>.json (+ .md, .trace.json).
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import indicators, news
from .companies import CompanyNotFound, get_company, tracked_companies
from .config import ANALYTICS_STUB


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    c = sub.add_parser("company"); c.add_argument("id"); c.add_argument("--refresh", action="store_true")
    i = sub.add_parser("ingest-news"); i.add_argument("--limit", type=int); i.add_argument("--days", type=int)
    sub.add_parser("indicators")
    sub.add_parser("cleanup")
    p = sub.add_parser("package"); p.add_argument("id"); p.add_argument("-o", "--output", type=Path)
    r = sub.add_parser("report"); r.add_argument("id"); r.add_argument("--force", action="store_true")
    t = sub.add_parser("tick"); t.add_argument("--news-limit", type=int)
    s = sub.add_parser("init-analytics-stub"); s.add_argument("package", type=Path)
    args = parser.parse_args()

    try:
        if args.command == "company":
            company = get_company(args.id, refresh=args.refresh)
            print(f"{company['id']} ({company['subject_type']}): аспектов {len(company['aspects'])} "
                  f"[{company['aspects_origin']}]")
            for a in company["aspects"]:
                print(f"  - [{a['category']}] {a['text']}")
        elif args.command == "ingest-news":
            news.ingest(days=args.days, limit=args.limit)
        elif args.command == "indicators":
            indicators.collect()
            indicators.detect_events()
        elif args.command == "cleanup":
            news.cleanup()
        elif args.command == "package":
            from .retrieval import build_package
            package, top = build_package(args.id)
            text = json.dumps(package, ensure_ascii=False, indent=2)
            args.output.write_text(text, encoding="utf-8") if args.output else print(text)
            print(f"новостей {len(package['news'])}, связей {len(package['connections'])}, топ-фрагменты {top}",
                  file=sys.stderr)
        elif args.command == "report":
            from .reports import refresh
            refresh(args.id, force=args.force)
        elif args.command == "tick":
            from .reports import refresh
            news.ingest(limit=args.news_limit)
            indicators.collect()
            indicators.detect_events()
            news.cleanup()
            for company_id in tracked_companies():
                refresh(company_id)
        elif args.command == "init-analytics-stub":
            package = json.loads(args.package.read_text(encoding="utf-8"))
            corpus = {k: package.get(k) or [] for k in
                      ("analytical_documents", "analytical_source_fragments", "analytical_mechanisms")}
            ANALYTICS_STUB.parent.mkdir(parents=True, exist_ok=True)
            ANALYTICS_STUB.write_text(json.dumps(corpus, ensure_ascii=False, indent=1), encoding="utf-8")
            print(f"Корпус аналитики: документов {len(corpus['analytical_documents'])}, "
                  f"механизмов {len(corpus['analytical_mechanisms'])} → {ANALYTICS_STUB}")
    except CompanyNotFound as error:
        sys.exit(f"Ошибка: {error}")


if __name__ == "__main__":
    main()
