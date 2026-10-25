"""Bound presentation volume without narrowing either retrieval branch."""
from collections import defaultdict
from .common import require


def select_analytics(report, include_candidates=False, per_news=2, total=5):
    for value in (per_news, total):
        require(type(value) is int and value > 0, 'Packet limits must be positive integers')
    result={n['news_id']:[] for n in report['news']}; pools=[]; available=0
    for n in report['news']:
        accepted={(a['mechanism_id'],a['news_chunk_id']) for a in n['analytics']}
        rows=list(n['analytics'])
        if include_candidates:
            rows += [a for a in n['mechanism_candidates'] if (a['mechanism_id'],a['news_chunk_id']) not in accepted]
        seen=set(); pool=[]
        for a in rows:
            key=(a['mechanism_id'],a['news_chunk_id'])
            if key in seen:continue
            seen.add(key);pool.append((a,key in accepted))
        available+=len(pool);pools.append((n['news_id'],pool))
    unique=set();used_sources=set();chains=defaultdict(set)
    # Intersections first, round-robin across news to avoid starving later events.
    # New source support is a diversity heuristic, never a test of applicability.
    for agreement in (True,False) if include_candidates else (True,):
        for _ in range(per_news):
            for nid,pool in pools:
                if len(result[nid])>=per_news:continue
                eligible=[]
                for a,ok in pool:
                    mid=a['mechanism_id']
                    if ok!=agreement or any(x[0]['mechanism_id']==mid for x in result[nid]):continue
                    if mid not in unique and len(unique)>=total:continue
                    body=a['mechanism']['mechanism']
                    chain=tuple(str(body.get(k,'')) for k in ('cause','channel','effect'))
                    if chain in chains[nid]:continue
                    refs=set(a['source_fragment_ids']);novel=bool(refs-used_sources)
                    # Unconfirmed extras must add different source support.
                    if not agreement and not novel:continue
                    score=min([a['mechanism_similarity']]+[s['similarity'] for s in a.get('source_similarities',[])])
                    eligible.append((-score,not novel,a['mechanism_id'],a['news_chunk_id'],a,chain))
                if not eligible:continue
                _,_,_,_,chosen,chain=min(eligible,key=lambda x:x[:4])
                result[nid].append((chosen,agreement));unique.add(chosen['mechanism_id'])
                used_sources.update(chosen['source_fragment_ids']);chains[nid].add(chain)
    selected=sum(len(rows) for rows in result.values())
    return result,dict(per_news=per_news,total_unique_mechanisms=total,available_connections=available,
        selected_connections=selected,selected_unique_mechanisms=len(unique),omitted_connections=available-selected,
        policy='intersection_first_round_robin_min_branch_score_then_source_diversity; similarity does not assess applicability')
