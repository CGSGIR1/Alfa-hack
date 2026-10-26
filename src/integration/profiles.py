"""Translate the team's CSV-based profiles into the retrieval input contract."""
import math
from news_analytics.validation import company as validate_company


def plain(value):
    """CSV/pandas scalars must remain portable JSON, with unknowns as null."""
    if isinstance(value, dict):
        return {str(k): plain(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [plain(v) for v in value]
    if hasattr(value, 'item'):
        return plain(value.item())
    if isinstance(value, float) and not math.isfinite(value):
        return None
    return value


def company_json(subject_id, csv_dir, refine=False, require_llm=False):
    from src.company_profile import DataStore, build_profile, build_group_profile, describe_full, describe_group
    from src.company_profile.queries import company_queries, group_queries
    # Pure fact helpers; get_company/get_store and the old pipeline DB are never called.
    from src.pipeline.companies import company_facts, group_facts, BASE_LIMITATIONS, CATEGORY_REFS
    store = DataStore(csv_dir)
    if subject_id.startswith('group_'):
        profile = build_group_profile(store, subject_id)
        facts = group_facts(profile)
        facts['members'] = [{'subject_id': str(i)} for i in store.group_members(subject_id).index]
        description, queries, subject_type = describe_group(profile), group_queries(profile), 'group'
    else:
        profile = build_profile(store, subject_id)
        facts = company_facts(profile)
        description, queries, subject_type = describe_full(profile), company_queries(profile), 'company'
    if refine:
        from src.company_profile.impact import get_impact_queries, is_fallback
        queries = get_impact_queries(store, subject_id)
        if require_llm and is_fallback(queries):
            raise ValueError('Не удалось подготовить профиль через LLM. Проверьте доступ к API и повторите добавление.')
    items = []
    for i, q in enumerate(queries.queries, 1):
        refs = [p for p in CATEGORY_REFS.get(q.category, ['/facts/industry']) if p.split('/')[2] in facts]
        items.append(dict(id=f'{subject_id}_Q{i:02d}', channel=q.channel, category=q.category, text=q.text,
            weight=q.weight, evidence_status='hypothesis' if q.origin == 'llm' and q.kind == 'llm' else 'grounded_topic',
            source_refs=refs, assumptions=[q.why] if q.kind == 'llm' else [], limitations=[],
            provenance=dict(origin=q.origin, original_sources=q.sources)))
    return validate_company(plain(dict(schema_version='company-profile-2.0', subject_id=subject_id,
        subject_type=subject_type, profile_text=description, facts=facts,
        limitations=BASE_LIMITATIONS + profile.data_notes, queries=items)))
