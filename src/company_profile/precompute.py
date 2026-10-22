"""Пакетный предрасчёт запросов влияния через LLM с сохранением в JSON.

    python -m src.company_profile.precompute --demo --dry-run        # оценка токенов, без вызовов API
    python -m src.company_profile.precompute --demo                  # демо-компании и их группы
    python -m src.company_profile.precompute --large --large-groups --max-tokens 2000000

Результат: precomputed/impact_queries/<id>.json. Уже посчитанные с той же моделью
и версией промпта пропускаются, поэтому запуск можно прерывать и продолжать.
Расход токенов останавливает запуск при превышении --max-tokens.
"""

from __future__ import annotations

import argparse
import json
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from ..llm import LLMClient, LLMError
from . import DataStore, build_group_profile, build_profile, describe_full, describe_group
from .profile import LARGE_BUSINESS_REVENUE
from .queries import company_queries, group_queries
from .refine import MIN_ASPECTS, PROMPT_VERSION, allowed_sources, build_messages, refine

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_OUT = ROOT / "precomputed" / "impact_queries"

# Демо-компании команды (src/company_data.py) и контрастные примеры из разбора данных.
DEMO_COMPANIES = ["U_57706", "U_200890", "U_34911", "U_68157", "U_102920", "U_44792", "U_9250"]
CHARS_PER_TOKEN = 3       # грубая оценка для русского текста
EXPECTED_OUTPUT_TOKENS = 9500   # с учётом скрытых рассуждений модели (замер на DeepSeek)
SPEND_CHECK_EVERY = 10
MAX_FALLBACKS_IN_ROW = 3


def prepare(store: DataStore, subject: str):
    """Черновые запросы, описание и признаки для компании или группы."""
    if subject.startswith("group_"):
        g = build_group_profile(store, subject)
        return group_queries(g), describe_group(g), None
    p = build_profile(store, subject)
    return company_queries(p), describe_full(p), p.features


def _num(n: int) -> str:
    return f"{n:,}".replace(",", " ")


