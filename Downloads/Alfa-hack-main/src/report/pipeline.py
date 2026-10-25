"""Генерация отчёта для компании из пакета report-generation-input-1.0.

A. подготовка пакета кодом            (package.py)
B. оценка применимости — LLM с рассуждениями   (assess.py)
C. проверка оценки кодом, 1 дозапрос при нарушениях
D. написание отчёта — LLM без рассуждений, только по принятому   (write.py)
E. проверка отчёта кодом + критик, 1 доработка при замечаниях     (verify.py)

Каждый шаг пишется в трейс: что модель вернула, что исправил код, сколько стоил вызов.
"""

from __future__ import annotations

import json
import time

from ..llm import LLMClient, LLMError
from . import assess, verify, write
from .package import build

SCHEMA_VERSION = "company-report-1.0"
ASSESS_PARAMS = {"max_tokens": 32000, "temperature": 0.1}
WRITE_PARAMS = {"max_tokens": 12000, "temperature": 0.2, "reasoning": {"enabled": False}}
CRITIC_PARAMS = {"max_tokens": 4000, "temperature": 0.0}


def generate(package: dict, client: LLMClient | None = None, critic: bool = True) -> tuple[dict, dict]:
    """Возвращает (отчёт, трейс)."""
    client = client or LLMClient()
    main_model = client.model("report", fallback="main")
    critic_model = client.model("critic", fallback="fast")
    started, usage_before = time.monotonic(), dict(client.usage)
    trace: dict = {"calls": [], "steps": {}}

    def call(model: str, messages: list[dict], task: str, params: dict) -> dict:
        before, t = dict(client.usage), time.monotonic()
        result = client.chat_json(model, messages, task=task, params=params)
        trace["calls"].append({"task": task, "model": model, "seconds": round(time.monotonic() - t, 1),
                               **{k: round(client.usage[k] - before[k], 4)
                                  for k in ("prompt_tokens", "completion_tokens", "cost")}})
        return result

    # A
    view, index = build(package)
    facts = package["company"].get("facts") or {}
    # числа из блока показателей тоже законный источник для проверки чисел в отчёте
    profile = (package["company"].get("profile") or "") + " " + json.dumps(
        package.get("market_indicators") or [], ensure_ascii=False)
    trace["steps"]["A"] = {"news": len(index.news), "links": len(index.links), "mechanisms": len(index.mechanisms),
                           "documents": {d: (v["source_kind"], v["age_days"]) for d, v in index.documents.items()}}

    # B + C
    base = assess.messages(view, package.get("generation_instructions") or [])
    raw = call(main_model, base, "report_assess", ASSESS_PARAMS)
    assessment, problems, fixes = assess.check(raw, index, facts)
    trace["steps"]["B"] = {"raw": raw, "problems": problems, "auto_fixes": fixes}
    if problems:
        raw = call(main_model, assess.repair_messages(base, raw, problems), "report_assess_repair", ASSESS_PARAMS)
        assessment, problems, more_fixes = assess.check(raw, index, facts)
        fixes += more_fixes
        trace["steps"]["C"] = {"raw": raw, "problems_after_repair": problems, "auto_fixes": more_fixes}

    # D
    data = write.writer_input(view, index, assessment)
    dismissed = write.dismissed(view, assessment)
    if data["accepted_news"] or data["independent_links"]:
        writer_messages = write.messages(data)
        report = call(main_model, writer_messages, "report_write", WRITE_PARAMS)
    else:
        writer_messages = []
        report = {"summary": "За период в отобранных материалах не найдено событий с установленным или условным "
                             "влиянием на компанию. Отброшенные материалы и причины приведены ниже.",
                  "items": [], "independent_risks": []}
    trace["steps"]["D"] = {"accepted_news": [n["id"] for n in data["accepted_news"]],
                           "independent_links": [l["id"] for l in data["independent_links"]]}

    # E
    code_issues = verify.check_report(report, index, assessment, facts, profile)
    critic_issues = []
    if critic and writer_messages:
        try:
            critic_issues = call(critic_model, verify.critic_messages(report, data), "report_critic",
                                 CRITIC_PARAMS).get("issues") or []
        except LLMError as error:
            trace["steps"]["E_critic_error"] = str(error)
    trace["steps"]["E"] = {"code_issues": list(code_issues), "critic_issues": critic_issues}
    remaining = code_issues
    if writer_messages and (code_issues or critic_issues):
        issues = code_issues + [f"{c.get('location')}: [{c.get('type')}] {c.get('problem')} — {c.get('suggestion')}"
                                for c in critic_issues if isinstance(c, dict)]
        report = call(main_model, verify.revise_messages(writer_messages, report, issues), "report_revise", WRITE_PARAMS)
        remaining = verify.check_report(report, index, assessment, facts, profile)
        trace["steps"]["E_revision"] = {"remaining_issues": remaining}

    document = {
        "schema_version": SCHEMA_VERSION,
        "request_id": package.get("request_id"),
        "company_id": index.company_id,
        "as_of": package.get("as_of"),
        "summary": report.get("summary"),
        "items": report.get("items") or [],
        "independent_risks": report.get("independent_risks") or [],
        "dismissed": dismissed,
        "missing_information": assessment.get("missing_information") or [],
        "assessment": assessment,
        "quality": {
            "assessment_auto_fixes": fixes,
            "assessment_unresolved": problems,
            "report_issues_found": len(code_issues) + len(critic_issues),
            "report_issues_remaining": remaining,
        },
        "models": {"main": main_model, "critic": critic_model},
        "usage": {k: round(client.usage[k] - usage_before[k], 4) for k in client.usage},
        "seconds": round(time.monotonic() - started, 1),
    }
    return document, trace
