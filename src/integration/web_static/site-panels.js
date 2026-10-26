'use strict';
/* Source material is escaped before it enters the reference's HTML templates. */
window.SitePanels = (() => {
  const esc = s => String(s == null ? '' : s).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const text = value => typeof value === 'string' ? value : value?.text || '';
  const list = values => (values || []).length ? '<ul class="tl">'+values.map(v=>'<li><span>'+esc(text(v))+'</span></li>').join('')+'</ul>' : '';
  const empty = message => '<p class="small muted">'+esc(message)+'</p>';
  function section(title,content) {return content ? '<div class="nbt-blk"><span class="nb-lab">'+esc(title)+'</span>'+content+'</div>' : '';}
  function conditions(values,title='Условия применимости') {
    return section(title,list(values));
  }
  function short(n) {
    const p=n.record.narrative || {},m=n.model;
    const labels={low:'Слабое влияние',weak:'Слабое влияние',medium:'Среднее влияние',high:'Сильное влияние',strong:'Сильное влияние'};
    const takeaway=p.brief?.takeaway || m.meaning;
    let body=takeaway?'<p class="nbt-text"><b class="nbt-why">Коротко.</b> '+esc(takeaway)+'</p>':'';
    if(n.impact)body+='<div class="nbt-imp"><b>'+esc(labels[n.impact.level] || n.impact.level_ru)+
      (n.impact.conditional?' · оценка условная':'')+'</b>'+(n.impact.explanation?'<p>'+esc(n.impact.explanation)+'</p>':'')+'</div>';
    if(m.actions.length)body+='<div class="nbt-foot"><span>Рекомендации: '+m.actions.length+'</span><button type="button" class="btn btn-red btn-s nbt-go" data-tab="acts">Посмотреть рекомендации</button></div>';
    return body || empty('Пояснение не подготовлено');
  }
  const TYPE_LABEL={news_fact:'Факт из новости',ai_inference:'Вывод AI',bank_view:'Позиция Альфа-Банка',third_party_view:'Оценка третьей стороны'};
  function tagged(items) {
    return (items || []).length ? '<ul class="tl">'+items.map(c=>'<li><span>'+esc(text(c))+'</span>'+
      (TYPE_LABEL[c.type]?'<i class="tl-tag">'+esc(TYPE_LABEL[c.type])+'</i>':'')+'</li>').join('')+'</ul>' : '';
  }
  function render(n) {
    const p=n.record.narrative || {},id=n.id,acts=n.model.actions;
    const hasMore=!n.record.independent || ['what_happened','why_it_matters','risks','opportunities','channels'].some(k=>p[k]?.length);
    const hasBank=Boolean(p.bank_view?.length);
    const hasCheck=p.not_established?.length || p.to_verify?.length || n.record.required_conditions?.length;
    const tabs=[['short','Кратко'],...(hasMore?[['more','Подробно']]:[]),
      ...(hasBank?[['bank',(p.bank_view || []).some(c=>c.type==='bank_view')?'Мнение банка':'Оценки аналитиков']]:[]),
      ...(hasCheck?[['chk','Что не установлено']]:[]),['acts','Рекомендации'+(acts.length?' · '+acts.length:'')]];
    const contents={short:()=>short(n),more:()=>more(n),bank:()=>tagged(p.bank_view),
      chk:()=>section('Что не установлено',list(p.not_established))+section('Что стоит проверить',list(p.to_verify))+conditions(n.record.required_conditions),
      acts:()=>(acts.length ? '<div class="nb-acts">'+acts.map((x,i)=>'<div class="nb-act"><span class="nbt-n">'+(i+1)+'</span>'+esc(x)+'</div>').join('')+'</div>' : empty('Реакция не требуется'))+
        '<p class="small muted nb-not-advice">Не является инвестиционной рекомендацией.</p>'};
    return '<div class="nbt"><div class="nbt-tabs" role="tablist" aria-label="Разделы разбора">'+tabs.map(([key,title],i)=>
      '<button type="button" role="tab" class="nbt-tab" id="t-'+id+'-'+key+'" data-tab="'+key+'" aria-controls="p-'+id+'-'+key+'" aria-selected="'+(i===0)+'" tabindex="'+(i===0?0:-1)+'">'+esc(title)+'</button>').join('')+'</div>'+tabs.map(([key])=>
      '<div class="nbt-panel" role="tabpanel" id="p-'+id+'-'+key+'" aria-labelledby="t-'+id+'-'+key+'"'+(key === 'short'?'':' hidden')+'>'+contents[key]()+'</div>').join('')+'</div>';
  }
  function more(n) {
    const p=n.record.narrative || {};
    let body=section('Каналы влияния',(p.channels || []).length?'<ul class="tl">'+p.channels.map(c=>'<li><span><b>'+esc(c.channel)+'</b> ('+esc({risk:'риск',opportunity:'возможность',mixed:'риск и возможность'}[c.direction] || c.direction)+'). '+esc(c.explanation)+'</span></li>').join('')+'</ul>':'')+
      section('Что произошло',tagged(p.what_happened))+section('Почему это важно компании',tagged(p.why_it_matters))+
      section('Риски',tagged(p.risks))+section('Возможности',tagged(p.opportunities));
    return body || empty('Дополнительных выводов нет');
  }
  return {render};
})();
