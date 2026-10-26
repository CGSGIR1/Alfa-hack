'use strict';
/* The reference UI receives saved server results. No demo scoring or inference here. */
window.SiteAdapter = (() => {
  let serverState, data, apiObject, selection = 0, stream, refreshInFlight, liveTimer, lastRevision, syncAgain=false, liveListeners=false;
  let deliveryTimer=null, deliveryTarget=null;
  const cardKey=n=>n.feed_id || n.id;
  const byDelivery=(a,b)=>(b.completed_at || '').localeCompare(a.completed_at || '') || (b.delivery_sequence || 0)-(a.delivery_sequence || 0);
  const date = value => new Date(value+'T12:00:00+03:00');
  const unique = values => [...new Set(values.filter(Boolean))];
  const cards = value => [...new Map([...(value.news || []),...(value.independent_risks || [])]
    .map(n=>[cardKey(n),n])).values()].sort(byDelivery);
  function withCards(value,visible) {
    const ids=new Set(visible.map(cardKey));
    return {...value,news:(value.news || []).filter(n=>ids.has(cardKey(n))),
      independent_risks:(value.independent_risks || []).filter(n=>ids.has(cardKey(n)))};
  }
  async function request(url,body) {
    const response=await fetch(url,body === undefined ? {cache:'no-store',signal:AbortSignal.timeout(20000)} : {
      method:'POST',headers:{'Content-Type':'application/json','X-Requested-With':'news-analytics-ui'},body:JSON.stringify(body)
    });
    const result=await response.json();
    if(!response.ok) {
      const error=Error(result.error || 'Не удалось выполнить запрос');error.status=response.status;throw error;
    }
    return result;
  }
  function safeUrl(value) {
    try {const u=new URL(value);return ['http:','https:'].includes(u.protocol) ? u.href : null;}catch{return null;}
  }
  function company(raw) {
    const f=raw.facts || {},flags=f.banking_product_flags || {},foreign=f.foreign_trade || {};
    return {id:raw.id,displayName:'Компания '+raw.id,industry:f.industry?.main_okved_name || raw.profile,
      okved:f.industry?.main_okved_code,city:f.registration?.city,profile:raw.profile,facts:f,
      limitations:raw.limitations || [],payments:{incoming:raw.payments.incoming,outgoing:raw.payments.outgoing,
        currency:raw.payments.currency,amountUnit:raw.payments.amount_unit},
      products:[['Расчётный счёт','settlement_services'],['Кредит','loans'],['Лизинг','leasing'],
        ['Зарплатный проект','payroll_project'],['Гарантии','bank_guarantees'],['Депозиты','deposits'],['Факторинг','factoring']]
        .map(([label,key])=>({label,on:flags[key] === true})).concat([{label:'ВЭД',on:foreign.fts_record_present === true}])};
  }
  function mapNews(n) {
    const p=n.narrative || {},risks=p.risks || [],opportunities=p.opportunities || [];
    const levels={weak:'low',low:'low',medium:'medium',strong:'high',high:'high'};
    const impact=p.impact && levels[p.impact.level] ? {...p.impact,level:levels[p.impact.level]} : null;
    const publication=date(n.publication_date);
    return {id:'n-'+encodeURIComponent(cardKey(n)),sourceId:n.id,notificationRevision:n.notification_revision || null,title:p.short_title || n.title,topic:p.topic || n.topic,date:publication,
      dateLabel:n.completed_at ? (n.updates?.length?'Обновлено ':'Получено ')+new Intl.DateTimeFormat('ru-RU',{day:'numeric',month:'long',hour:'2-digit',minute:'2-digit',timeZone:'Europe/Moscow'}).format(new Date(n.completed_at)) :
        new Intl.DateTimeFormat('ru-RU',{day:'numeric',month:'long',timeZone:'Europe/Moscow'}).format(publication),
      ageDays:Math.max(0,Math.round((date(data.today)-date(n.delivery_date || n.publication_date))/864e5)),
      source:{publisher:n.source.publisher || 'Источник',url:safeUrl(n.source.url)},
      facts:(p.what_happened || []).map(c=>c.text),figures:p.key_figures || [],brief:p.brief || null,
      bankView:(p.bank_view || []).map(c=>c.text).join('\n\n'),
      model:{meaning:p.brief?.takeaway || p.why_you_see_this || (p.why_it_matters || [])[0]?.text || '',
        rationale:(p.why_it_matters || []).map(c=>c.text).join('\n\n'),
        actions:unique([p.brief?.recommendation,...(p.next_steps || [])])},
      kind:risks.length && opportunities.length ? 'mixed' : risks.length ? 'risk' : opportunities.length ? 'opportunity' : 'relevant',
      hasRisks:risks.length > 0,hasOpportunities:opportunities.length > 0,
      // This is a qualitative rubric, never a probability or a 0–100 meter.
      impact,score:null,level:impact ? impact.level : 'unrated',drivers:[],areas:{},record:n};
  }
  function retainContexts(next) {
    // Older running servers return only the latest report's independent
    // section. Preserve already delivered contexts until that server restarts.
    if(next.independent_risks_scope !== 'history' && data?.company.id === next.company.id) {
      const retained=new Map((data.independent_risks || []).map(n=>[cardKey(n),n]));
      for(const item of next.independent_risks || [])retained.set(cardKey(item),item);
      next={...next,independent_risks:[...retained.values()].sort(byDelivery)};
    }
    return next;
  }
  function apply(next) {
    next=retainContexts(next);
    data=next;
    apiObject.today=date(next.today);apiObject.company=company(next.company);
    apiObject.roles=serverState.companies.map(c=>({key:c.id,label:c.id+(c.type === 'group' ? ' · группа' : '')}));
    apiObject.defaultRole=next.company.id;
    apiObject.mode='report';apiObject.market=next.market_indicators || [];
    apiObject.summary=next.latest_report?.summary || null;
    apiObject.keyRecs=(next.latest_report?.key_recommendations || []).map(k=>k.text);
    apiObject.missing=next.latest_report?.missing_information || [];apiObject.dismissed=[];
  }
  async function selectCompany(id) {
    const revision=++selection;
    clearTimeout(deliveryTimer);deliveryTimer=null;deliveryTarget=null;
    await request('/api/select-company',{company_id:id});
    const next=await request('/api/site-feed?company='+encodeURIComponent(id));
    if(revision !== selection)return false;
    apply(next);connectLive();return true;
  }
  const companyJobKey='alfa-add-company-job-v1';
  function pendingCompany() {
    try {return sessionStorage.getItem(companyJobKey);}catch{return null;}
  }
  async function addCompany(id,progress=()=>{},resumeJob=null) {
    if(!serverState.integration?.can_add_company)throw Error('Добавление компаний станет доступно после перезапуска приложения.');
    const jobId=resumeJob || (await request('/api/add-company',{company_id:id})).job_id;
    if(!jobId)throw Error('Не удалось начать подготовку компании');
    try {sessionStorage.setItem(companyJobKey,jobId);}catch{}
    while(true) {
      let job;
      try {job=await request('/api/jobs/'+encodeURIComponent(jobId));}
      catch(error) {
        if(error.status === 404)try {sessionStorage.removeItem(companyJobKey);}catch{}
        throw error;
      }
      progress(job.phase || 'Подготавливаем компанию…');
      if(job.status === 'failed') {
        try {sessionStorage.removeItem(companyJobKey);}catch{}
        throw Error(job.error || 'Не удалось добавить компанию');
      }
      if(job.status === 'complete') {
        serverState=await request('/api/state');
        try {sessionStorage.removeItem(companyJobKey);}catch{}
        return job.company_result.company_id;
      }
      await new Promise(resolve=>setTimeout(resolve,1500));
    }
  }
  async function refresh() {
    const id=data.company.id,revision=selection,previous=JSON.stringify(data);
    const nextState=await request('/api/state');
    const next=await request('/api/site-feed?company='+encodeURIComponent(id));
    if(revision !== selection || id !== data.company.id)return false;
    serverState=nextState;applyIncremental(next);
    return previous !== JSON.stringify(next);
  }
  function applyIncremental(next) {
    next=retainContexts(next);
    deliveryTarget=next;
    const visible=new Set(cards(data).map(cardKey));
    apply(withCards(next,cards(next).filter(n=>visible.has(cardKey(n)))));
    if(deliveryTimer === null)deliverOne();
  }
  function deliverOne() {
    deliveryTimer=null;
    if(!deliveryTarget || deliveryTarget.company.id !== data.company.id)return;
    const visible=new Set(cards(data).map(cardKey));
    const pending=cards(deliveryTarget).filter(n=>!visible.has(cardKey(n))).sort((a,b)=>-byDelivery(a,b));
    const next=pending[0];if(!next)return;
    apply(withCards(deliveryTarget,[next,...cards(data)]));
    changed();
    // Backlog received after reconnect is also presented one card at a time.
    deliveryTimer=setTimeout(deliverOne,1200);
  }
  async function syncLive() {
    if(refreshInFlight){syncAgain=true;return refreshInFlight;}
    refreshInFlight=(async()=>{
      try {do {syncAgain=false;if(await refresh())changed();}while(syncAgain);}
      catch{/* Keep saved cards readable while reconnecting. */}
      finally{refreshInFlight=null;}
    })();
    return refreshInFlight;
  }
  function connectLive() {
    if(stream)stream.close();lastRevision=null;
    if(!data.company.id)return;
    const id=data.company.id;
    if(window.EventSource) {
      stream=new EventSource('/api/site-events?company='+encodeURIComponent(id));
      stream.addEventListener('feed',event=>{
        if(id !== data.company.id)return;
        const revision=JSON.parse(event.data).revision;
        if(revision !== lastRevision){lastRevision=revision;syncLive();}
      });
    }
  }
  function startLive() {
    clearTimeout(liveTimer);
    async function watch() {
      if(!document.hidden) {
        if(!stream || stream.readyState !== 1)await syncLive();
        else try {serverState=await request('/api/state');}catch{/* Retry on the next poll. */}
      }
      liveTimer=setTimeout(watch,5000);
    }
    liveTimer=setTimeout(watch,5000);
    if(liveListeners)return;liveListeners=true;
    document.addEventListener('visibilitychange',()=>{if(!document.hidden)syncLive();});
    window.addEventListener('online',()=>{connectLive();syncLive();});
    window.addEventListener('pagehide',()=>{clearTimeout(liveTimer);clearTimeout(deliveryTimer);deliveryTimer=null;if(stream)stream.close();});
    window.addEventListener('pageshow',event=>{if(event.persisted){connectLive();startLive();syncLive();}});
  }
  let changed=()=>{};
  async function init() {
    serverState=await request('/api/state');
    apiObject={areas:[],roles:[],excluded:[],typo:s=>s,getNews:()=>cards(data).map(mapNews),
      topics:()=>unique(cards(data).map(n=>n.narrative?.topic || n.topic)),
      getIndependent:()=>(data.independent_risks || []).map(mapNews),selectCompany,refresh,addCompany,pendingCompany,
      onChange:handler=>{changed=handler;startLive();},getState:()=>serverState,getData:()=>data};
    const id=serverState.integration?.selected_company_id || serverState.companies[0]?.id;
    if(id)await selectCompany(id);
    else {
      data={company:{id:'',profile:'Загрузите JSON компании.',facts:{},limitations:[],payments:{incoming:[],outgoing:[]}},
        today:serverState.today,window_days:serverState.settings.news_days || 7,news:[],history:[]};apply(data);
    }
    return apiObject;
  }
  return {init,safeUrl};
})();
