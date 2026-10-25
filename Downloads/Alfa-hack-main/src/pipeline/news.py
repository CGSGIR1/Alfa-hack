"""Новости: база парсера → разметка (Gemma, prepare-news) → БД пайплайна → эмбеддинги.

Срок хранения зависит от важности (config.RETENTION_DAYS):
- high   — несколько содержательных фрагментов и перепечатки в нескольких изданиях,
           либо новость попала в топ отчёта какой-либо компании (mark_important);
- normal — есть хотя бы один фрагмент с механизмом влияния;
- low    — механизм не выделен ни в одном фрагменте.
Истёкшие новости удаляются из БД и из эмбеддингов (cleanup). Их id запоминаются в news_expired,
поэтому ingest не принимает их снова как новые, пока они ещё попадают в окно приёма.
Новости, которые не удалось разметить, запоминаются в news_failed: их повторяют с паузой (config.INGEST_BACKOFF_HOURS)
не больше INGEST_MAX_ATTEMPTS раз, чтобы не платить за один и тот же сбой каждый час.
"""

from __future__ import annotations

import json
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone

from ..embeddings import get_store
from ..llm import LLMClient, LLMUnavailable
from ..news_prep.batch import DEFAULT_OUT as PREPARED_DIR
from ..news_prep.batch import load_events, to_request
from ..news_prep.pipeline import PROMPT_VERSION, prepare_news
from . import store
from .config import (EXPIRED_MEMORY_DAYS, INGEST_ABORT_AFTER_UNAVAILABLE, INGEST_BACKOFF_HOURS,
                     INGEST_MAX_ATTEMPTS, PARSER_DB, RETENTION_DAYS, RETRIEVAL)


def importance(doc: dict, articles_in_event: int) -> str:
    with_mechanism = [f for f in doc["fragments"] if f["mechanism"]["status"] != "not_identified"]
    if not with_mechanism:
        return "low"
    if len(with_mechanism) >= 2 and (articles_in_event >= 2 or len(doc["fragments"]) >= 3):
        return "high"
    return "normal"


def expires(published_utc: str, level: str) -> str:
    published = datetime.fromisoformat(published_utc)
    return (published + timedelta(days=RETENTION_DAYS[level])).isoformat(timespec="seconds")


def _prepared_cached(news_id: str) -> dict | None:
    """Уже размеченная новость той же версии промпта (например, из пакетного прогона)."""
    path = PREPARED_DIR / f"{news_id}.json"
    try:
        doc = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    return doc if doc.get("preparation_meta", {}).get("prompt_version") == PROMPT_VERSION else None


def _prepare(event: dict, client: LLMClient, model: str) -> dict:
    row = event["row"]
    doc = _prepared_cached(row["id"])
    if doc is None:
        doc, report = prepare_news(to_request(row), client=client, model=model)
        doc["preparation_meta"] = {"prompt_version": PROMPT_VERSION, "model": model,
                                   "prepared_at": time.strftime("%Y-%m-%dT%H:%M:%S"), "report": report}
        PREPARED_DIR.mkdir(parents=True, exist_ok=True)
        (PREPARED_DIR / f"{row['id']}.json").write_text(json.dumps(doc, ensure_ascii=False, indent=1), encoding="utf-8")
    doc["event"] = {"event_id": event["event_id"], "article_ids": event["article_ids"]}
    return doc


def remember_expired(con, news_id: str, published_utc: str) -> None:
    """Запомнить новость, срок хранения которой истёк: ingest её больше не возьмёт."""
    con.execute("insert or ignore into news_expired (id, published_utc, expired_at) values (?,?,?)",
                (news_id, published_utc, store.now()))


def record_failure(con, news_id: str, published_utc: str, error: Exception) -> int:
    """Запомнить сбой разметки и назначить следующую попытку. Возвращает число неудачных попыток."""
    row = con.execute("select attempts from news_failed where id = ?", (news_id,)).fetchone()
    attempts = (row["attempts"] if row else 0) + 1
    retry_after = None  # попытки исчерпаны
    if attempts < INGEST_MAX_ATTEMPTS:
        pause = INGEST_BACKOFF_HOURS[min(attempts, len(INGEST_BACKOFF_HOURS)) - 1]
        retry_after = (datetime.now(timezone.utc) + timedelta(hours=pause)).isoformat(timespec="seconds")
    con.execute(
        "insert into news_failed (id, published_utc, attempts, last_error, last_attempt_at, retry_after)"
        " values (?,?,?,?,?,?) on conflict(id) do update set attempts=excluded.attempts,"
        " last_error=excluded.last_error, last_attempt_at=excluded.last_attempt_at, retry_after=excluded.retry_after",
        (news_id, published_utc, attempts, f"{type(error).__name__}: {str(error)[:300]}", store.now(), retry_after))
    return attempts


def save_news(doc: dict, kind: str, published_utc: str, level: str) -> None:
    """Сохранить размеченный документ (новость или событие по показателю) и проиндексировать его."""
    news_id = doc["document_id"]
    fragments = [{**f, "id": f"{news_id}::{f['id']}"} for f in doc["fragments"]]
    get_store().index_news(news_id, fragments)
    with store.connect() as con:
        con.execute(
            "insert into news (id, kind, event_id, published_utc, title, url, publisher, importance, expires_at,"
            " prepared, prepared_at, embedded_at) values (?,?,?,?,?,?,?,?,?,?,?,?)"
            " on conflict(id) do update set prepared=excluded.prepared, importance=excluded.importance,"
            " expires_at=excluded.expires_at, embedded_at=excluded.embedded_at",
            (news_id, kind, (doc.get("event") or {}).get("event_id"), published_utc, doc["document_title"],
             (doc.get("source") or {}).get("url"), (doc.get("source") or {}).get("publisher"), level,
             expires(published_utc, level), store.dumps(doc), store.now(), store.now()))