def is_done(path: Path, model: str) -> bool:
    if not path.exists():
        return False
    try:
        data = json.loads(path.read_text())
    except json.JSONDecodeError:
        return False
    return data.get("model") == model and data.get("prompt_version") == PROMPT_VERSION


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("ids", nargs="*", help="SYNTHETIC_U или GROUP_ID")
    parser.add_argument("--demo", action="store_true", help="демо-компании и их группы")
    parser.add_argument("--large", action="store_true", help="все компании с выручкой от 15 млрд")
    parser.add_argument("--large-groups", action="store_true", help="все группы с выручкой от 15 млрд")
    parser.add_argument("--limit", type=int, help="не больше N субъектов за запуск")
    parser.add_argument("--max-tokens", type=int, default=1_000_000, help="остановиться после стольких токенов (вход+выход)")
    parser.add_argument("--max-spend", type=float, help="остановиться, когда расход ключа по /key/info достигнет суммы")
    parser.add_argument("--workers", type=int, default=4, help="параллельных запросов к API")
    parser.add_argument("--force", action="store_true", help="пересчитать уже готовые")
    parser.add_argument("--dry-run", action="store_true", help="только оценить объём, без вызовов API")
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    args = parser.parse_intermixed_args()

    store = DataStore()
    subjects = list(args.ids)
    if args.demo:
        subjects += DEMO_COMPANIES
        subjects += [store.companies.at[c, "GROUP_ID"] for c in DEMO_COMPANIES]
    if args.large:
        large = store.companies[store.companies["REVENUE_COMPANY"] >= LARGE_BUSINESS_REVENUE]
        subjects += large.sort_values("REVENUE_COMPANY", ascending=False).index.tolist()
    if args.large_groups:
        subjects += store.groups[store.groups["revenue"] >= LARGE_BUSINESS_REVENUE].index.tolist()
    subjects = list(dict.fromkeys(subjects))
    if not subjects:
        parser.error("укажите идентификаторы, --demo, --large или --large-groups")

    client = LLMClient()
    try:
        model = "dry-run" if args.dry_run else client.model("queries", fallback="main")
    except LLMError as error:
        parser.error(f"{error}. Заполните .env по образцу .env.example")
    args.out.mkdir(parents=True, exist_ok=True)
    todo = [s for s in subjects if args.force or not is_done(args.out / f"{s}.json", model)]
    if args.limit:
        todo = todo[:args.limit]
    print(f"Субъектов: {len(subjects)}, к расчёту: {len(todo)}, модель: {model}")

    if args.dry_run:
        prompt_tokens = 0
        for s in todo:
            seeds, text, features = prepare(store, s)
            messages = build_messages(seeds, text, allowed_sources(seeds, features))
            prompt_tokens += sum(len(m["content"]) for m in messages) // CHARS_PER_TOKEN
        total = prompt_tokens + EXPECTED_OUTPUT_TOKENS * len(todo)
        print(f"Оценка: ~{_num(prompt_tokens)} токенов на вход, ~{_num(total)} всего "
              f"(~{_num(total // max(len(todo), 1))} на субъект; дозапросы не учтены)")
        return

    started = time.monotonic()
    stats = {"done": 0, "short": 0, "fallback": 0, "aspects": []}
    fallbacks_in_row = 0

    def spend_reached() -> bool:
        if args.max_spend is None:
            return False
        try:
            info = client.key_info()
        except LLMError as error:
            print(f"  не удалось проверить расход: {error}")
            return False
        spend, budget = info.get("spend") or 0.0, info.get("max_budget")
        print(f"  расход ключа: {spend:.2f} из {budget}")
        return spend >= args.max_spend

    if spend_reached():
        print(f"Расход уже не меньше {args.max_spend}, не запускаю.")
        return

    def work(subject: str):
        seeds, text, features = prepare(store, subject)
        result = refine(seeds, text, features, client=client, model=model)
        return subject, result

    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        pending = iter(todo)
        futures = {pool.submit(work, s) for s in [next(pending) for _ in range(min(args.workers, len(todo)))]}
        while futures:
            done = next(as_completed(futures))
            futures.remove(done)
            try:
                subject, result = done.result()
            except (LLMError, KeyError) as error:
                print(f"  ошибка: {error}")
            else:
                fallback = any(n.startswith("LLM недоступна") for n in result.notes)
                payload = {
                    **result.to_dict(),
                    "model": None if fallback else model,
                    "prompt_version": PROMPT_VERSION,
                    "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
                }
                if not fallback:
                    (args.out / f"{subject}.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2))
                n = len(result.queries)
                stats["done"] += 1
                stats["aspects"].append(n)
                stats["short"] += n < MIN_ASPECTS
                stats["fallback"] += fallback
                fallbacks_in_row = fallbacks_in_row + 1 if fallback else 0
                note = f" — {result.notes[0]}, не сохранено" if fallback else ""
                print(f"  [{stats['done']}/{len(todo)}] {subject}: {n} аспектов{note}")

            used = client.usage["prompt_tokens"] + client.usage["completion_tokens"]
            if used >= args.max_tokens:
                print(f"Достигнут лимит {args.max_tokens} токенов, останавливаюсь (запуск можно продолжить).")
                pending = iter(())
            elif fallbacks_in_row >= MAX_FALLBACKS_IN_ROW:
                print(f"{fallbacks_in_row} отказа API подряд, останавливаюсь (возможно, исчерпан лимит).")
                pending = iter(())
            elif stats["done"] % SPEND_CHECK_EVERY == 0 and spend_reached():
                print(f"Расход достиг {args.max_spend}, останавливаюсь (запуск можно продолжить).")
                pending = iter(())
            nxt = next(pending, None)
            if nxt is not None:
                futures.add(pool.submit(work, nxt))

    u = client.usage
    aspects = stats["aspects"] or [0]
    print(
        f"\nГотово: {stats['done']} за {time.monotonic() - started:.0f} с. "
        f"Аспектов: мин {min(aspects)}, среднее {sum(aspects) / len(aspects):.1f}, меньше {MIN_ASPECTS}: {stats['short']}. "
        f"Откат на правила: {stats['fallback']}.\n"
        f"Токены: {u['prompt_tokens']} вход + {u['completion_tokens']} выход за {u['calls']} вызовов, "
        f"стоимость по данным API: {u['cost']:.2f}. "
        f"Результаты: {args.out}"
    )


if __name__ == "__main__":
    main()
