"""Explicit admission policy shared by new processing and the current feed.

Grounded topics and profile hypotheses use the configured admission thresholds.
This is a relevance gate, not an estimate of business impact or probability.
"""
WEAK_EVIDENCE = {'hypothesis', 'partial_payment_evidence'}


def admits(candidate, minimum=.345, weak_minimum=.345):
    score = candidate.get('_score', candidate.get('company_score'))
    if score is None:
        return True  # Historical packets without scores are not fabricated/re-scored.
    if score < minimum:
        return False
    matches = candidate.get('matching_queries', [])
    if not matches:
        return True
    grounded = max((m.get('weighted_score', 0) for m in matches
                    if (m.get('evidence') or {}).get('evidence_status') not in WEAK_EVIDENCE), default=0)
    return grounded >= minimum or score >= weak_minimum


def policy(config):
    minimum = config.get('retrieval', {}).get('min_news_score', .345)
    return dict(minimum=minimum,
                weak_minimum=config.get('admission', {}).get('min_weak_news_score', minimum))


def filter_admitted(data, company_id, candidates, **options):
    return [c for c in candidates if admits(c, **options)]