def ingest(days: int | None = None, limit: int | None = None, workers: int = 8, log=print) -> dict:
    """Забрать новые события из базы парсера за последние days дней, разметить и сохранить."""
    days = days or RETRIEVAL["news_days"]
    since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    with store.connect() as con:
        known = {r["id"] for r in con.execute("select id from news where kind = 'article'")}
        known |= {r["id"] for r in con.execute("select id from news_expired")}
        failed_before = {r["id"] for r in con.execute("select id from news_failed")}
        paused = {r["id"] for r in con.execute(
            "select id from news_failed where retry_after is null or retry_after > ?", (store.now(),))}
    candidates = [e for e in load_events(PARSER_DB) if e["latest"] >= since and e["row"]["id"] not in known]
    events = [e for e in candidates if e["row"]["id"] not in paused]  # отложенные после сбоя пропускаем
    postponed = len(candidates) - len(events)
    if limit:
        events = events[:limit]
    client = LLMClient()
    model = client.model("news", fallback="fast")
    stats = {"new": 0, "reused": 0, "failed": 0, "unavailable": 0, "expired": 0, "postponed": postponed,
             "by_importance": {}}
    log(f"Новых событий за {days} дн.: {len(events)}"
        + (f" (ещё {postponed} отложено или пропущено после прошлых сбоев)" if postponed else ""))

    def work(event):
        reused = _prepared_cached(event["row"]["id"]) is not None
        return event, _prepare(event, client, model), reused

    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(work, e): e for e in events}
        for future in as_completed(futures):
            if future.cancelled():
                continue
            row = futures[future]["row"]
            try:
                event, doc, reused = future.result()
            except LLMUnavailable as error:
                # API недоступно: новость тут ни при чём, попытку не засчитываем — повторим при следующем запуске
                stats["unavailable"] += 1
                log(f"  API недоступно: {str(error)[:150]}")
                if stats["unavailable"] == INGEST_ABORT_AFTER_UNAVAILABLE:
                    log("  слишком много отказов API — остальные запросы в этом запуске не отправляем")
                    for pending in futures:
                        pending.cancel()
                continue
            except Exception as error:  # одна статья не должна останавливать сбор
                stats["failed"] += 1
                with store.connect() as con:
                    attempts = record_failure(con, row["id"], row["published_utc"], error)
                log(f"  ошибка разметки ({attempts}/{INGEST_MAX_ATTEMPTS}): {type(error).__name__}: {str(error)[:150]}"
                    + (" — больше не повторяем" if attempts >= INGEST_MAX_ATTEMPTS else ""))
                continue
            level = importance(doc, len(event["article_ids"]))
            stats["reused" if reused else "new"] += 1
            published = event["row"]["published_utc"]
            if expires(published, level) < store.now():
                # срок хранения уже вышел (новость старше 3/10/30 дней для своей важности):
                # не сохраняем и не индексируем, только запоминаем, чтобы не разбирать её снова
                with store.connect() as con:
                    remember_expired(con, doc["document_id"], published)
                    con.execute("delete from news_failed where id = ?", (row["id"],))
                stats["expired"] += 1
                continue
            save_news(doc, "article", published, level)
            if row["id"] in failed_before:  # раньше не получалось, теперь получилось
                with store.connect() as con:
                    con.execute("delete from news_failed where id = ?", (row["id"],))
            stats["by_importance"][level] = stats["by_importance"].get(level, 0) + 1
    log(f"  размечено заново: {stats['new']}, взято готовых: {stats['reused']}, ошибок: {stats['failed']}, "
        f"уже просрочено (не сохранены): {stats['expired']}, API недоступно: {stats['unavailable']}, "
        f"по важности: {stats['by_importance']}")
    return stats


def mark_important(news_ids: list[str]) -> None:
    """Новость попала в топ отчёта — храним дольше."""
    with store.connect() as con:
        for nid in news_ids:
            row = con.execute("select published_utc from news where id = ?", (nid,)).fetchone()
            if row:
                con.execute("update news set importance = 'high', expires_at = ? where id = ?",
                            (expires(row["published_utc"], "high"), nid))


def cleanup(log=print) -> int:
    """Удалить новости с истёкшим сроком хранения из БД и эмбеддингов."""
    ts = store.now()
    with store.connect() as con:
        rows = con.execute("select id, kind, published_utc from news where expires_at < ?", (ts,)).fetchall()
        ids = [r["id"] for r in rows]
        for r in rows:
            if r["kind"] == "article":  # события по показателям ingest не берёт, их помнить не нужно
                remember_expired(con, r["id"], r["published_utc"])
        con.executemany("delete from news where id = ?", [(i,) for i in ids])
        # id старше EXPIRED_MEMORY_DAYS уже не попадут в окно приёма — забываем их
        forget_before = (datetime.now(timezone.utc) - timedelta(days=EXPIRED_MEMORY_DAYS)).isoformat()
        con.execute("delete from news_expired where published_utc < ?", (forget_before,))
        con.execute("delete from news_failed where published_utc < ?", (forget_before,))
    if ids:
        get_store().delete_news(ids)
    log(f"Удалено новостей с истёкшим сроком: {len(ids)}")
    return len(ids)


def active_news(days: int) -> list[dict]:
    since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    with store.connect() as con:
        rows = con.execute("select * from news where published_utc >= ? and expires_at >= ?",
                           (since, store.now())).fetchall()
    return [{**dict(r), "prepared": store.loads(r["prepared"])} for r in rows]
