"""Пакетная подготовка новостей из базы парсера (src/parser/news.db).

    python -m src.news_prep.batch --until 02:55 --max-spend 480 --workers 8

- одна статья на событие (event_id): берётся самая полная, остальные перепечатки
  записываются в event.article_ids;
- только is_full = 1 (так требует README парсера);
- от новых событий к старым;
- готовые (та же версия промпта) пропускаются — запуск можно прерывать и продолжать;
- остановка по времени (--until, местное время), по расходу ключа (/key/info) или
  после нескольких сбоев подряд.

Результат: data/news_prepared/<news_id>.json и журнал data/news_prepared/index.jsonl.
В файлах есть полные тексты статей, поэтому они лежат в data/ (не попадает в git).
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import threading
import time
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from datetime import datetime, timedelta
from pathlib import Path

from ..llm import LLMClient, LLMError
from .pipeline import PROMPT_VERSION, make_request, prepare_news

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_DB = ROOT / "src" / "parser" / "news.db"
DEFAULT_OUT = ROOT / "data" / "news_prepared"
PUBLISHERS = {"kommersant": "Коммерсантъ", "vedomosti": "Ведомости"}
SPEND_CHECK_EVERY = 10
MAX_FAILURES_IN_ROW = 5


def load_events(db: Path) -> list[dict]:
    """По одной (самой полной) статье на событие, от новых событий к старым."""
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    rows = con.execute(
        "select id, event_id, outlet, url, title, subtitle, published_utc, body_clean "
        "from news where is_full = 1 and body_clean is not null and body_clean != ''"
    ).fetchall()
    con.close()
    events: dict[str, list[sqlite3.Row]] = {}
    for r in rows:
        events.setdefault(r["event_id"] or r["id"], []).append(r)
    result = []
    for event_id, articles in events.items():
        main = max(articles, key=lambda r: len(r["body_clean"]))
        result.append({
            "event_id": event_id,
            "article_ids": [r["id"] for r in sorted(articles, key=lambda r: r["published_utc"])],
            "latest": max(r["published_utc"] for r in articles),
            "row": dict(main),
        })
    return sorted(result, key=lambda e: e["latest"], reverse=True)


def to_request(row: dict) -> dict:
    paragraphs = [p.strip() for p in row["body_clean"].split("\n") if p.strip()]
    subtitle = (row.get("subtitle") or "").strip()
    if subtitle and subtitle not in row["body_clean"]:
        paragraphs = [subtitle] + paragraphs
    return make_request(
        document_id=row["id"],
        title=row["title"],
        date=(row["published_utc"] or "")[:10],
        url=row["url"],
        publisher=PUBLISHERS.get(row["outlet"], row["outlet"]),
        paragraphs=paragraphs,
    )


def is_done(path: Path) -> bool:
    try:
        return json.loads(path.read_text()).get("preparation_meta", {}).get("prompt_version") == PROMPT_VERSION
    except (OSError, json.JSONDecodeError):
        return False


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    parser.add_argument("--workers", type=int, default=8)
    parser.add_argument("--limit", type=int, help="не больше N событий за запуск")
    parser.add_argument("--until", help="не начинать новые статьи после этого местного времени, ЧЧ:ММ")
    parser.add_argument("--max-spend", type=float, help="остановиться, когда расход ключа достигнет суммы")
    args = parser.parse_args()

    deadline = None
    if args.until:
        hh, mm = map(int, args.until.split(":"))
        now = datetime.now()
        deadline = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
        if deadline <= now:
            deadline += timedelta(days=1)

    args.out.mkdir(parents=True, exist_ok=True)
    events = load_events(args.db)
    todo = [e for e in events if not is_done(args.out / f"{e['row']['id']}.json")]
    if args.limit:
        todo = todo[:args.limit]
    client = LLMClient()
    model = client.model("news", fallback="fast")
    stop = f"{deadline:%H:%M}" if deadline else "нет"
    print(f"Событий с полным текстом: {len(events)}, к разметке: {len(todo)}, модель: {model}, "
          f"стоп по времени: {stop}, по расходу: {args.max_spend or 'нет'} ₽", flush=True)

    lock = threading.Lock()
    stats = {"done": 0, "failed": 0, "fragments": 0, "uncovered": 0, "cost": 0.0, "fail_row": 0}
    started = time.monotonic()

    def work(event: dict) -> dict:
        row = event["row"]
        t = time.monotonic()
        doc, report = prepare_news(to_request(row), client=LLMClient(), model=model)
        doc["event"] = {"event_id": event["event_id"], "article_ids": event["article_ids"]}
        doc["preparation_meta"] = {"prompt_version": PROMPT_VERSION, "model": model,
                                   "prepared_at": time.strftime("%Y-%m-%dT%H:%M:%S"), "report": report}
        path = args.out / f"{row['id']}.json"
        tmp = path.with_suffix(f".{os.getpid()}.{threading.get_ident()}.tmp")
        tmp.write_text(json.dumps(doc, ensure_ascii=False, indent=1), encoding="utf-8")
        tmp.replace(path)
        return {"id": row["id"], "event_id": event["event_id"], "published": row["published_utc"],
                "title": row["title"], "fragments": report["fragments"], "uncovered": report["uncovered_after"],
                "sentences": report["sentences"], "cost": report["usage"]["cost"],
                "seconds": round(time.monotonic() - t, 1), "calls": len(report["calls"])}

    def should_stop() -> str | None:
        if deadline and datetime.now() >= deadline:
            return f"время {deadline:%H:%M}"
        if stats["fail_row"] >= MAX_FAILURES_IN_ROW:
            return f"{MAX_FAILURES_IN_ROW} сбоев подряд"
        if args.max_spend and stats["done"] and stats["done"] % SPEND_CHECK_EVERY == 0:
            try:
                spend = client.key_info().get("spend") or 0
            except LLMError:
                return None
            print(f"  расход ключа: {spend:.2f} ₽", flush=True)
            if spend >= args.max_spend:
                return f"расход {spend:.2f} ₽"
        return None

    queue = iter(todo)
    stop_reason = None
    with ThreadPoolExecutor(max_workers=args.workers) as pool, (args.out / "index.jsonl").open("a") as log:
        running = {pool.submit(work, e): e for e in [next(queue, None) for _ in range(args.workers)] if e}
        while running:
            finished, _ = wait(running, return_when=FIRST_COMPLETED)
            for future in finished:
                event = running.pop(future)
                with lock:
                    try:
                        rec = future.result()
                    except Exception as error:  # сбой одной статьи не останавливает прогон
                        stats["failed"] += 1
                        stats["fail_row"] += 1
                        rec = {"id": event["row"]["id"], "error": f"{type(error).__name__}: {str(error)[:200]}"}
                        print(f"  ошибка {event['row']['id']}: {rec['error']}", flush=True)
                    else:
                        stats["done"] += 1
                        stats["fail_row"] = 0
                        stats["fragments"] += rec["fragments"]
                        stats["uncovered"] += rec["uncovered"]
                        stats["cost"] += rec["cost"]
                        print(f"  [{stats['done']}/{len(todo)}] {rec['published'][:16]} {rec['fragments']} фр., "
                              f"непокр. {rec['uncovered']}/{rec['sentences']}, {rec['cost']:.2f} ₽, {rec['seconds']} с — "
                              f"{rec['title'][:60]}", flush=True)
                    log.write(json.dumps(rec, ensure_ascii=False) + "\n")
                    log.flush()
                    stop_reason = stop_reason or should_stop()
                if not stop_reason:
                    nxt = next(queue, None)
                    if nxt:
                        running[pool.submit(work, nxt)] = nxt

    elapsed = time.monotonic() - started
    print(f"\nИтог: размечено {stats['done']}, ошибок {stats['failed']}, фрагментов {stats['fragments']}, "
          f"непокрытых предложений {stats['uncovered']}, {stats['cost']:.2f} ₽ за {elapsed / 60:.0f} мин. "
          f"Остановка: {stop_reason or 'все события обработаны'}.", flush=True)


if __name__ == "__main__":
    main()
