/* Public adapter behavior with a virtual clock; no API, browser or model costs. */
const fs=require('node:fs');
const vm=require('node:vm');
const assert=require('node:assert/strict');
const source=fs.readFileSync(require('node:path').join(__dirname,'../src/integration/web_static/site-adapter.js'),'utf8');

function harness() {
  let clock=0,nextTimer=1;const timers=new Map();
  const news=(id,at)=>({id,feed_id:id,title:id,publication_date:'2026-10-21',completed_at:at,
    source:{publisher:'Test'},narrative:{},topic:'Test'});
  const profile=id=>({id,facts:{},profile:'Test company',payments:{incoming:[],outgoing:[]}});
  const state={companies:[{id:'C1'},{id:'C2'}],integration:{selected_company_id:'C1'},settings:{},today:'2026-10-24'};
  const one=news('N1','2026-10-24T10:00:00Z'),two=news('N2','2026-10-24T10:01:00Z'),three=news('N3','2026-10-24T10:02:00Z');
  const feeds={C1:{company:profile('C1'),today:'2026-10-24',window_days:7,news:[one],history:[]},
               C2:{company:profile('C2'),today:'2026-10-24',window_days:7,news:[],history:[]}};
  const fakeNode=()=>({dataset:{},setAttribute(){},before(){},textContent:''});
  const status=fakeNode();
  class EventSource {constructor(){this.readyState=1;} addEventListener(){} close(){this.readyState=2;}}
  const context={URL,Intl,Date,AbortSignal,EventSource,
    setTimeout(fn,ms){const id=nextTimer++;timers.set(id,{fn,due:clock+ms});return id;},
    clearTimeout(id){timers.delete(id);},
    document:{hidden:false,querySelector(){return status;},createElement:fakeNode,addEventListener(){}},
    window:{EventSource,addEventListener(){}},
    async fetch(url,options){let body;if(url==='/api/state')body=state;
      else if(url==='/api/select-company')body={selected_company_id:JSON.parse(options.body).company_id};
      else body=feeds[new URL(url,'http://localhost').searchParams.get('company')];
      return {ok:true,json:async()=>structuredClone(body)};}
  };
  vm.createContext(context);vm.runInContext(source,context);
  return {adapter:context.window.SiteAdapter,feeds,one,two,three,
    advance(ms){clock+=ms;for(const [id,t] of [...timers])if(t.due<=clock){timers.delete(id);t.fn();}}};
}

