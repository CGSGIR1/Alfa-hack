"""Шаг A: подготовка входного пакета report-generation-input-1.0 кодом, без LLM.

- индекс всех ID, на которые модели разрешено ссылаться;
- тип источника аналитики: Альфа-Банк (может быть «позицией банка») или третья сторона;
- возраст аналитики относительно as_of;
- допустимые ссылки на факты компании (JSON-указатели вида /facts/foreign_trade/countries);
- компактное представление для модели без шума: путей к файлам, хешей и баллов сходства
  (инструкция запрещает использовать сходство как вероятность — лучше их не показывать).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

SUPPORTED_SCHEMA = "report-generation-input-1.0"
BANK_PUBLISHERS = ("альфа-банк", "alfa-bank", "альфа банк")
STALE_DAYS = 180


@dataclass
class PackageIndex:
    company_id: str
    as_of: str = ""
    news: dict[str, dict] = field(default_factory=dict)            # news_id -> новость
    fragments: dict[str, dict] = field(default_factory=dict)       # fragment_id -> фрагмент новости
    fragment_news: dict[str, str] = field(default_factory=dict)    # fragment_id -> news_id
    mechanisms: dict[str, dict] = field(default_factory=dict)      # mechanism_id -> механизм
    sources: dict[str, dict] = field(default_factory=dict)         # source_fragment_id -> фрагмент аналитики
    documents: dict[str, dict] = field(default_factory=dict)       # document_id -> документ
    links: dict[str, dict] = field(default_factory=dict)           # link_id -> связь
    aspects: dict[str, dict] = field(default_factory=dict)         # aspect_id -> аспект компании
    basis_paths: list[str] = field(default_factory=list)           # допустимые /facts/... указатели
    indicators: dict[str, dict] = field(default_factory=dict)      # код показателя -> значение из market_indicators

    def all_ids(self) -> set[str]:
        return (set(self.news) | set(self.fragments) | set(self.mechanisms) | set(self.sources)
                | set(self.documents) | set(self.links) | set(self.aspects) | set(self.indicators))

    def source_kind_of(self, ref: str) -> str | None:
        """bank | third_party для механизма, фрагмента или документа аналитики."""
        doc_id = (self.mechanisms.get(ref) or self.sources.get(ref) or {}).get("document_id") or (
            ref if ref in self.documents else None)
        return self.documents[doc_id]["source_kind"] if doc_id in self.documents else None


def _pointer_paths(obj, prefix: str = "") -> list[str]:
    paths = [prefix] if prefix else []
    if isinstance(obj, dict):
        for key, value in obj.items():
            paths += _pointer_paths(value, f"{prefix}/{key}")
    return paths


def resolve_pointer(facts: dict, pointer: str) -> bool:
    """Существует ли путь /facts/... в фактах компании."""
    parts = [p for p in pointer.strip("/").split("/") if p]
    if not parts or parts[0] != "facts":
        return False
    node = facts
    for part in parts[1:]:
        if not isinstance(node, dict) or part not in node:
            return False
        node = node[part]
    return True


def normalize_pointer(facts: dict, pointer: str) -> str | None:
    """Существующий путь к факту; модели иногда опускают префикс /facts — достраиваем его."""
    if not isinstance(pointer, str):
        return None
    candidates = [pointer] if pointer.startswith("/facts") else [pointer, "/facts" + (pointer if pointer.startswith("/") else "/" + pointer)]
    return next((c for c in candidates if resolve_pointer(facts, c)), None)


def _source_kind(publisher: str | None) -> str:
    return "bank" if publisher and publisher.strip().lower() in BANK_PUBLISHERS else "third_party"


def _age_days(as_of: str, published: str | None) -> int | None:
    try:
        return (date.fromisoformat(as_of) - date.fromisoformat(published)).days
    except (TypeError, ValueError):
        return None


def build(package: dict) -> tuple[dict, PackageIndex]:
    """Возвращает (представление для модели, индекс ID)."""
    if package.get("schema_version") != SUPPORTED_SCHEMA:
        raise ValueError(f"Ожидается {SUPPORTED_SCHEMA}, получено {package.get('schema_version')}")
    as_of = package["as_of"]
    company = package["company"]
    index = PackageIndex(company_id=company["id"], as_of=as_of)
    index.basis_paths = _pointer_paths(company.get("facts") or {}, "/facts")
    index.indicators = {i["code"]: i for i in package.get("market_indicators") or [] if i.get("code")}

    for doc in package.get("analytical_documents") or []:
        index.documents[doc["id"]] = {
            "id": doc["id"],
            "title": doc.get("title"),
            "publisher": doc.get("publisher"),
            "publication_date": doc.get("publication_date"),
            "source_kind": _source_kind(doc.get("publisher")),
            "age_days": _age_days(as_of, doc.get("publication_date")),
        }
        age = index.documents[doc["id"]]["age_days"]
        index.documents[doc["id"]]["stale"] = age is not None and age > STALE_DAYS

    for src in package.get("analytical_source_fragments") or []:
        index.sources[src["id"]] = {
            "id": src["id"],
            "document_id": src["document_id"],
            "title": src.get("title"),
            "pages": src.get("pages") or [],
            "text": src.get("text"),
            "ocr_note": src.get("review_status"),
        }

    for m in package.get("analytical_mechanisms") or []:
        mech = m.get("mechanism") or {}
        index.mechanisms[m["id"]] = {
            "id": m["id"],
            "document_id": m.get("document_id"),
            "title": m.get("title"),
            "cause": mech.get("cause"),
            "channel": mech.get("channel"),
            "effect": mech.get("effect"),
            "conditions": (m.get("context") or {}).get("conditions"),
            "sectors": (m.get("context") or {}).get("sectors"),
            "observed_at": (m.get("context") or {}).get("observed_at"),
            "statement_type": m.get("statement_type"),
            "source_fragment_ids": m.get("source_fragment_ids") or [],
            "limitations": m.get("limitations") or [],
        }

    news_view = []
    for n in package.get("news") or []:
        index.news[n["id"]] = n
        matched = []
        for a in (n.get("company_match") or {}).get("aspects") or []:
            index.aspects.setdefault(a["id"], {
                "id": a["id"], "channel": a.get("channel"), "text": a.get("text"),
                "evidence_status": a.get("evidence_status"), "assumptions": a.get("assumptions") or [],
                "limitations": a.get("limitations") or [], "company_refs": a.get("original_company_source_refs") or [],
            })
            if a["id"] not in matched:
                matched.append(a["id"])
        fragments = []
        for f in n.get("fragments") or []:
            index.fragments[f["id"]] = f
            index.fragment_news[f["id"]] = n["id"]
            mech = f.get("mechanism") or {}
            ctx = mech.get("context") or {}
            fragments.append({
                "id": f["id"],
                "title": f.get("title"),
                "thesis": f.get("thesis"),
                "context": f.get("context"),
                "mechanism": {
                    "status": mech.get("status"),
                    "description": mech.get("description"),
                    "conditions": ctx.get("conditions"),
                    "event_period": ctx.get("event_period"),
                    "boundaries": mech.get("boundaries") or [],
                    "unknown_company_exposure": ctx.get("unknown_company_exposure"),
                },
                "attributions": [a.get("author") for a in f.get("attributions") or [] if a.get("author")],
                "limitations": f.get("limitations") or [],
            })
        news_view.append({
            "id": n["id"],
            "title": n.get("title"),
            "publication_date": n.get("publication_date"),
            "publisher": (n.get("source") or {}).get("publisher"),
            "limitations": n.get("limitations") or [],
            "matched_aspect_ids": matched,
            **({"indicator": n["indicator"]} if n.get("indicator") else {}),
            "fragments": fragments,
        })

    for link in package.get("connections") or []:
        index.links[link["id"]] = link

    view = {
        "as_of": as_of,
        "company": {
            "id": company["id"],
            "subject_type": company.get("subject_type"),
            "profile": company.get("profile"),
            "facts": company.get("facts"),
            "limitations": company.get("limitations") or [],
            "allowed_basis_paths": index.basis_paths,
        },
        "company_aspects": list(index.aspects.values()),
        "news": news_view,
        "analytics_documents": list(index.documents.values()),
        "analytics_source_fragments": list(index.sources.values()),
        "analytics_mechanisms": list(index.mechanisms.values()),
        "market_indicators": package.get("market_indicators") or [],
        "links": [
            {"id": l["id"], "news_id": l["news_id"], "news_fragment_id": l["news_fragment_id"],
             "mechanism_id": l["mechanism_id"], "selection_status": l.get("selection_status")}
            for l in index.links.values()
        ],
    }
    return view, index
