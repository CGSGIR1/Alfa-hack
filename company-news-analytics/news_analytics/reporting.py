from __future__ import annotations
from dataclasses import asdict
from datetime import datetime,timezone
from html import escape
from pathlib import Path
from uuid import uuid4
import csv,json,re
from .common import now,dumps,sha,write_json
from .database import connect,check
from .packet_selection import select_analytics
from .llm_io import report_input
from .analytics_sources import source_provenance


def source_label(item):
    return source_provenance(item.get('document_id'))['label'] or ''


def source_badge(item):
    label=source_label(item)
    return f'<span class="organizer-badge">{escape(label)}</span>' if label else ''

def markdown(report):
    compact,_=select_analytics(report)
    lines=[f"# Подбор для {report['company_id']}",'',report['company_profile'],'',
      f"Дата отбора: {report['as_of']}. Новостей: {report['totals']['selected_news']}; связей в пересечении: {report['totals']['intersection_links']}.",'',
      'Сходство не является вероятностью или доказательством влияния на компанию.','']
    for n in report['news']:
        lines += [f"## {n['title']}",'',f"{n['url']} · {n['publication_date']} · сходство с компанией: {n['company_score']}",'',
                  '### Почему выбрана новость','']
        for q in n['matching_queries']:lines += [f"- {q['channel']}: {q['query']} · cosine={q['cosine']} · weighted={q['weighted_score']}"]
        lines += ['','### Фрагменты новости','']
        for f in n['selected_fragments']:
            lines += [f"**{f['local_id']}: {f['title']}**",'',f['thesis'],'',f['context'],'',
                      '**Запрос к обоим индексам аналитики:**',f.get('analytics_embedding_text') or 'Механизм не выделен.','']
        lines += ['### Пересечение двух поисков','']
        if not n['analytics']:lines += ['Пересечение пустое. Ниже сохранены кандидаты каждой ветви; пустое пересечение не доказывает отсутствие связи.','']
        for a,_ in compact[n['news_id']]:
            lines += [f"#### {a['title']}",'']
            if source_label(a):lines += [f'**{source_label(a)}**','']
            lines += [f"Механизм: {a['mechanism_similarity']}; исходные фрагменты: "+', '.join(f"{s['source_chunk_id']} = {s['similarity']}" for s in a['source_similarities']),
                      '',a['mechanism']['embedding_text'],'',f"Источник: {a['document_title']} · {a['document_date'] or a['document_period']}",'']
            for source in a['source_fragments']:lines += [f"**{source['id']} · страницы {source['pages']}**",'',source['text'],'']
            lines += ['Ограничения:',*['- '+x for x in a['mechanism'].get('limitations',[])],'']
        lines += ['### Все кандидаты механизменного поиска','',
                  '| Механизм | Сходство | Есть в пересечении | Источник |','|---|---:|---|---|']
        for a in n['mechanism_candidates']:lines += [f"| {a['mechanism_id']} | {a['mechanism_similarity']} | {'да' if a['retrieval_agreement'] else 'нет'} | {source_label(a)} |"]
        lines += ['','### Кандидаты исходных аналитических фрагментов','',
                  '| Фрагмент | Сходство с тем же запросом новости | Источник |','|---|---:|---|']
        for s in n['source_candidates']:lines += [f"| {s['source_chunk_id']} | {s['similarity']} | {source_label(s)} |"]
        lines += ['']
    return '\n'.join(lines)+'\n'

