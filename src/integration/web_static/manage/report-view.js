'use strict';
/* Report presentation only. Retrieval scores never become impact/confidence labels. */
window.ReportView = (() => {
  let sequence = 0;
  const el = (tag, text, cls) => {
    const n = document.createElement(tag);
    if (text !== undefined && text !== null) n.textContent = String(text);
    if (cls) n.className = cls;
    return n;
  };
  const unique = values => [...new Set(values.filter(Boolean))];
  const textOf = value => typeof value === 'string' ? value : value?.text || '';
  const labels = {applicable:'Применимо к компании', conditional:'Условно применимо', background:'Только фон', rejected:'Связь отклонена'};
  const list = values => {
    const ul = el('ul', null, 'rv-list');
    for (const value of values || []) if (textOf(value)) ul.append(el('li', textOf(value)));
    return ul;
  };
  function disclosure(title, content, cls='rv-disclosure') {
    const d = el('details', null, cls);
    d.append(el('summary', title));
    if (content) d.append(content);
    return d;
  }
  function paragraph(text, cls='rv-text') {return el('p', text, cls);}
  function dateLabel(value) {
    if (!value) return 'Дата не указана';
    const d = new Date(value.length === 10 ? value+'T12:00:00+03:00' : value);
    const format=value.length === 7 ? {month:'long',year:'numeric'} : {day:'numeric',month:'long',year:'numeric'};
    if(value.length > 10)Object.assign(format,{hour:'2-digit',minute:'2-digit'});
    format.timeZone='Europe/Moscow';
    return Number.isNaN(d.valueOf()) ? value : new Intl.DateTimeFormat('ru-RU', format).format(d);
  }
  function sourceLink(source) {
    try {
      const u = new URL(source?.url);
      if (!['http:', 'https:'].includes(u.protocol)) return null;
      const a = el('a', source.publisher || 'Открыть публикацию', 'rv-source-link');
      a.href = u.href; a.target = '_blank'; a.rel = 'noopener noreferrer';
      return a;
    } catch {return null;}
  }
  function claims(target, title, values, cls='', compact=false) {
    if (!values?.length) return;
    const box = el('section', null, 'rv-section '+cls);
    box.append(el('h4', title));
    const extra = el('div');
    for (const [i,claim] of values.entries()) {
      const row = el('div', null, 'rv-claim');
      const type = {news_fact:'Факт из новости',ai_inference:'Вывод ИИ',bank_view:'Из аналитики Альфа-Банка',third_party_view:'Из внешней аналитики'}[claim.type];
      if (type) row.append(el('span', type, 'rv-claim-type'));
      row.append(paragraph(textOf(claim)));
      (compact && i > 0 ? extra : box).append(row);
    }
    if (extra.childNodes.length) box.append(disclosure('Дополнительные выводы · '+extra.childNodes.length,extra));
    target.append(box);
  }
  function section(target, title, values) {
    if (!values?.length) return;
    const box = el('section', null, 'rv-section');
    box.append(el('h4', title), list(values)); target.append(box);
  }
  function narrative(item, result) {
    return item.narrative || result.narrative?.items?.find(n => n.news_id === item.news_id) || {};
  }
  function conditions(item, newsConditions=[]) {
    if(newsConditions.length)return unique(newsConditions);
    return unique((item.assessments || [])
      .filter(a => ['applicable','conditional'].includes(a.news_mechanism_fit))
      .flatMap(a => a.company_impact?.required_conditions || []));
  }
  function overview(panel, item, prose, newsConditions) {
    if(prose.brief?.takeaway)panel.append(paragraph(prose.brief.takeaway,'rv-summary-text'));
    if(prose.brief?.recommendation)section(panel,'Рекомендация',[prose.brief.recommendation]);
    const impact=prose.impact,levels={weak:'слабое',low:'слабое',medium:'среднее',strong:'сильное',high:'сильное'};
    if(impact && levels[impact.level]) {
      const box=el('section',null,'rv-relevance');
      box.append(el('span','Сила влияния'),paragraph(levels[impact.level]+(impact.conditional ? ' · при выполнении условий' : '')));
      if(impact.explanation)box.append(paragraph(impact.explanation));
      const factors={exposure:'Связь с компанией',magnitude:'Масштаб',certainty:'Статус события',transmission:'Канал влияния',mitigation:'Компенсирующие факторы'};
      box.append(disclosure('Основания оценки',list(Object.entries(impact.rubric || {}).map(([key,value])=>
        (factors[key] || key)+': '+value.score+' — '+(value.note || 'Пояснение не указано')))));
      panel.append(box);
    }
    if (prose.why_you_see_this) {
      const why = el('div', null, 'rv-relevance');
      why.append(el('span', 'Почему вы видите эту новость'), paragraph(prose.why_you_see_this)); panel.append(why);
    }
    claims(panel, 'Значение для компании', prose.why_it_matters, '', true);
    if (!prose.why_it_matters?.length && !prose.brief?.takeaway) panel.append(paragraph('В итоговом отчёте нет дополнительного пояснения влияния.'));
    const effects = el('div', null, 'rv-effects');
    claims(effects, 'Возможные риски', prose.risks, 'rv-risk', true);
    claims(effects, 'Возможности', prose.opportunities, 'rv-opportunity', true);
    if (effects.childNodes.length) panel.append(effects);
    const required = conditions(item,newsConditions);
    if (required.length) section(panel, 'Условия, от которых зависит вывод', required);
    if (item.applicability === 'conditional') panel.append(paragraph('Это условный сценарий. Он не означает, что последствия уже наступили.', 'rv-note'));
    const actions = [prose.brief?.recommendation,...(prose.next_steps || []), ...(prose.to_verify || [])].filter(Boolean);
    if (actions.length) {
      const d = disclosure('Что проверить и сделать · '+unique(actions).length, list(unique(actions)));
      d.classList.add('rv-actions'); panel.append(d);
    }
    if (prose.not_established?.length) panel.append(disclosure('Что пока не установлено · '+prose.not_established.length, list(prose.not_established)));
  }
  function newsPanel(panel, item, prose, packet) {
    const news = packet.news.find(n => n.id === item.news_id);
    claims(panel, 'Что произошло · изложение в отчёте', prose.what_happened);
    const link = sourceLink(news?.source || item.source);
    if (link) panel.append(link);
    if (!news) {panel.append(paragraph('Исходные фрагменты этой новости в сохранённом пакете отсутствуют.', 'rv-note')); return;}
    panel.append(paragraph('Публикация: '+dateLabel(news.publication_date), 'rv-note'));
    const complete = news.source_traceability?.status === 'complete';
    panel.append(paragraph(complete
      ? 'Ниже — исходные абзацы, включённые в анализ. Полная статья доступна по ссылке на публикацию.'
      : 'В этом пакете исходные абзацы отсутствуют или представлены не полностью. Пересказ не заменяет проверку оригинала.',
      complete ? 'rv-note' : 'rv-warning'));
    if (news.source_blocks?.length) {
      const original = el('section', null, 'rv-section'); original.append(el('h4', 'Абзацы из источника'));
      for (const block of news.source_blocks) {
        const quote = el('blockquote', null, 'rv-quote'); quote.append(paragraph(block.text));
        original.append(quote);
      }
      panel.append(original);
    }
    const fragments = el('div');
    for (const fragment of news.fragments || []) {
      const box = el('section', null, 'rv-section'); box.append(el('h4', fragment.title || 'Фрагмент новости'));
      box.append(paragraph(fragment.thesis));
      if (fragment.context) box.append(paragraph(fragment.context, 'rv-note'));
      if (fragment.limitations?.length) section(box, 'Ограничения фрагмента', fragment.limitations);
      fragments.append(box);
    }
    if (fragments.childNodes.length) panel.append(disclosure('Подготовленные тезисы и контекст', fragments));
    if (news.limitations?.length) section(panel, 'Ограничения источника', news.limitations);
  }
  function referencedMechanisms(item, prose, packet) {
    const refs = unique(['what_happened','why_it_matters','risks','opportunities','bank_view'].flatMap(k => (prose[k] || []).flatMap(c => c.refs || [])));
    return packet.analytical_mechanisms.filter(m => refs.includes(m.id) || refs.includes(m.document_id) || m.source_fragment_ids.some(id => refs.includes(id)));
  }
  function mechanismSources(panel, mechanisms, packet, item) {
    const sourceIds = new Set(), excerpts = new Set();
    for (const mechanism of mechanisms) {
      const doc = packet.analytical_documents.find(d => d.id === mechanism.document_id) || {};
      const box = el('article', null, 'rv-analytical');
      const meta = el('div', null, 'rv-meta');
      meta.append(el('span', doc.publisher || 'Издатель не указан'), el('span', dateLabel(doc.publication_date || doc.period)));
      box.append(meta, el('h4', mechanism.title));
      if (doc.source_provenance?.provided_by_organizer === true) box.append(el('span', doc.source_provenance.label || 'Аналитика Альфа-Банка · материал организаторов', 'organizer-badge'));
      if (doc.title) box.append(paragraph(doc.title, 'rv-note'));
      const chain = el('div', null, 'rv-chain');
      for (const [key,label] of [['cause','Причина'],['channel','Канал воздействия'],['effect','Следствие']]) {
        if (!mechanism.mechanism?.[key]) continue;
        const step = el('div'); step.append(el('span',label),paragraph(mechanism.mechanism[key])); chain.append(step);
      }
      box.append(chain);
      if (mechanism.context?.conditions) {
        const cond = el('div',null,'rv-relevance'); cond.append(el('span','Условия применимости из аналитики'),paragraph(mechanism.context.conditions)); box.append(cond);
      }
      for (const ref of mechanism.evidence_refs || []) {
        const source = packet.analytical_source_fragments.find(s => s.id === ref.fragment_id);
        if (!source) continue;
        // Positions in the packet count Unicode code points (Python), not UTF-16 units.
        const quote = Array.from(source.text).slice(ref.start_char,ref.end_char).join('');
        const key=source.id+'::'+quote;
        if (!quote || excerpts.has(key)) continue;
        excerpts.add(key);
        const block = el('blockquote',null,'rv-quote');
        block.append(paragraph(quote),el('cite', 'Страницы: '+(ref.pages?.join(', ') || source.pages?.join(', ') || 'не указаны'))); box.append(block);
      }
      if (mechanism.limitations?.length) box.append(disclosure('Ограничения механизма',list(mechanism.limitations)));
      panel.append(box);
      for (const id of mechanism.source_fragment_ids) sourceIds.add(id);
    }
    if (sourceIds.size) {
      const originals = el('div');
      for (const source of packet.analytical_source_fragments.filter(s => sourceIds.has(s.id))) {
        const box = el('section',null,'rv-section');
        box.append(el('h4',source.title || 'Фрагмент аналитики'),paragraph('Страницы: '+(source.pages?.join(', ') || 'не указаны'),'rv-note'),paragraph(source.text));
        originals.append(box);
      }
      panel.append(disclosure('Полные исходные фрагменты аналитики · '+sourceIds.size,originals));
    }
  }
  function analyticsPanel(panel,item,prose,packet) {
    claims(panel,'Оценки из аналитики в изложении отчёта',prose.bank_view);
    panel.append(paragraph('Оценки источников относятся к дате соответствующего материала. Итоговые выводы для компании подготовлены ИИ.', 'rv-note'));
    const mechanisms = referencedMechanisms(item,prose,packet);
    if(mechanisms.length)mechanismSources(panel,mechanisms,packet,item);
    const refs=new Set(['what_happened','why_it_matters','risks','opportunities','bank_view'].flatMap(k=>(prose[k] || []).flatMap(c=>c.refs || [])));
    const covered=new Set(mechanisms.flatMap(m=>m.source_fragment_ids));
    const standalone=packet.analytical_source_fragments.filter(s=>!covered.has(s.id)&&(refs.has(s.id)||refs.has(s.document_id)));
    for(const source of standalone) {
      const doc=packet.analytical_documents.find(d=>d.id===source.document_id) || {};
      const box=el('section',null,'rv-analytical');box.append(el('h4',source.title || doc.title || 'Цитируемый фрагмент'));
      if(doc.source_provenance?.provided_by_organizer===true)box.append(el('span',doc.source_provenance.label || 'Аналитика Альфа-Банка · материал организаторов','organizer-badge'));
      box.append(paragraph([doc.publisher,dateLabel(doc.publication_date || doc.period)].filter(Boolean).join(' · '),'rv-note'),paragraph(source.text));panel.append(box);
    }
    if(!mechanisms.length&&!standalone.length)panel.append(paragraph('В итоговом тексте нет ссылок на аналитические источники.', 'rv-note'));
  }
  const fieldLabels = {
    main_okved_code:'Основной ОКВЭД',main_okved_name:'Основной вид деятельности',
    countries:'Страны ВЭД',fts_record_present:'Запись ФТС',annual_revenue:'Выручка',
    banking_product_flags:'Банковские продукты',industry:'Отрасль',profile:'Описание компании',
    foreign_trade:'Сведения о ВЭД',loan:'Кредитные продукты',leasing:'Лизинг',
    counterparty_payments:'Платёжные связи',region:'Регион',city:'Город'
  };
  function factValue(root,path) {
    let value=root;
    for (const key of path.split('/').slice(1).map(k => k.replace(/~1/g,'/').replace(/~0/g,'~'))) {
      if (value === null || typeof value !== 'object' || !Object.prototype.hasOwnProperty.call(value,key)) return undefined;
      value=value[key];
    }
    return value;
  }
  function displayValue(value) {
    if (value === true) return 'Да'; if (value === false) return 'Нет';
    if (value === null) return 'Не указано';
    if (Array.isArray(value)) return value.map(displayValue).join(', ');
    if (typeof value === 'object') return Object.entries(value).map(([k,v]) => (fieldLabels[k] || k)+': '+displayValue(v)).join('; ');
    return String(value);
  }
  function basisPanel(panel,item,prose,packet) {
    if (prose.why_you_see_this) {
      const reason=el('div',null,'rv-relevance');reason.append(el('span','Почему вы видите эту новость'),paragraph(prose.why_you_see_this));panel.append(reason);
    }
    const claimPaths=['why_it_matters','risks','opportunities'].flatMap(k => (prose[k] || []).flatMap(c => c.company_basis || []));
    const paths=unique([...claimPaths,...(item.assessments || []).flatMap(a => a.company_impact?.company_basis || [])]);
    if (paths.length) {
      const dl=el('dl',null,'rv-facts');
      for (const path of paths) {
        const value=factValue(packet.company,path);
        const row=el('div');row.append(el('dt',fieldLabels[path.split('/').at(-1)] || 'Сведения профиля'),el('dd',value === undefined ? 'Основание недоступно в сохранённом пакете' : displayValue(value)));dl.append(row);
      }
      panel.append(el('h4','Известные основания в профиле компании'),dl);
    } else panel.append(paragraph('Отдельные основания в профиле компании не перечислены.', 'rv-note'));
    const news=packet.news.find(n => n.id === item.news_id);
    const hypotheses=(news?.company_match?.aspects || []).filter(a => a.evidence_status !== 'grounded_topic' && a.evidence_status !== 'grounded_fact');
    if (hypotheses.length) panel.append(paragraph('Поисковые аспекты могут содержать гипотезы. Они не дополняют факты о компании.', 'rv-note'));
    section(panel,'Чего не хватает для вывода',prose.not_established);
    const diagnostics=el('div');
    diagnostics.append(paragraph('Сходство текстов не является вероятностью влияния на бизнес.'));
    if (news?.company_match?.score !== undefined) diagnostics.append(paragraph('Сходство новости с профилем: '+Number(news.company_match.score).toFixed(3)));
    for (const link of packet.connections.filter(c => c.news_id === item.news_id)) {
      const m=packet.analytical_mechanisms.find(m => m.id === link.mechanism_id);
      const text=link.selection_status === 'intersection' ? 'Найдена в обеих ветвях поиска' : 'Кандидат из одной ветви поиска';
      diagnostics.append(paragraph((m?.title || 'Аналитический материал')+' · '+text));
    }
    panel.append(disclosure('Как сработал поиск',diagnostics));
  }
  function tabs(card,item,prose,packetLoader,newsConditions=[]) {
    const uid='rv-'+(++sequence), bar=el('div',null,'rv-tabs'), content=el('div',null,'rv-tab-content');
    bar.setAttribute('role','tablist');bar.setAttribute('aria-label','Подробности новости');
    const specs=[['overview','Для компании'],['news','Новость'],['analytics','Аналитика'],['basis','Основания и ограничения']];
    const buttons=[],panels=[];
    for (const [key,title] of specs) {
      const button=el('button',title,'rv-tab');button.type='button';button.id=uid+'-tab-'+key;
      button.setAttribute('role','tab');button.setAttribute('aria-controls',uid+'-panel-'+key);
      const panel=el('section',null,'rv-tab-panel');panel.id=uid+'-panel-'+key;
      panel.setAttribute('role','tabpanel');panel.setAttribute('aria-labelledby',button.id);panel.tabIndex=0;
      buttons.push(button);panels.push(panel);bar.append(button);content.append(panel);
      button.addEventListener('click',() => select(key));
      button.addEventListener('keydown',e => {
        const i=buttons.indexOf(button);
        const next=e.key === 'ArrowRight' ? (i+1)%specs.length : e.key === 'ArrowLeft' ? (i+specs.length-1)%specs.length : e.key === 'Home' ? 0 : e.key === 'End' ? specs.length-1 : -1;
        if(next < 0)return;e.preventDefault();buttons[next].focus();select(specs[next][0]);
      });
    }
    async function fill(key,panel) {
      if(panel.dataset.ready || panel.dataset.loading)return;
      panel.dataset.loading='true';panel.replaceChildren(paragraph('Загружаем сохранённые материалы…','rv-note'));panel.setAttribute('aria-busy','true');
      try {
        const packet=await packetLoader();panel.replaceChildren();
        ({news:newsPanel,analytics:analyticsPanel,basis:basisPanel})[key](panel,item,prose,packet);
        panel.dataset.ready='true';
      } catch {
        panel.replaceChildren(paragraph('Не удалось загрузить исходные материалы. Текст отчёта сохранён.', 'rv-warning'));
        const retry=el('button','Повторить загрузку','rv-link-button');retry.type='button';retry.addEventListener('click',() => fill(key,panel));panel.append(retry);
      } finally {delete panel.dataset.loading;panel.removeAttribute('aria-busy');}
    }
    function select(key) {
      specs.forEach(([k],i) => {const active=k===key;buttons[i].setAttribute('aria-selected',String(active));buttons[i].tabIndex=active ? 0 : -1;panels[i].hidden=!active;});
      if(key !== 'overview')fill(key,panels[specs.findIndex(s => s[0]===key)]);
    }
    overview(panels[0],item,prose,newsConditions);select('overview');card.append(bar,content);
  }
  function render(result,{packetLoader,createdAt}={}) {
    const root=el('div',null,'rv-report'),accepted=(result.items || []).filter(i => ['applicable','conditional'].includes(i.applicability));
    root.dataset.companyId=result.company_id;
    const metadata=[];
    const omitted=(result.items || []).filter(i => !['applicable','conditional'].includes(i.applicability));
    const summary=el('section',null,'rv-summary');
    const meta=el('div',null,'rv-meta');meta.append(el('span','КОМПАНИЯ '+result.company_id),el('span','На '+dateLabel(result.as_of)));
    summary.append(meta,el('h3','Главное для бизнеса'),paragraph(result.summary,'rv-summary-text'));
    const footer=el('div',null,'rv-summary-footer');
    const n=accepted.length,word=n%100>=11&&n%100<=14?'новостей':n%10===1?'новость':n%10>=2&&n%10<=4?'новости':'новостей';
    footer.append(el('span',n+' '+word+' в выводах'),el('span',omitted.length+' без существенной связи'));
    if(createdAt)footer.append(el('span','Сохранён '+dateLabel(createdAt)));
    footer.append(el('span','Не является инвестиционной рекомендацией'));summary.append(footer);root.append(summary);
    for (const item of accepted) {
      const prose=narrative(item,result),card=el('article',null,'rv-news');card.dataset.newsId=item.news_id;
      const head=el('header',null,'rv-news-head'),meta=el('div',null,'rv-meta');
      const badge=el('span',labels[item.applicability] || item.applicability,'rv-status rv-'+item.applicability);meta.append(badge);
      const link=sourceLink(item.source);if(link)meta.append(link);
      metadata.push({item,meta});
      head.append(meta,el('h3',prose.short_title || item.title || prose.headline || item.news_id));
      const lead=(prose.what_happened || []).find(c => c.type === 'news_fact')?.text || item.event_summary;
      if(lead)head.append(paragraph(lead,'rv-event'));card.append(head);
      const newsConditions=result.narrative?.assessment?.news_assessments?.find(n => n.news_id === item.news_id)?.required_conditions || [];
      tabs(card,item,prose,packetLoader,newsConditions);root.append(card);
    }
    if(packetLoader)packetLoader().then(packet => {
      for(const {item,meta} of metadata){
        const news=packet.news.find(n => n.id === item.news_id);
        if(news?.publication_date)meta.append(el('span',dateLabel(news.publication_date),'rv-publication'));
      }
      const window=packet.retrieval?.settings?.news_days;
      if(window)footer.append(el('span','Окно поиска этой партии: '+window+' дней'));
    }).catch(()=>{/* Source tabs have an explicit retry when materials are unavailable. */});
    if(!accepted.length)root.append(paragraph('По этой партии существенного влияния на компанию не установлено. Отклонённые материалы доступны ниже.','rv-empty'));
    const independent=result.narrative?.independent_risks || [];
    if(independent.length) {
      const box=el('section',null,'rv-independent');box.append(el('h3','Отдельный контекст компании'),paragraph('Эти выводы относятся к компании, но не объясняют выбранную новость.','rv-note'));
      for(const item of independent){const section=el('section',null,'rv-section');section.append(el('h4',item.headline || 'Условный сценарий'));claims(section,'Возможное значение',item.why_it_matters);box.append(section);}
      const sources=disclosure('Материалы и условия для отдельного контекста');
      sources.addEventListener('toggle',async() => {
        if(!sources.open || sources.dataset.ready || sources.dataset.loading)return;sources.dataset.loading='true';
        try {const p=await packetLoader();const ids=new Set(independent.flatMap(x=>referencedMechanisms({},x,p).map(m=>m.id)).concat(independent.map(x=>x.mechanism_id)));const body=el('div');mechanismSources(body,p.analytical_mechanisms.filter(m => ids.has(m.id)),p);sources.append(body);sources.dataset.ready='true';}
        catch {sources.append(paragraph('Исходные материалы временно недоступны.','rv-note'));}
        finally {delete sources.dataset.loading;}
      });box.append(sources);root.append(box);
    }
    if(omitted.length) {
      const body=el('div');for(const item of omitted){const box=el('section',null,'rv-section');box.append(el('span',labels[item.applicability] || 'Без вывода','rv-status'),el('h4',item.title || item.news_id),paragraph(item.reason));const a=sourceLink(item.source);if(a)box.append(a);body.append(box);}
      root.append(disclosure('Не вошло в выводы · '+omitted.length,body,'rv-disclosure rv-omitted'));
    }
    if(result.overall_limitations?.length)root.append(disclosure('Ограничения отчёта и исходных данных',list(unique(result.overall_limitations)),'rv-disclosure rv-overall'));
    root.append(paragraph('Выводы подготовлены ИИ на основе сохранённых материалов. Пометка Альфа-Банка относится только к соответствующему источнику аналитики.','rv-footnote'));
    return root;
  }
  return {render,dateLabel};
})();