(async()=>{
  const h=harness(),api=await h.adapter.init();const deliveries=[];
  api.onChange(()=>deliveries.push(Array.from(api.getData().news,n=>n.id)));
  h.feeds.C1.news=[h.three,h.two,h.one];
  await api.refresh();
  assert.deepEqual(Array.from(api.getData().news,n=>n.id),['N2','N1']);
  h.advance(1199);
  assert.deepEqual(Array.from(api.getData().news,n=>n.id),['N2','N1']);
  h.advance(1);
  assert.deepEqual(Array.from(api.getData().news,n=>n.id),['N3','N2','N1']);
  assert.deepEqual(deliveries,[['N2','N1'],['N3','N2','N1']]);
  // A refresh must not reinsert/reorder already delivered cards.
  await api.refresh();assert.deepEqual(Array.from(api.getData().news,n=>n.id),['N3','N2','N1']);

  const other=harness(),selected=await other.adapter.init();selected.onChange(()=>{});
  other.feeds.C1.news=[other.three,other.two,other.one];await selected.refresh();
  await selected.selectCompany('C2');other.advance(1200);
  assert.equal(selected.getData().company.id,'C2');assert.equal(selected.getData().news.length,0);
  // New presentation fields must stay company-scoped and support old impact codes.
  const final=harness();
  final.one.narrative={short_title:'Короткий заголовок',topic:'Издержки и цены',
    brief:{takeaway:'При сохранении спроса возможен рост затрат.',recommendation:'Стоит оценить закупки.'},
    impact:{level:'high'},next_steps:['Стоит оценить закупки.']};
  final.one.publication_date='2026-01-01';final.one.delivery_date='2026-10-23';
  final.feeds.C1.latest_report={summary:'Сводка первой компании',key_recommendations:[{text:'Стоит оценить закупки.'}]};
  const view=await final.adapter.init();
  assert.equal(view.getNews()[0].title,'Короткий заголовок');
  assert.equal(view.getNews()[0].level,'high');
  assert.equal(view.getNews()[0].ageDays,1);
  assert.equal(view.getNews()[0].model.actions.length,1);
  assert.equal(view.summary,'Сводка первой компании');
  final.one.narrative.impact.level='strong';await view.refresh();
  assert.equal(view.getNews()[0].level,'high');
  await view.selectCompany('C2');
  assert.equal(view.summary,null);assert.equal(view.keyRecs.length,0);assert.equal(view.getIndependent().length,0);

  const panelContext={window:{SiteAdapter:final.adapter,NewsAdapter:{company:{id:'C1',profile:'Test'}}},URLSearchParams,Intl,Date};
  vm.createContext(panelContext);
  vm.runInContext(fs.readFileSync(require('node:path').join(__dirname,'../src/integration/web_static/site-panels.js'),'utf8'),panelContext);
  await view.selectCompany('C1');const card=view.getNews()[0];
  Object.assign(card.record,{source_traceability:{},source_blocks:[],fragments:[],limitations:[],
    analytical_mechanisms:[],analytical_documents:[],analytical_source_fragments:[],connections:[],assessments:[]});
  card.record.narrative.brief.takeaway='<img src=x onerror=alert(1)>';
  const html=panelContext.window.SitePanels.render(card);
  assert.ok(html.includes('&lt;img'));assert.ok(!html.includes('<img'));
  assert.ok(html.includes('Подробно'));assert.ok(html.includes('Рекомендации'));
  assert.equal(typeof view.showControls,'undefined');
  card.record.independent=true;
  assert.ok(!panelContext.window.SitePanels.render(card).includes('data-tab="news"'));
  // An empty section in the next report must not erase a delivered context
  // when connected to a server that still exposes only the latest report.
  const archive=harness();
  const scenario={...archive.one,id:'M1',feed_id:'independent-M1'};
  archive.feeds.C1.independent_risks=[scenario];
  const archiveView=await archive.adapter.init();archiveView.onChange(()=>{});
  archive.feeds.C1.news=[archive.two,archive.one];archive.feeds.C1.independent_risks=[];
  await archiveView.refresh();
  assert.equal(archiveView.getIndependent().length,1);
  archive.advance(1200);assert.equal(archiveView.getIndependent().length,1);
  archive.feeds.C1.independent_risks=[{...scenario,title:'Updated scenario'}];
  await archiveView.refresh();assert.equal(archiveView.getIndependent().length,1);
  assert.equal(archiveView.getIndependent()[0].title,'Updated scenario');
  await archiveView.selectCompany('C2');assert.equal(archiveView.getIndependent().length,0);
  // New servers return authoritative history, including an explicit empty archive.
  archive.feeds.C2.independent_risks=[scenario];archive.feeds.C2.independent_risks_scope='history';
  await archiveView.refresh();assert.equal(archiveView.getIndependent().length,1);
  archive.feeds.C2.independent_risks=[];await archiveView.refresh();
  assert.equal(archiveView.getIndependent().length,0);
  // News and company context use one chronological, sequentially delivered
  // feed. Context updates replace the same card and obey company isolation.
  const mixed=harness();const mixedView=await mixed.adapter.init();mixedView.onChange(()=>{});
  const contextCard={...mixed.two,id:'M1',feed_id:'independent-M1',independent:true,topic:'Контекст',completed_at:'2026-10-24T10:00:30Z'};
  mixed.feeds.C1.news=[mixed.two,mixed.one];mixed.feeds.C1.independent_risks=[contextCard];
  mixed.feeds.C1.independent_risks_scope='history';
  await mixedView.refresh();
  assert.deepEqual(Array.from(mixedView.getNews(),n=>n.sourceId),['M1','N1']);
  assert.equal(mixedView.getNews()[0].record.independent,true);assert.ok(mixedView.topics().includes('Контекст'));
  mixed.advance(1200);
  assert.deepEqual(Array.from(mixedView.getNews(),n=>n.sourceId),['N2','M1','N1']);
  mixed.feeds.C1.independent_risks=[{...contextCard,title:'Обновлённый контекст'}];await mixedView.refresh();
  assert.equal(mixedView.getNews().length,3);assert.equal(mixedView.getNews()[1].title,'Обновлённый контекст');
  await mixedView.selectCompany('C2');assert.equal(mixedView.getNews().length,0);
  process.stdout.write('Unified feed: mixed sequential delivery, stable context updates, topics and company isolation passed.\n');
  // A financial revision changes the existing card, without queueing a second
  // one. Only the server's 12-hour notification version changes unread state.
  const financial=harness();financial.one.feed_id='fx-usd-official';financial.one.notification_revision='first';
  const financialView=await financial.adapter.init();financialView.onChange(()=>{});
  const financialId=financialView.getNews()[0].id;
  financial.feeds.C1.news=[{...financial.two,feed_id:'fx-usd-official',notification_revision:'first',updates:[{id:'N1'}]}];
  await financialView.refresh();
  assert.equal(financialView.getNews().length,1);assert.equal(financialView.getNews()[0].id,financialId);
  assert.equal(financialView.getNews()[0].sourceId,'N2');assert.equal(financialView.getNews()[0].notificationRevision,'first');
  assert.ok(financialView.getNews()[0].dateLabel.startsWith('Обновлено'));
  financial.feeds.C1.news[0].notification_revision='after-12h';await financialView.refresh();
  assert.equal(financialView.getNews().length,1);assert.equal(financialView.getNews()[0].notificationRevision,'after-12h');
  process.stdout.write('Financial updates: stable card, immediate content and server notification revision passed.\n');
  process.stdout.write('Live client: sequential delivery, stable ordering and company-switch cancellation passed.\n');
  process.stdout.write('Final UI: compact/legacy fields, company isolation, source tabs and HTML escaping passed.\n');
  process.stdout.write('Independent history: empty-report retention, stable updates and company isolation passed.\n');
})().catch(error=>{process.stderr.write(String(error.stack)+'\n');process.exitCode=1;});
