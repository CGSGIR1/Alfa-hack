/* Render real public components with disputed candidates and final citations. */
const fs=require('node:fs'),vm=require('node:vm'),path=require('node:path'),assert=require('node:assert/strict');
const staticRoot=path.join(__dirname,'../src/integration/web_static');
const quote='Оригинальный фрагмент, использованный в итоговом выводе.';
const mechanism=(id,doc,title)=>({id,document_id:doc,title,mechanism:{cause:'Причина',channel:'Канал',effect:'Следствие'},
  context:{conditions:'Только при сохранении спроса'},source_fragment_ids:[doc+'::F1'],
  evidence_refs:[{fragment_id:doc+'::F1',start_char:0,end_char:quote.length,pages:[1]}],limitations:[]});
const prose={brief:{takeaway:'При сохранении спроса возможен эффект.',recommendation:'Стоит оценить спрос.'},
  why_you_see_this:'Публичное пояснение.',bank_view:[{text:'Итоговая аналитическая оценка.',refs:['A2::F1'],type:'bank_view'}],
  why_it_matters:[],what_happened:[],risks:[],opportunities:[],not_established:[]};
const record={id:'N1',publication_date:'2026-10-22',run_id:'R1',narrative:prose,
  reason:'SERVICE_REASON opinion_1 /facts/industry',applicability:'conditional',
  analytical_documents:[{id:'A1',title:'Отклонённый кандидат',publisher:'Третий источник'},
    {id:'A2',title:'Нужный материал',publisher:'Альфа-Банк',source_provenance:{provided_by_organizer:true}}],
  analytical_mechanisms:[mechanism('A1::M1','A1','Ненужный механизм'),mechanism('A2::M1','A2','Цитируемый механизм')],
  analytical_source_fragments:['A1','A2'].map(id=>({id:id+'::F1',document_id:id,text:quote,pages:[1]})),
  connections:[{id:'L1',news_id:'N1',mechanism_id:'A1::M1'}],
  assessments:[{connection_id:'L1',news_mechanism_fit:'rejected',company_impact:{statement:'SERVICE_LINK opinion_2'}}],
  source_blocks:[],source_traceability:{},fragments:[],limitations:[]};
const context={window:{NewsAdapter:{company:{id:'C1',profile:'Профиль'}},SiteAdapter:{safeUrl:()=>null}},Intl,Date,URLSearchParams};
vm.createContext(context);vm.runInContext(fs.readFileSync(path.join(staticRoot,'site-panels.js'),'utf8'),context);
const card={id:'n1',record,source:{},model:{actions:['Стоит оценить спрос.'],meaning:''},figures:[]};
let html=context.window.SitePanels.render(card);
assert.ok(!html.includes('<details'), 'No nested disclosure controls in the public report');
assert.equal((html.match(/role="tablist"/g)||[]).length,1,'Only the main report tabs remain');
for(const text of ['Итоговая аналитическая оценка.','Позиция Альфа-Банка'])assert.ok(html.includes(text),text);
for(const text of ['SERVICE_','opinion_','Ненужный механизм','Цитируемый механизм',quote,'Отклонённый кандидат','Связь с новостью отклонена','Источник новости и исходные абзацы','Источники аналитики и цитаты'])assert.ok(!html.includes(text),text);
const independent=structuredClone(card);independent.record.independent=true;
html=context.window.SitePanels.render(independent);
assert.ok(html.includes('Итоговая аналитическая оценка.'));assert.ok(!html.includes('data-tab="news"'));
const legacy=structuredClone(card);legacy.record.narrative={};
html=context.window.SitePanels.render(legacy);
assert.ok(!html.includes('SERVICE_'));assert.ok(!html.includes('Ненужный механизм'));

// The expanded report view must apply the same contract as the main feed.
class Element {
  constructor(tag){this.tag=tag;this.children=[];this.dataset={};this.events={};this.attrs={};this.classList={add(){}};this.value='';}
  append(...nodes){this.children.push(...nodes);}
  replaceChildren(...nodes){this.children=nodes;}
  set textContent(x){this.value=String(x);this.children=[];}
  get textContent(){return this.value+this.children.map(x=>typeof x==='string'?x:x.textContent).join(' ');}
  get childNodes(){return this.children;}
  setAttribute(k,v){this.attrs[k]=String(v);}
  removeAttribute(k){delete this.attrs[k];}
  addEventListener(k,fn){this.events[k]=fn;}
}
const manage={window:{},document:{createElement:tag=>new Element(tag)},URL,Intl,Date};
vm.createContext(manage);vm.runInContext(fs.readFileSync(path.join(staticRoot,'manage/report-view.js'),'utf8'),manage);
const walk=n=>[n,...n.children.filter(x=>typeof x!=='string').flatMap(walk)];
(async()=>{
  const packet={news:[{id:'N1',publication_date:record.publication_date,source:{}}],company:{},
    connections:record.connections,analytical_documents:record.analytical_documents,
    analytical_mechanisms:record.analytical_mechanisms,analytical_source_fragments:record.analytical_source_fragments};
  const result={company_id:'C1',as_of:'2026-10-23',summary:'Сводка.',items:[{...record,news_id:'N1',title:'Новость'}],
    company_context:[{mechanism_id:'A1::M1',statement:'SERVICE_CONTEXT opinion_1'}],narrative:{independent_risks:[]}};
  const root=manage.window.ReportView.render(result,{packetLoader:async()=>packet});
  const tab=walk(root).find(n=>n.attrs.role==='tab'&&n.textContent==='Аналитика');assert.ok(tab);tab.events.click();
  await new Promise(setImmediate);
  assert.ok(root.textContent.includes('Цитируемый механизм'),root.textContent);
  assert.ok(!root.textContent.includes('Ненужный механизм'));assert.ok(!root.textContent.includes('SERVICE_'));
  process.stdout.write('Report presentation: final citations, hidden diagnostics, independent context and legacy compatibility passed.\n');
})().catch(e=>{process.stderr.write(e.stack+'\n');process.exitCode=1;});
