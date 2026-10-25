"""Собирает web/index.html: подставляет в дизайн отчёты и данные компаний.

    python web/build.py                       # все отчёты из data/reports, данные компаний из CSV кейса
    python web/build.py --default U_10529     # какая компания открывается первой
    python web/build.py --data-dir D:\\case    # где лежат df_Uid_data_hack.csv и df_Uid_payment_hack.csv

Что попадает на страницу
- отчёты company-report-2.0 из data/reports/*.json (по одному, самому свежему, на компанию);
- в переключатель — только «наши» категории классификатора крупного бизнеса (src/big_business, выбор категорий в
  src/pipeline/config.py: CLASSIFIER); та же разметка, что у пользователей пайплайна; остальных на сайте нет;
- компания без отчёта открывается пустой страницей: «отчёт ещё не сформирован», никаких заглушек;
- профиль (24 поля) и топ платежей встраиваются только для компаний с отчётом.

Откуда данные компании
1. CSV кейса в --data-dir ($DATA_DIR или data/) — тогда считаются и выручка группы, и платежи;
2. если CSV нет — снимок data/companies/<id>.json (выручку группы проверить нельзя, об этом будет предупреждение).

Дизайн остаётся одним файлом: index.html открывается двойным щелчком, сервер не нужен.
Перед сборкой проверяется связность данных: отчёт ↔ компания ↔ новости ↔ рекомендации ↔ оценки.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import sys
from pathlib import Path

WEB = Path(__file__).resolve().parent
ROOT = WEB.parent
sys.path.insert(0, str(ROOT))

REPORTS_DIR = ROOT / "data" / "reports"
SNAPSHOTS_DIR = ROOT / "data" / "companies"
DEFAULT_DATA_DIR = ROOT / "data"
SCHEMA = "company-report-2.0"
TOP_PAYMENTS = 12                      # сколько контрагентов в каждую сторону хранит дизайн
MARKER = "/*COMPANY_DATA*/"
PROFILE_FIELDS = [
    "SYNTHETIC_U", "ORG_TYPE", "OKFS_NAME", "COMPANY_AGE_YEARS_GRADATION", "ECO_MARK_LABEL", "MAINOKVED_CODE",
    "MAINOKVED_NAME", "NEGATIVEMARK", "AMOUNT_WORKERS_FNS_GRADATION", "AMOUNT_WORKERS_SPARK_GRADATION", "CL_TYPE",
    "CL_PRODUCT_RKO_MARK", "CL_PRODUCT_GAR_MARK", "CL_PRODUCT_LOA_MARK", "CL_PRODUCT_LEA_MARK",
    "CL_PRODUCT_DEP_MARK", "CL_PRODUCT_ZPP_MARK", "FTS_FLAG", "FTS_COUNTRY_NAME", "FACTORING_MARK",
    "REVENUE_COMPANY", "GROUP_ID", "CITY", "REGION",
]

# Скрипт выбора компании: идёт первым, остальные блоки дизайна читают window.SELECTED.
# Компания без отчёта получает пустую страницу: ни демонстрационных, ни синтетических данных.
SELECT_JS = """
(function () {
  var FLAGS = ["CL_PRODUCT_RKO_MARK", "CL_PRODUCT_GAR_MARK", "CL_PRODUCT_LOA_MARK", "CL_PRODUCT_LEA_MARK",
               "CL_PRODUCT_DEP_MARK", "CL_PRODUCT_ZPP_MARK", "FTS_FLAG", "FACTORING_MARK"];
  window.COMPANY_LIST = window.COMPANY_LIST.map(function (r) {
    return { id: r[0], city: r[1], revenue: r[2], group_revenue: r[3], flags: r[4], has_report: !!window.COMPANY_DATA[r[0]] };
  });
  var known = {};
  window.COMPANY_LIST.forEach(function (c) { known[c.id] = c; });
  var id = null;
  try { id = new URLSearchParams(location.search).get("u"); } catch (e) {}
  if (!id) { try { id = localStorage.getItem("alfa-company"); } catch (e) {} }
  if (!known[id]) id = window.DEFAULT_COMPANY;
  window.SELECTED_ID = id;
  var sel = window.COMPANY_DATA[id];
  if (!sel) {
    var c = known[id], profile = { SYNTHETIC_U: id, CITY: c.city, REVENUE_COMPANY: c.revenue };
    FLAGS.forEach(function (f, i) { profile[f] = Number(c.flags.charAt(i)); });
    sel = {
      company: { profile: profile, payments: {} },
      report: { schema_version: "company-report-2.0", company_id: id, as_of: new Date().toISOString().slice(0, 10),
                items: [], independent_risks: [], key_recommendations: [], dismissed: [], missing_information: [],
                market_indicators: [], summary: null },
      empty: true
    };
  }
  window.SELECTED = sel;
})();
"""


# ---------- отчёты ----------

def find_reports(directory: Path) -> tuple[dict[str, tuple[Path, dict]], list[str]]:
    """Самый свежий отчёт нужной схемы по каждой компании."""
    best: dict[str, tuple[tuple, Path, dict]] = {}
    notes = []
    for path in sorted(directory.glob("*.json")):
        if path.name.endswith(".trace.json"):
            continue
        try:
            report = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as error:
            notes.append(f"{path.name}: не читается ({error})")
            continue
        if not isinstance(report, dict) or "company_id" not in report:
            continue
        if report.get("schema_version") != SCHEMA:
            notes.append(f"{path.name}: схема {report.get('schema_version')!r}, дизайн ждёт {SCHEMA!r} — пропущен")
            continue
        key = (report.get("as_of") or "", path.stat().st_mtime)
        cid = report["company_id"]
        if cid not in best or key > best[cid][0]:
            best[cid] = (key, path, report)
    return {cid: (path, report) for cid, (_, path, report) in best.items()}, notes


def check(report: dict) -> list[str]:
    """Связность отчёта: рекомендации, оценки, источники."""
    problems = []
    items = report.get("items") or []
    risks = report.get("independent_risks") or []
    ids = [i.get("news_id") for i in items]
    if len(ids) != len(set(ids)):
        problems.append("news_id в items повторяются")
    known = set(ids) | {r.get("mechanism_id") for r in risks}
    for k, rec in enumerate(report.get("key_recommendations") or []):
        if rec.get("source_id") not in known:
            problems.append(f"key_recommendations[{k}].source_id={rec.get('source_id')} нет среди новостей и рисков")
    assessed = {a.get("news_id") for a in (report.get("assessment") or {}).get("news_assessments") or []}
    for nid in ids:
        if assessed and nid not in assessed:
            problems.append(f"у новости {nid} нет оценки применимости в assessment")
    for it in items:
        if not it.get("source_url"):
            problems.append(f"у новости {it.get('news_id')} нет source_url")
        if not it.get("published_at"):
            problems.append(f"у новости {it.get('news_id')} нет published_at")
    return problems


# ---------- данные компаний ----------

def clean(value):
    """NaN и numpy-типы → обычные значения JSON."""
    if value is None:
        return None
    if hasattr(value, "item"):
        value = value.item()
    if isinstance(value, float) and math.isnan(value):
        return None
    return value


class CaseData:
    """Компании и платежи из CSV кейса (через DataStore проекта)."""

    def __init__(self, data_dir: Path):
        from src.company_profile.data import DataStore
        self.store = DataStore(data_dir)

    def group_revenue(self, company_id: str) -> float | None:
        c = self.store.companies
        return float(c.at[company_id, "group_revenue"]) if company_id in c.index else None

    def company(self, company_id: str) -> dict | None:
        store = self.store
        c = store.companies
        if company_id not in c.index:
            return None
        row = c.loc[company_id]
        profile = {k: clean(row[k]) for k in PROFILE_FIELDS}
        group_id = profile["GROUP_ID"]

        def side(frame, other, direction):
            total = float(frame["PAY_AMT"].sum()) if len(frame) else 0.0
            rows = []
            for _, r in frame.head(TOP_PAYMENTS).iterrows():
                cid = r[other]
                known = cid in c.index
                rows.append({
                    "direction": direction, "counterparty": cid, "amount": float(r["PAY_AMT"]),
                    "count": int(r["PAY_CNT"]),
                    "okved": clean(c.at[cid, "MAINOKVED_CODE"]) if known else None,
                    "okved_name": clean(c.at[cid, "MAINOKVED_NAME"]) if known else None,
                    "own": bool(known and group_id is not None and c.at[cid, "GROUP_ID"] == group_id),
                })
            return rows, total, int(frame["PAY_CNT"].sum()) if len(frame) else 0, len(frame)

        inc, inc_total, inc_cnt, _ = side(store.incoming(company_id), "PAYER", "in")
        out, out_total, out_cnt, out_n = side(store.outgoing(company_id), "BENEFICIARY", "out")
        return {"profile": profile, "payments": {
            "incoming": inc, "outgoing": out, "outgoing_total": out_total, "outgoing_cnt": out_cnt,
            "outgoing_n": out_n, "incoming_total": inc_total, "incoming_cnt": inc_cnt}}


def load_company(company_id: str, case: CaseData | None, warnings: list[str]) -> tuple[dict | None, float | None]:
    """(данные компании, выручка её группы). Сначала CSV, затем снимок."""
    if case is not None:
        data = case.company(company_id)
        if data is not None:
            return data, case.group_revenue(company_id)
        warnings.append(f"{company_id}: нет в df_Uid_data_hack.csv")
    snapshot = SNAPSHOTS_DIR / f"{company_id}.json"
    if snapshot.exists():
        data = json.loads(snapshot.read_text(encoding="utf-8"))
        if case is None:
            warnings.append(f"{company_id}: взят снимок {snapshot.name}, выручка группы не проверена")
        return data, None
    return None, None


# ---------- сборка ----------

FLAG_COLUMNS = ["CL_PRODUCT_RKO_MARK", "CL_PRODUCT_GAR_MARK", "CL_PRODUCT_LOA_MARK", "CL_PRODUCT_LEA_MARK",
                "CL_PRODUCT_DEP_MARK", "CL_PRODUCT_ZPP_MARK", "FTS_FLAG", "FACTORING_MARK"]


def has_csv(data_dir: Path) -> bool:
    return any((data_dir / n).exists() for n in ("df_Uid_data_hack.csv", "df_Uid_data_hack.csv.gz"))


def eligible_rows(case: "CaseData") -> list[list]:
    """Все компании крупных групп: [id, город, выручка, выручка группы, флаги продуктов 0/1]."""
    from src.pipeline import users
    table = users.eligible_table(case.store)
    rows = []
    for cid, r in table.iterrows():
        flags = "".join("1" if clean(r[f]) == 1 else "0" for f in FLAG_COLUMNS)
        rows.append([cid, clean(r["CITY"]), clean(r["REVENUE_COMPANY"]),
                     clean(r["group_revenue"]) or clean(r["REVENUE_COMPANY"]), flags])
    return rows


def build(out: Path, default: str, data_dir: Path, strict: bool = True) -> int:
    reports, notes = find_reports(REPORTS_DIR)
    warnings = list(notes)
    skipped = []
    case = CaseData(data_dir) if has_csv(data_dir) else None

    if case is not None:
        listing = {row[0]: row for row in eligible_rows(case)}
    else:
        warnings.append(f"нет df_Uid_data_hack.csv(.gz) в {data_dir} — в переключателе только компании с отчётом и снимком "
                        "data/companies/, крупность группы не проверена")
        listing = {}

    data = {}
    for cid, (path, report) in sorted(reports.items()):
        if case is not None and cid not in listing:
            skipped.append(f"{cid}: не в «наших» категориях классификатора (или нет в данных) — отчёт не показываем")
            continue
        problems = check(report)
        company, group_revenue = load_company(cid, case, warnings)
        if company is None:
            skipped.append(f"{cid}: нет данных компании (ни в CSV, ни в data/companies/)")
            continue
        profile = company["profile"]
        if profile.get("SYNTHETIC_U") != cid:
            problems.append(f"в данных компании {profile.get('SYNTHETIC_U')}, а отчёт по {cid}")
        if problems:
            for p in problems:
                print(f"  ! {cid}: {p}", file=sys.stderr)
            if strict:
                skipped.append(f"{cid}: {len(problems)} нарушений связности — страница будет пустой (--no-strict, чтобы включить)")
                continue
        data[cid] = {"company": company, "report": report}
        if case is None:
            flags = "".join("1" if clean(profile.get(f)) == 1 else "0" for f in FLAG_COLUMNS)
            listing[cid] = [cid, profile.get("CITY"), profile.get("REVENUE_COMPANY"), group_revenue, flags]

    for line in warnings:
        print("  · " + line, file=sys.stderr)
    for line in skipped:
        print("  - " + line, file=sys.stderr)
    if not listing:
        sys.exit("Нечего собирать: нет ни одной подходящей компании")

    # сначала компании с отчётом, дальше по убыванию выручки группы
    order = sorted(listing, key=lambda c: (c not in data, -(listing[c][3] or 0), c))
    default = default if default in listing else order[0]
    meta = [listing[c] for c in order]

    html = (WEB / "index.template.html").read_text(encoding="utf-8")
    if html.count(MARKER) != 1:
        sys.exit(f"В шаблоне должен быть ровно один маркер {MARKER}")
    dump = lambda o, **kw: json.dumps(o, ensure_ascii=False, **kw).replace("</", "<\\/")  # </script> в тексте закрыл бы тег
    block = (f"window.COMPANY_LIST = {dump(meta, separators=(',', ':'))};\n"
             f"window.COMPANY_DATA = {dump(data, indent=1)};\n"
             f"window.DEFAULT_COMPANY = {json.dumps(default)};\n{SELECT_JS}")
    out.write_text(html.replace(MARKER, block), encoding="utf-8")
    print(f"{out.name}: в переключателе {len(listing)} компаний, с отчётом {len(data)} ({', '.join(sorted(data))}), "
          f"первая — {default}; {out.stat().st_size / 1e6:.1f} МБ")
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--out", type=Path, default=WEB / "index.html")
    parser.add_argument("--default", default="U_10529")
    parser.add_argument("--data-dir", type=Path, default=Path(os.environ.get("DATA_DIR") or DEFAULT_DATA_DIR))
    parser.add_argument("--no-strict", action="store_true")
    args = parser.parse_args()
    sys.exit(build(args.out, args.default, args.data_dir, strict=not args.no_strict))


if __name__ == "__main__":
    main()