def html_reports(reports,run_id):
    parts=['<!doctype html><html lang="ru"><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">',
      '<title>Сопоставление новостей и аналитики</title><style>body{font:16px/1.6 system-ui,sans-serif;max-width:1100px;margin:35px auto;padding:0 20px;color:#192a3a;background:#f5f7fa}article,section{background:white;border:1px solid #dbe3eb;border-radius:10px;padding:22px;margin:20px 0}h1,h2,h3{line-height:1.25}table{border-collapse:collapse;width:100%;font-size:14px}td,th{padding:8px;text-align:left;border-bottom:1px solid #ddd;vertical-align:top}pre{white-space:pre-wrap;overflow-wrap:anywhere;font:inherit}summary{cursor:pointer;color:#24488f}.score{font-weight:650;color:#24488f}.note{color:#526576}a{color:#24488f}</style>',
      '<style>.organizer-badge{display:inline-block;margin:5px 0;padding:3px 9px;border:1px solid #efb2ba;border-radius:6px;background:#fff1f3;color:#971c2b;font-size:13px;font-weight:600}</style>',
      '<h1>Компания → новости → аналитика</h1><p class="note">Отчёт о сравнении. Сходство не доказывает причинную связь или применимость. Итоговая LLM здесь не вызывается.</p>',f'<p>Запуск: {escape(run_id)}</p>']
    for report in reports:
        compact,_=select_analytics(report)
        parts += [f"<article><h2>{escape(report['company_id'])}</h2><p>{escape(report['company_profile'])}</p>",
                  f"<p>Дата: {report['as_of']} · Новостей: {report['totals']['selected_news']} · Связей в пересечении: {report['totals']['intersection_links']}</p>"]
        if not report['news']:parts += ['<p>Новостей выше порога в выбранном временном окне нет.</p>']
        for n in report['news']:
            parts += [f"<section><h3>{escape(n['title'])}</h3><p><a href=\"{escape(n['url'],quote=True)}\">Открыть источник</a> · {n['publication_date']} · <span class=\"score\">Компания: {n['company_score']}</span></p>",
                      '<details><summary>Основания подбора и исходная новость</summary>']
            for q in n['matching_queries']:parts += [f"<p>{escape(q['channel'])}: {escape(q['query'])} · cosine {q['cosine']}; с весом {q['weighted_score']}</p>"]
            for f in n['selected_fragments']:parts += [f"<h4>{escape(f['title'])}</h4><pre>{escape(f['thesis'])}\n\n{escape(f['context'])}</pre><p><b>Один запрос к двум индексам:</b></p><pre>{escape(f.get('analytics_embedding_text') or 'Механизм не выделен')}</pre>"]
            parts += ['</details><details><summary>Подробные кандидаты обеих ветвей</summary><h4>Кандидаты механизменного поиска</h4><table><tr><th>Механизм</th><th>Сходство</th><th>Пересечение</th></tr>']
            for a in n['mechanism_candidates']:
                parts += [f"<tr><td>{escape(a['title'])}<br>{source_badge(a)}<br><small>{escape(a['mechanism_id'])}</small></td><td>{a['mechanism_similarity']}</td><td>{'да' if a['retrieval_agreement'] else 'нет'}</td></tr>"]
            parts += ['</table><h4>Поиск по исходным аналитическим фрагментам</h4><table><tr><th>Фрагмент</th><th>Сходство с тем же запросом</th></tr>']
            for s in n['source_candidates']:parts += [f"<tr><td>{escape(s['title'])}<br>{source_badge(s)}<br><small>{escape(s['source_chunk_id'])}</small></td><td>{s['similarity']}</td></tr>"]
            parts += ['</table></details>']
            if not n['analytics']:parts += ['<p class="note">Пересечение пустое. Отдельные кандидаты сохранены для проверки.</p>']
            for a,_ in compact[n['news_id']]:
                parts += [f"<details><summary>Совпало в обоих поисках: {escape(a['title'])} {source_badge(a)}</summary><pre>{escape(a['mechanism']['embedding_text'])}</pre>",
                          f"<p>{escape(a['document_title'])} · {escape(a['document_date'] or a['document_period'] or '')}</p>"]
                for source in a['source_fragments']:parts += [f"<p><b>{escape(source['id'])}</b> · страницы {source['pages']}</p><pre>{escape(source['text'])}</pre>"]
                for limit in a['mechanism'].get('limitations',[]):parts += [f'<p class="note">{escape(limit)}</p>']
                parts += ['</details>']
            parts += ['</section>']
        parts += ['</article>']
    return ''.join(parts)+'</html>'

def report_name(company_id):
    return 'company_'+(company_id if re.fullmatch(r'[A-Za-z0-9_-]+',company_id) else sha(company_id)[:12])

def save(db,output,reports,settings,model,stages,on_save=None):
    run_id=datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')+'-'+uuid4().hex[:6]
    folder=Path(output)/run_id;folder.mkdir(parents=True)
    summary=dict(companies=len(reports),news=sum(r['totals']['selected_news'] for r in reports),
                 intersection_links=sum(r['totals']['intersection_links'] for r in reports))
    manifest=dict(run_id=run_id,created_at=now(),settings=asdict(settings),model=model,stages=stages,summary=summary,
                  report_files=[report_name(r['company_id'])+'.json' for r in reports],
                  llm_input_files=['llm-input-'+report_name(r['company_id']).removeprefix('company_')+'.json' for r in reports])
    for report in reports:
        report['run_id']=run_id;filename=report_name(report['company_id'])
        packet=report_input(report)
        report['presentation']={**packet['retrieval']['packet_selection'],
            'connections':[{k:c[k] for k in ('news_id','mechanism_id','news_fragment_id')} for c in packet['connections']]}
        write_json(folder/(filename+'.json'),report)
        write_json(folder/('llm-input-'+filename.removeprefix('company_')+'.json'),packet)
        (folder/(filename+'.md')).write_text(markdown(report),encoding='utf-8')
    summary.update(packet_connections=sum(r['presentation']['selected_connections'] for r in reports),
                   packet_unique_mechanisms=sum(r['presentation']['selected_unique_mechanisms'] for r in reports))
    rendered=html_reports(reports,run_id);(folder/'report.html').write_text(rendered,encoding='utf-8')
    with (folder/'comparisons.csv').open('w',encoding='utf-8-sig',newline='') as f:
        writer=csv.writer(f);writer.writerow(['company_id','news_id','news_title','company_score','mechanism_id','mechanism_similarity','source_similarities','intersection','source_origin','source_label'])
        for report in reports:
            for n in report['news']:
                for a in n['mechanism_candidates']:writer.writerow([report['company_id'],n['news_id'],n['title'],n['company_score'],a['mechanism_id'],a['mechanism_similarity'],dumps(a['source_similarities']),int(a['retrieval_agreement']),source_provenance(a['document_id'])['origin'],source_label(a)])
    write_json(folder/'run.json',manifest)
    # Snapshots remain interpretable after later imports; no mutable live joins are required.
    with connect(db) as c:
        with c:
            c.execute('INSERT INTO comparison_runs VALUES(?,?,?,?,?,?,?,?)',(run_id,manifest['created_at'],settings.as_of,dumps(asdict(settings)),dumps(model),'complete',dumps(stages),dumps(summary)))
            for report in reports:c.execute('INSERT INTO company_reports VALUES(?,?,?)',(run_id,report['company_id'],dumps(report)))
            if on_save:on_save(c,run_id,folder,summary)
            check(c)
    write_json(Path(output)/'latest.json',dict(run_id=run_id,directory=str(folder.resolve()),summary=summary))
    (Path(output)/'latest.html').write_text(rendered,encoding='utf-8')
    return dict(run_id=run_id,output=str(folder.resolve()),summary=summary)
