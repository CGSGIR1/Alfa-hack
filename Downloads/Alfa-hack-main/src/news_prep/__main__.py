"""Команда prepare-news: подготовка новости по схеме news-two-views-1.0.

    python -m src.news_prep prepare-news input.json -o prepared.json
    python -m src.news_prep prepare-news --text article.txt --id ID --title "..." --date 2026-10-21 \\
        --url https://... --publisher "Коммерсантъ" -o prepared.json

input.json: {"metadata": {...}, "source_access": "full_text", "source_blocks": [{"id": "B001", "text": "..."}]}
В --text абзацы разделяются пустой строкой; первый абзац может быть лидом.
Модель — LLM_MODEL_NEWS, если задана, иначе LLM_MODEL_FAST.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from ..llm import LLMError
from .pipeline import make_request, prepare_news


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("prepare-news", help="подготовить новость")
    p.add_argument("input", nargs="?", type=Path, help="JSON со входом (metadata, source_access, source_blocks)")
    p.add_argument("--text", type=Path, help="текст статьи вместо JSON: абзацы через пустую строку")
    p.add_argument("--id"); p.add_argument("--title"); p.add_argument("--date")
    p.add_argument("--url"); p.add_argument("--publisher")
    p.add_argument("--access", default="full_text", choices=["full_text", "partial_text", "summary_only"])
    p.add_argument("--no-repair", action="store_true", help="без дозапроса по непокрытым частям")
    p.add_argument("-o", "--output", type=Path, help="куда записать результат (по умолчанию stdout)")
    p.add_argument("--report", type=Path, help="куда записать отчёт проверки (по умолчанию stderr)")
    args = parser.parse_args()

    if args.input:
        request = json.loads(args.input.read_text(encoding="utf-8"))
    elif args.text:
        paragraphs = [x.strip() for x in args.text.read_text(encoding="utf-8").split("\n\n")]
        request = make_request(args.id, args.title, args.date, args.url, args.publisher, paragraphs, args.access)
    else:
        parser.error("укажите input.json или --text")

    try:
        document, report = prepare_news(request, repair=not args.no_repair)
    except (ValueError, LLMError) as error:
        sys.exit(f"Ошибка: {error}")

    text = json.dumps(document, ensure_ascii=False, indent=2)
    if args.output:
        args.output.write_text(text, encoding="utf-8")
    else:
        print(text)
    report_text = json.dumps(report, ensure_ascii=False, indent=2)
    if args.report:
        args.report.write_text(report_text, encoding="utf-8")
    else:
        print(report_text, file=sys.stderr)


if __name__ == "__main__":
    main()
