"""Описание компаний и групп компаний из командной строки.

    python -m src.company_profile U_9250 U_57706          # полное описание компаний
    python -m src.company_profile U_9250 --short          # только паспорт
    python -m src.company_profile U_9250 --json           # структурированный профиль
    python -m src.company_profile --large 5               # 5 крупнейших компаний

    python -m src.company_profile group_133188_sb         # отчёт по группе
    python -m src.company_profile U_9250 --group          # отчёт по группе, в которую входит компания
    python -m src.company_profile --large-groups 5        # 5 крупнейших групп

    python -m src.company_profile U_9250 --queries        # запросы влияния (правила, без LLM)
    python -m src.company_profile U_9250 --queries --llm  # запросы, доработанные LLM (нужны LLM_* переменные)
    python -m src.company_profile U_9250 --queries --show-prompt  # промпт для LLM без вызова
"""

from __future__ import annotations

import argparse
import json

from . import DataStore, build_group_profile, build_profile, describe_full, describe_group, describe_short
from .profile import LARGE_BUSINESS_REVENUE
from .queries import QuerySet, company_queries, group_queries
from .refine import allowed_sources, build_messages, refine

SEPARATOR = "\n\n" + "=" * 80 + "\n\n"


def format_queries(qs: QuerySet) -> str:
    lines = [f"## Запросы влияния: {qs.subject_id}"]
    for q in qs.queries:
        mark = " (LLM)" if q.origin == "llm" else ""
        lines.append(f"- {q.weight:.2f} [{q.channel}]{mark} {q.text}\n  почему: {q.why}")
    if qs.notes:
        lines.append("\nЗаметки доработки:\n" + "\n".join(f"- {n}" for n in qs.notes))
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("ids", nargs="*", help="SYNTHETIC_U компаний или GROUP_ID групп")
    parser.add_argument("--short", action="store_true", help="только короткий паспорт компании")
    parser.add_argument("--json", action="store_true", help="вывести результат в JSON")
    parser.add_argument("--group", action="store_true", help="для компаний показать отчёт по их группе")
    parser.add_argument("--large", type=int, metavar="N", help="взять N крупнейших компаний (выручка от 15 млрд)")
    parser.add_argument("--large-groups", type=int, metavar="N", help="взять N крупнейших групп по выручке")
    parser.add_argument("--queries", action="store_true", help="вывести запросы влияния вместо описания")
    parser.add_argument("--llm", action="store_true", help="доработать запросы через LLM")
    parser.add_argument("--show-prompt", action="store_true", help="показать промпт доработки, не вызывая LLM")
    parser.add_argument("--data-dir", help="папка с CSV (по умолчанию data/)")
    args = parser.parse_intermixed_args()

    store = DataStore(args.data_dir)
    ids = list(args.ids)
    if args.large:
        large = store.companies[store.companies["REVENUE_COMPANY"] >= LARGE_BUSINESS_REVENUE]
        ids += large.nlargest(args.large, "REVENUE_COMPANY").index.tolist()
    if args.large_groups:
        ids += store.groups.head(args.large_groups).index.tolist()
    if not ids:
        parser.error("укажите идентификаторы компаний или групп, --large N или --large-groups N")

    for item in ids:
        if args.group and not item.startswith("group_"):
            item = store.companies.at[item, "GROUP_ID"]
        if item.startswith("group_"):
            group = build_group_profile(store, item)
            data, text, features = group.to_dict(), describe_group(group), None
            seeds = group_queries(group) if args.queries else None
        else:
            profile = build_profile(store, item)
            data, features = profile.to_dict(), profile.features
            text = f"{item}: {describe_short(profile)}" if args.short else describe_full(profile)
            seeds = company_queries(profile) if args.queries else None

        if seeds is not None:
            if args.show_prompt:
                for m in build_messages(seeds, text, allowed_sources(seeds, features)):
                    print(f"--- {m['role']} ---\n{m['content']}\n")
                continue
            result = refine(seeds, text, features) if args.llm else seeds
            data, text = result.to_dict(), format_queries(result)

        if args.json:
            print(json.dumps(data, ensure_ascii=False, indent=2))
        else:
            print(text, end=SEPARATOR)


if __name__ == "__main__":
    main()
