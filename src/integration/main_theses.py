"""Local comparison of the headline-aligned thesis, never a deletion decision.

This index is separate from news_context/company matching and news_mechanism.
Prepared fragments are not guaranteed to put the main event first, so retain
the selected fragment and alignment scores for inspection.
"""
from datetime import date
from decimal import Decimal
import re

import numpy as np


def cosine(a, b):
    a, b = np.asarray(a, dtype=np.float32), np.asarray(b, dtype=np.float32)
    if a.ndim != 1 or a.shape != b.shape or not np.isfinite(a).all() or not np.isfinite(b).all():
        raise ValueError('Invalid or incompatible thesis vectors')
    norms = float(np.linalg.norm(a) * np.linalg.norm(b))
    if not norms:
        raise ValueError('Empty thesis vector')
    return float(np.clip(a @ b / norms, -1, 1))


def main_thesis(title_vector, fragments, vectors):
    """Select an existing thesis by headline alignment; do not generate a summary."""
    candidates = []
    for fragment in fragments:
        text = (fragment.get('thesis') or '').strip()
        if not text:
            continue
        fid = fragment.get('chunk_id', fragment.get('id'))
        if fid not in vectors:
            raise ValueError('Missing thesis vector: '+str(fid))
        candidates.append((cosine(title_vector, vectors[fid]), str(fid), text))
    if not candidates:
        return None
    candidates.sort(key=lambda item: (-item[0], item[1]))
    score, fid, text = candidates[0]
    return dict(chunk_id=fid, text=text, title_alignment=score,
                selection_method='maximum_headline_alignment',
                alignment_warning=score<.55,
                alignment_margin=score-candidates[1][0] if len(candidates)>1 else None,
                alternatives=[dict(chunk_id=f, score=s) for s,f,t in candidates[1:]])


def numbers(text):
    """Conservative change warning; never claim amounts with different units equal."""
    return sorted({format(Decimal(m.replace(',', '.')), 'f').rstrip('0').rstrip('.')
                   if '.' in m.replace(',', '.') else str(int(m))
                   for m in re.findall(r'(?<![\w])\d+(?:[.,]\d+)?(?![\w])', text)})


def compare_main(a, b, title_vectors, thesis_vectors, *, min_thesis=.88,
                 min_title=.80, min_cross_alignment=.70, window_days=3):
    """Return evidence for event grouping. Ambiguity always preserves articles."""
    gap = abs((date.fromisoformat(a['date'])-date.fromisoformat(b['date'])).days)
    ta, tb = a['main'], b['main']
    if ta is None or tb is None:
        return dict(status='missing_main_thesis', day_gap=gap, suppress=False)
    score = cosine(thesis_vectors[ta['chunk_id']], thesis_vectors[tb['chunk_id']])
    title_score = cosine(title_vectors[a['id']], title_vectors[b['id']])
    cross = min(cosine(title_vectors[a['id']], thesis_vectors[tb['chunk_id']]),
                cosine(title_vectors[b['id']], thesis_vectors[ta['chunk_id']]))
    qualifies = (gap<=window_days and score>=min_thesis and title_score>=min_title
                 and cross>=min_cross_alignment)
    na, nb = numbers(ta['text']), numbers(tb['text'])
    changes = dict(only_a=sorted(set(na)-set(nb)), only_b=sorted(set(nb)-set(na)))
    changed = bool(changes['only_a'] or changes['only_b'])
    # A missed threshold is not proof of different events.
    status = 'below_candidate_threshold'
    if qualifies:
        status = 'possible_update' if changed or gap else 'possible_same_event'
    return dict(status=status, thesis_cosine=score, title_cosine=title_score,
                cross_title_alignment=cross, day_gap=gap, numeric_differences=changes,
                numeric_change_warning=changed, suppress=False)


def compare_main_with_context(a, b, title_vectors, thesis_vectors, context_vectors,
                              *, min_context=.90):
    """Exploratory profile: retain the main fragment's essential context.

    Uses only the selected main fragments, never a maximum over background
    fragments. All four thresholds were explored on the same corpus; this is
    candidate evidence, not a validated automatic suppression policy.
    """
    result=compare_main(a,b,title_vectors,thesis_vectors,
                        min_thesis=.70,min_title=.65,min_cross_alignment=.50)
    if not a['main'] or not b['main']:
        return result
    score=cosine(context_vectors[a['main']['chunk_id']],context_vectors[b['main']['chunk_id']])
    result['main_context_cosine']=score
    if score<min_context:
        result['status']='below_candidate_threshold'
    return result
