"""Генерация отчёта по пакету report-generation-input-1.0.

    python -m src.report generate package.json -o report.json --md report.md --trace trace.json

Модели: LLM_MODEL_REPORT (иначе LLM_MODEL_MAIN) для оценки и написания,
LLM_MODEL_CRITIC (иначе LLM_MODEL_FAST) для критика.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0,str(Path(__file__).resolve().parents[2]/'company-news-analytics'))

from ..llm import LLMError
from .pipeline import generate
from .render import to_markdown


def main() -> None:
    if hasattr(sys.stdout,'reconfigure'):sys.stdout.reconfigure(encoding='utf-8')
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    g = sub.add_parser("generate", help="сгенерировать отчёт")
    g.add_argument("package", type=Path)
    g.add_argument("-o", "--output", type=Path, help="отчёт JSON (по умолчанию stdout)")
    g.add_argument("--md", type=Path, help="отчёт в Markdown")
    g.add_argument("--trace", type=Path, help="трейс шагов")
    g.add_argument("--no-critic", action="store_true")
    d = sub.add_parser("deep", help="углублённый отчёт (логика v2)")
    d.add_argument("package", type=Path)
    d.add_argument("--extras", type=Path, help="полные тексты и кандидаты аналитики (из адаптера)")
    d.add_argument("--budget", type=float, default=20.0)
    d.add_argument("-o", "--output", type=Path)
    d.add_argument("--md", type=Path)
    d.add_argument("--trace", type=Path)
    d.add_argument('--no-critic',action='store_true')
    args = parser.parse_args()

    package = json.loads(args.package.read_text(encoding="utf-8"))
    try:
        inferred_extras={'full_texts':{},'candidates':{}}
        if package.get('schema_version')=='comparison-report-1.0':
            from .adapters import comparison_to_package
            package,inferred_extras=comparison_to_package(package)
        if args.command == "deep":
            from .deep import generate as deep_generate
            extras = json.loads(args.extras.read_text(encoding="utf-8")) if args.extras else inferred_extras
            document, trace = deep_generate(package, extras, budget=args.budget,critic=not args.no_critic)
            if extras.get('catalog'):
                from src.integration.report_context import used_catalog
                document['evidence_catalog']=used_catalog(document,extras)
        else:
            document, trace = generate(package, critic=not args.no_critic)
    except (ValueError, LLMError) as error:
        sys.exit(f"Ошибка: {error}")
    text = json.dumps(document, ensure_ascii=False, indent=2)
    if args.output:
        args.output.write_text(text, encoding="utf-8")
    else:
        print(text)
    if args.md:
        args.md.write_text(to_markdown(document), encoding="utf-8")
    if args.trace:
        args.trace.write_text(json.dumps(trace, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Готово: {len(document['items'])} событий, {len(document['dismissed'])} отброшено, "
          f"{document['usage']['cost']:.2f} ₽, {document['seconds']} с", file=sys.stderr)


if __name__ == "__main__":
    main()
