"""Отчёты: когда пересчитывать и где хранить для фронтенда.

Отчёт по компании пересчитывается, если:
- его ещё нет (first);
- ему больше REPORT_MAX_AGE_HOURS (stale);
- в топ-TRIGGER_TOP_N фрагментов по сходству появился фрагмент, которого не было при прошлой
  генерации (new_top_fragment) — видимо, пришла важная новость.
Иначе возвращается последний отчёт без вызовов LLM.

Для фронтенда: data/reports/<company_id>.json (последний отчёт) и <company_id>.md, а также get_report().
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from ..report import generate
from ..report.render import to_markdown
from . import news, store
from .config import REPORT_MAX_AGE_HOURS, REPORTS_DIR
from .retrieval import build_package


def last_report(company_id: str) -> dict | None:
    with store.connect() as con:
        row = con.execute("select * from reports where company_id = ? order by generated_at desc limit 1",
                          (company_id,)).fetchone()
    if not row:
        return None
    return {"generated_at": row["generated_at"], "reason": row["reason"],
            "top_fragments": store.loads(row["top_fragments"]), "report": store.loads(row["report"])}


def decide(company_id: str, top_fragments: list[str]) -> tuple[bool, str]:
    last = last_report(company_id)
    if last is None:
        return True, "first"
    age = datetime.now(timezone.utc) - datetime.fromisoformat(last["generated_at"])
    if age > timedelta(hours=REPORT_MAX_AGE_HOURS):
        return True, "stale"
    new = [f for f in top_fragments if f not in set(last["top_fragments"])]
    if new:
        return True, f"new_top_fragment:{','.join(new)}"
    return False, "up_to_date"


def refresh(company_id: str, force: bool = False, log=print) -> dict:
    """Пересчитать отчёт по правилам (или принудительно) и вернуть актуальный."""
    package, top_fragments = build_package(company_id)
    need, reason = (True, "forced") if force else decide(company_id, top_fragments)
    if not need:
        log(f"{company_id}: отчёт актуален")
        return last_report(company_id)["report"]
    log(f"{company_id}: генерация отчёта ({reason}); новостей в пакете {len(package['news'])}, "
        f"связей {len(package['connections'])}")
    document, trace = generate(package)
    document["refresh_reason"] = reason
    document["market_indicators"] = package.get("market_indicators") or []
    news.mark_important([n["id"] for n in package["news"]])
    ts = store.now()
    with store.connect() as con:
        con.execute("insert into reports (company_id, generated_at, reason, top_fragments, package, report, cost)"
                    " values (?,?,?,?,?,?,?)",
                    (company_id, ts, reason, store.dumps(top_fragments), store.dumps(package),
                     store.dumps(document), document["usage"]["cost"]))
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    (REPORTS_DIR / f"{company_id}.json").write_text(json.dumps(document, ensure_ascii=False, indent=2), encoding="utf-8")
    (REPORTS_DIR / f"{company_id}.md").write_text(to_markdown(document), encoding="utf-8")
    (REPORTS_DIR / f"{company_id}.trace.json").write_text(json.dumps(trace, ensure_ascii=False, indent=2), encoding="utf-8")
    log(f"{company_id}: готово, {document['usage']['cost']:.2f} ₽, {document['seconds']} с")
    return document


def get_report(company_id: str) -> dict | None:
    """Последний отчёт для фронтенда (без пересчёта)."""
    last = last_report(company_id)
    return last["report"] if last else None
