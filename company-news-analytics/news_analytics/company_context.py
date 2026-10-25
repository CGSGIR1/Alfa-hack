"""Compact existing payment aggregates; never infer commercial roles or recalculate CSV."""
import copy


def counterparty_summary(profile, top=5):
    payments=profile.get('relationships',{}).get('payments',{})
    summaries=payments.get('summaries',{})
    if not summaries:
        return dict(available=False,reason='Payment industry aggregates not provided for this profile')
    out=dict(available=True,classification='okved_division',top_per_direction=top,
        currency=payments.get('currency'),amount_unit=payments.get('amount_unit'),
        period_start=payments.get('period_start'),period_end=payments.get('period_end'),
        direction_rules=copy.deepcopy(payments.get('direction_rules',{})),
        amount_share_denominator=payments.get('amount_share_denominator'),
        count_share_denominator=payments.get('count_share_denominator'),
        counterparty_roles=payments.get('counterparty_roles') or 'Payer is not necessarily a customer; beneficiary is not necessarily a supplier.',
        same_group_rule=payments.get('same_group_rule'),selections={})
    for scope in ('all_observed','excluding_same_group'):
        if scope not in summaries:continue
        out['selections'][scope]={}
        for direction in ('incoming','outgoing'):
            if direction not in summaries[scope]:continue
            d=summaries[scope][direction];all_rows=d.get('aggregates',{}).get('okved_division',[])
            rows=[r for r in all_rows if r.get('key') not in (None,'(unknown)')]
            rows.sort(key=lambda r:(-float(r.get('share_amount_pct') or 0),str(r['key'])))
            out['selections'][scope][direction]=dict(
                source_ref='/relationships/payments/summaries/'+scope+'/'+direction,
                source_rows=d.get('source_rows'),unique_counterparties=d.get('unique_counterparties'),
                pay_amt_total=d.get('pay_amt_total'),pay_cnt_total=d.get('pay_cnt_total'),
                coverage=copy.deepcopy(d.get('coverage',{}).get('okved_division',{})),
                top_industries=[{k:copy.deepcopy(v) for k,v in r.items() if k in
                    ('key','pay_amt','pay_cnt','source_rows','share_amount_pct','share_count_pct',
                     'share_amount_among_known_pct','share_count_among_known_pct')} for r in rows[:top]],
                omitted_known_industry_count=max(0,len(rows)-top),
                omitted_known_share_amount_pct=sum(float(r.get('share_amount_pct') or 0) for r in rows[top:]))
    return out
