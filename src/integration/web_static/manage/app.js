'use strict';
const $ = id => document.getElementById(id);
let state = null, selectedFiles = [], busy = false, lastJob = null;
let currentView = null, profileCompany = null, watchedJob = null;
const packetCache = new Map();
const node = (tag, text, cls) => {const e=document.createElement(tag);if(text!==undefined)e.textContent=String(text);if(cls)e.className=cls;return e;};
function showError(message){$('error').textContent=message;$('error').hidden=false;}
function clearNotice(){$('error').hidden=true;$('message').hidden=true;}
function renderAutomation(){
 const enabled=!!state?.integration?.auto_poll;
 $('auto-state').textContent=enabled?'Автоматическая обработка включена':'Автоматическая обработка выключена';
 $('auto-toggle').textContent=enabled?'Выключить автоматическую обработку':'Включить автоматическую обработку';
 $('auto-toggle').disabled=!state?.integration?.llm_configured;
 $('worker-state').textContent=state?.active_job?.status==='running'?state.active_job.phase:
  state?.integration?.collection?.status==='running'?'Проверяем новостные источники.':enabled?'Ожидаем новые материалы.':'Фоновые циклы приостановлены.';
}
async function api(path,body){const r=await fetch(path,body===undefined?{}:{method:'POST',headers:{'Content-Type':'application/json','X-Requested-With':'news-analytics-ui'},body:JSON.stringify(body)});const data=await r.json();if(!r.ok)throw Error(data.error||'Ошибка запроса.');return data;}
function setBusy(value){busy=value;$('search-button').disabled=value||!$('company').value;$('upload-button').disabled=value||!selectedFiles.length;$('company').disabled=value;$('progress').hidden=!value;$('complete-button').disabled=value||!$('llm-result').files.length;$('poll-button').disabled=value||!state?.integration?.llm_configured;$('generate-report').disabled=value||!state?.integration?.llm_configured;}
function batchURL(runId,companyId,format){return '/api/batch?'+new URLSearchParams({run_id:runId,company_id:companyId,format});}
function loadPacket(runId,companyId){
 const key=runId+'::'+companyId;
 if(!packetCache.has(key)){
  const value=api(batchURL(runId,companyId,'llm')).catch(error=>{packetCache.delete(key);throw error;});
  packetCache.set(key,value);if(packetCache.size>8)packetCache.delete(packetCache.keys().next().value);
 }
 return packetCache.get(key);
}
function showCompanyReport(result,runId,createdAt){
 if(result.company_id!==$('company').value)return;
 currentView={type:'report',company:result.company_id,runId};
 $('results-title').textContent='Отчёт для '+result.company_id;
 $('result-summary').replaceChildren();
 $('results').replaceChildren(ReportView.render(result,{packetLoader:()=>loadPacket(runId,result.company_id),createdAt}));
 const downloads=$('downloads');downloads.replaceChildren();downloads.hidden=false;
 const files=details('Скачать материалы','rv-download-menu');
 for(const [format,label] of [['result','Отчёт JSON'],['html','Подбор источников HTML'],['llm','Пакет для LLM']]){
  const a=node('a',label);a.href=batchURL(runId,result.company_id,format);files.append(a);
 }
 downloads.append(files);
}
function renderGenerated(job){
 showCompanyReport(job.result,job.completion?.run_id||job.result.request_id.split('::')[0]);
}
function profile(){
 const c=state?.companies.find(c=>c.id===$('company').value);$('profile').replaceChildren();
 if(profileCompany!==c?.id){
  profileCompany=c?.id;currentView=null;$('downloads').hidden=true;$('result-summary').replaceChildren();
  $('results-title').textContent='Новости и связанная аналитика';
  const empty=node('div',undefined,'empty');empty.append(node('h3','Загружаем историю компании…'));$('results').replaceChildren(empty);
 }
 if(c){$('profile').append(node('span',c.type==='group'?'Профиль группы':'Компания','type'),node('p',c.profile));api('/api/select-company',{company_id:c.id}).catch(e=>showError(e.message));}
 else $('profile').append(node('p','Сначала загрузите JSON компании.','muted'));
 $('search-button').disabled=busy||!c;loadHistory({showLatest:!currentView}).catch(e=>showError(e.message));
}
async function loadHistory({showLatest=false}={}){
 const viewAtRequest=currentView;
 const company=$('company').value;if(!company){$('company-history').replaceChildren(node('p','Выберите компанию.','muted'));return;}
 const rows=await api('/api/history?company='+encodeURIComponent(company));if(company!==$('company').value)return;
 const target=$('company-history');target.replaceChildren();
 if(!rows.length){target.append(node('p','Сохранённых партий пока нет.','muted'));if(!currentView){const e=node('div',undefined,'empty');e.append(node('h3','Для этой компании отчётов пока нет'),node('p','Получите новые материалы или включите автоматическую обработку.'));$('results').replaceChildren(e);}return;}
 for(const row of rows){
  const card=node('article',undefined,'rv-history-row'),info=node('div');
  info.append(node('strong',row.status==='completed'?'Готовый отчёт':row.status==='prepared'?'Ожидает завершения':'Партия отменена'),node('p',ReportView.dateLabel(row.completed_at||row.created_at),'muted'));
  if(row.last_error)info.append(detailsWithText('Причина незавершения',row.last_error));
  card.append(info);
  if(row.result){const open=node('button','Открыть отчёт','button secondary');open.type='button';open.addEventListener('click',()=>{showCompanyReport(row.result,row.run_id,row.completed_at||row.created_at);$('results-title').scrollIntoView({behavior:matchMedia('(prefers-reduced-motion: reduce)').matches?'auto':'smooth',block:'start'});});card.append(open);}
  else {const a=node('a','Подбор материалов');a.href=batchURL(row.run_id,company,'html');card.append(a);}
  target.append(card);
 }
 const requestedRun=new URLSearchParams(location.search).get('run_id');
 const latest=rows.find(r=>r.run_id===requestedRun&&r.status==='completed'&&r.result)||rows.find(r=>r.status==='completed'&&r.result);
 if(showLatest&&latest&&currentView===viewAtRequest&&currentView?.runId!==latest.run_id)showCompanyReport(latest.result,latest.run_id,latest.completed_at||latest.created_at);
 else if(showLatest&&!latest&&!currentView){const e=node('div',undefined,'empty');e.append(node('h3','Отчёт ещё не завершён'),node('p','Подготовленные партии доступны в истории ниже.'));$('results').replaceChildren(e);}
}
async function refresh(){
 const previous=$('company').value;state=await api('/api/state');renderAutomation();$('stats').replaceChildren();
 for(const [key,label] of [['companies','профилей'],['news','новостей'],['analytics','отчётов']]){const s=node('div',undefined,'stat');s.append(node('strong',state.counts[key]),node('span',label));$('stats').append(s);}
 $('company').replaceChildren();for(const c of state.companies){const o=node('option',c.id+(c.type==='group'?' · группа':''));o.value=c.id;$('company').append(o);}
 if(!state.companies.length){const o=node('option','Нет профилей — загрузите JSON');o.value='';$('company').append(o);}
 const selected=previous||new URLSearchParams(location.search).get('company')||state.integration?.selected_company_id;
 if(state.companies.some(c=>c.id===selected))$('company').value=selected;
 if(!$('as-of').value)$('as-of').value=state.today;
 $('connection-state').textContent=(state.integration?.model_available?'Локальная модель подключена. ':'Укажите папку EmbeddingGemma в настройках. ')+(state.integration?.llm_configured?'Подключение к ИИ настроено.':'Для итогового отчёта настройте доступ к ИИ; поиск материалов доступен отдельно.');
 if(!state.integration?.llm_configured){$('generate-report').checked=false;$('generate-report').disabled=true;}
 $('poll-button').disabled=busy||!state.integration?.llm_configured;
 $('window-hint').textContent=`Поиск новостей за ${state.settings.news_days??7} дней до выбранной даты.`;
 $('candidate-limit').textContent=`Поиск: до ${state.settings.analytics_per_news??10} кандидатов на ветвь. Финансовые новости проходят общий отбор.`;
 if(!$('top-news').dataset.initialized){$('top-news').value=state.settings.top_news??3;$('top-news').dataset.initialized='true';}profile();
}
function safeLink(url){try{const parsed=new URL(url);return ['http:','https:'].includes(parsed.protocol)?parsed.href:null;}catch{return null;}}
function details(title,cls){const d=node('details',undefined,cls);d.append(node('summary',title));return d;}
function detailsWithText(title,text){const d=details(title,'rv-disclosure');d.append(node('p',text,'muted'));return d;}
function renderAnalysis(a){
 const card=node('article',undefined,'analysis-card');card.append(node('h4',a.title),node('p',`${a.document_title} · ${a.document_date||a.document_period||'Дата не указана'}`,'source'));
 if(a.source_provenance?.label)card.append(node('span',a.source_provenance.label,'organizer-badge'));
 const m=a.mechanism,chain=node('div',undefined,'chain');
 for(const [key,title] of [['cause','Причина'],['channel','Механизм'],['effect','Результат']]){const col=node('div');col.append(node('small',title),node('p',m.mechanism[key]));chain.append(col);}card.append(chain);
 const conditions=node('div',undefined,'conditions');conditions.append(node('small','Условия применимости'),node('span',m.context.conditions));card.append(conditions);
 const scores=node('div',undefined,'analysis-scores');scores.append(node('span',`Механизм: ${a.mechanism_similarity.toFixed(3)}`),node('span',`Исходный текст: ${a.source_similarities.map(s=>s.similarity.toFixed(3)).join(' / ')}`));card.append(scores);
 const source=details('Читать исходные фрагменты и ограничения','original');for(const f of a.source_fragments){source.append(node('h5',`${f.title} · стр. ${f.pages.join(', ')}`),node('p',f.text));}
 for(const limit of m.limitations||[])source.append(node('p',limit));card.append(source);return card;
}
function candidateTable(title,rows,columns){const table=node('table');table.append(node('caption',title));const head=node('thead'),tr=node('tr');for(const c of columns)tr.append(node('th',c[0]));head.append(tr);table.append(head);const body=node('tbody');for(const r of rows){const row=node('tr');for(const [label,get] of columns)row.append(node('td',get(r),label==='Сходство'?'score-cell':''));body.append(row);}table.append(body);return table;}
function renderReport(report,jid){
 if(report.company_id!==$('company').value)return;
 currentView={type:'search',company:report.company_id};
 $('results-title').textContent=`Подбор для ${report.company_id}`;$('results').replaceChildren();
 const summary=node('div',undefined,'result-summary');for(const s of [`${report.totals.selected_news} новостей`,`${report.presentation?.selected_unique_mechanisms??report.totals.intersection_links} механизмов в пакете · ${report.totals.intersection_links} связей в полном поиске`,`На ${report.as_of}`,`${report.settings.analytics_per_news} кандидатов в каждой ветви`])summary.append(node('span',s,'pill'));$('result-summary').replaceChildren(summary);
 $('downloads').replaceChildren();for(const [fmt,title] of [['html','Скачать отчёт'],['json','Полный JSON поиска'],['llm','Вход для LLM']]){const a=node('a',title);a.href=`/api/download/${jid}/${fmt}`;$('downloads').append(a);}$('downloads').hidden=false;
 for(const t of report.financial_topic_coverage?.topics||[]){if(t.status==='omitted_financial_news_limit')$('results').append(node('p',`Тема ${t.topic==='currency'?'курса':'ставки'} пропущена: выбран лимит в одну финансовую новость.`,'muted'));else if(t.status==='no_qualified_fresh_event')$('results').append(node('p',`Нет подходящего свежего события по теме: ${t.topic==='currency'?'курс и трансграничные расчёты':'ставка и финансирование'}.`,'muted'));}
 if(!report.news.length){const e=node('div',undefined,'empty');e.append(node('div','⌕','empty-icon'),node('h3','Подходящих новостей не найдено'),node('p','Проверьте дату поиска или добавьте новые материалы.'));$('results').append(e);return;}
 report.news.forEach((n,index)=>{
  const visible=n.analytics.filter(a=>!report.presentation||report.presentation.connections.some(c=>c.news_id===n.news_id&&c.mechanism_id===a.mechanism_id&&c.news_fragment_id===a.news_chunk_id));
  const card=node('article',undefined,'news-card'),head=node('div',undefined,'news-head'),meta=node('div',undefined,'meta');meta.append(node('span',String(index+1).padStart(2,'0'),'number'),node('span',n.publisher||'Источник'),node('span',n.publication_date),node('span',`Сходство с компанией ${n.company_score.toFixed(3)}`,'score'));head.append(meta);
  const title=node('h3');const url=safeLink(n.url);if(url){const a=node('a',n.title);a.href=url;a.target='_blank';a.rel='noopener noreferrer';title.append(a);}else title.textContent=n.title;head.append(title,node('p',n.selected_fragments.map(f=>f.thesis).join('\n'),'lead'));card.append(head);
  const evidence=details('Почему выбрана новость · контекст и источники','evidence');if(n.selection_reason==='financial_topic_coverage')evidence.append(node('p','Дополнительная новость для покрытия финансовой темы; влияние на компанию требует проверки.'));if(!n.source_blocks?.length)evidence.append(node('p','Исходные блоки статьи отсутствуют: доступен подготовленный пересказ.'));for(const b of n.source_blocks||[])evidence.append(node('p',b.text));for(const q of n.matching_queries)evidence.append(node('p',q.query+` · сходство с учётом веса ${q.weighted_score.toFixed(3)}`));for(const f of n.selected_fragments)evidence.append(node('p',f.context));for(const limit of n.document_limitations||[])evidence.append(node('p',limit));card.append(evidence);
  const section=node('section',undefined,'analysis-section'),label=node('div',undefined,'analysis-heading');label.append(node('span','','dot'),node('span',`Аналитика для отчёта · ${visible.length} из ${n.analytics.length} совпадений`));section.append(label);
  if(visible.length){for(const a of visible)section.append(renderAnalysis(a));}else section.append(node('p','В компактный пакет аналитика не включена. Полные результаты поиска доступны ниже.','analysis-empty'));
  const candidates=details(`Все кандидаты: ${n.mechanism_candidates.length} по механизмам · ${n.source_candidates.length} по исходному тексту`,'candidates');
  candidates.append(candidateTable('По механизмам аналитики',n.mechanism_candidates,[['Материал',r=>r.title],['Сходство',r=>r.mechanism_similarity.toFixed(3)],['Пересечение',r=>r.retrieval_agreement?'Да':'Нет'],['Источник',r=>r.source_provenance?.label||r.document_publisher||'Внешний источник']]));
  const sourceTable=candidateTable('По исходным фрагментам',n.source_candidates,[['Материал',r=>r.title],['Сходство',r=>r.similarity.toFixed(3)],['Источник',r=>r.source_provenance?.label||'Внешний источник']]);candidates.append(sourceTable);
  const texts=details('Читать найденные исходные фрагменты','original');for(const s of n.source_candidates){texts.append(node('h5',`${s.title} · стр. ${s.pages.join(', ')}`),node('p',s.text));}candidates.append(texts);section.append(candidates);card.append(section);$('results').append(card);
 });
}
async function follow(jid){
 lastJob=jid;setBusy(true);
 try{while(lastJob===jid){const job=await api('/api/jobs/'+jid);$('phase').textContent=job.phase;
  if(job.status==='failed'){setBusy(false);await refresh();throw Error(job.error+(job.import_result?' Материалы уже сохранены; после исправления проблемы нажмите «Найти новости и аналитику», чтобы продолжить подготовку.':''));}
  if(job.status==='complete'){setBusy(false);await refresh();if(job.result){renderGenerated(job);$('message').textContent='Отчёт проверен и сохранён. Следующий запуск обработает следующую партию.';$('message').hidden=false;}
   else if(job.pipeline_result){const r=job.pipeline_result;await loadHistory({showLatest:r.results.some(x=>x.company_id===$('company').value&&x.status==='completed')});$('message').textContent=`Обновление завершено. Подготовлено новостей: ${r.stages.preparation?.prepared??0}. Отчётов обработано: ${r.results.length}. Ошибок: ${r.errors.length}.`;if(r.errors.length)$('message').textContent+=' '+r.errors.map(x=>x.reason).join('; ');$('message').hidden=false;}
   else if(job.completion){$('llm-result').value='';$('complete-button').disabled=true;$('message').textContent='Отчёт сохранён в истории компании. Можно обработать следующую партию.';$('message').hidden=false;}
   else if(job.workflow_status==='idle'){$('message').textContent=`Новых подходящих новостей нет. Новых проверок: ${job.workflow.new_comparisons}. История отчётов сохранена.`;$('message').hidden=false;}
   else if(job.report){renderReport(job.report,jid);$('message').textContent=job.workflow_status==='resumed'?'Эта партия уже подготовлена и ожидает результата LLM. Повторного сравнения не было.':'Новая партия подготовлена. Передайте пакет итоговой LLM и сохраните её ответ.';$('message').hidden=false;}
   else {selectedFiles=[];$('files').value='';fileList();const total=job.stages.import.changed_documents;const rejected=job.stages.import.rejected_news||[];$('message').textContent=`Готово: добавлено или обновлено — ${total}. Пропущено с ошибками — ${rejected.length}. Новых векторов — ${job.stages.embedding.computed}. Можно запускать поиск.`;if(rejected.length)$('message').textContent+=' Причины: '+rejected.map(r=>r.file+': '+r.reason).join('; ');$('message').hidden=false;$('results-title').textContent='Материалы обновлены';$('result-summary').replaceChildren();$('downloads').hidden=true;const empty=node('div',undefined,'empty');empty.append(node('div','✓','empty-icon'),node('h3','Материалы готовы'),node('p','Выберите компанию и запустите новый поиск.'));$('results').replaceChildren(empty);}$('results').classList.remove('busy');return;}
  await new Promise(r=>setTimeout(r,800));
 }}catch(e){setBusy(false);$('results').classList.remove('busy');showError(e.message);}
}
function fileList(){$('file-list').replaceChildren();for(const f of selectedFiles){const row=node('li',f.name);row.append(node('small',`${Math.ceil(f.size/1024)} КБ`));$('file-list').append(row);}$('upload-button').disabled=busy||!selectedFiles.length;}
$('company').addEventListener('change',profile);
$('llm-result').addEventListener('change',()=>{$('complete-button').disabled=busy||!$('llm-result').files.length;});
$('complete-button').addEventListener('click',async()=>{if(busy||!$('llm-result').files.length)return;clearNotice();try{const f=$('llm-result').files[0];if(f.size>25*1024*1024)throw Error('Максимальный размер ответа — 25 МБ.');const result=JSON.parse(await f.text());if(typeof result.request_id!=='string'||!result.request_id.includes('::'))throw Error('В ответе отсутствует request_id исходной партии.');const run_id=result.request_id.split('::')[0];const r=await api('/api/complete',{run_id,company_id:result.company_id,result});await follow(r.job_id);}catch(e){setBusy(false);showError(e.message);}});
$('dataset-date').addEventListener('click',()=>{if(state?.latest_news_date)$('as-of').value=state.latest_news_date;});
$('files').addEventListener('change',e=>{selectedFiles=Array.from(e.target.files);fileList();});
const zone=$('dropzone');for(const event of ['dragenter','dragover'])zone.addEventListener(event,e=>{e.preventDefault();zone.classList.add('drag');});for(const event of ['dragleave','drop'])zone.addEventListener(event,e=>{e.preventDefault();zone.classList.remove('drag');});zone.addEventListener('drop',e=>{if(busy)return;selectedFiles=Array.from(e.dataTransfer.files);fileList();});
$('search-form').addEventListener('submit',async e=>{e.preventDefault();if(busy)return;clearNotice();setBusy(true);$('phase').textContent='Начинаем обработку…';$('results').classList.add('busy');try{const r=await api($('generate-report').checked?'/api/generate':'/api/search',{company_id:$('company').value,as_of:$('as-of').value,top_news:Number($('top-news').value)});await follow(r.job_id);}catch(err){setBusy(false);$('results').classList.remove('busy');showError(err.message);}});
$('poll-button').addEventListener('click',async()=>{if(busy)return;clearNotice();setBusy(true);try{const r=await api('/api/tick',{});await follow(r.job_id);}catch(e){setBusy(false);showError(e.message);}});
$('auto-toggle').addEventListener('click',async()=>{clearNotice();$('auto-toggle').disabled=true;try{await api('/api/auto-poll',{enabled:!state.integration.auto_poll});await refresh();}catch(e){showError(e.message);renderAutomation();}});
$('upload-button').addEventListener('click',async()=>{if(busy||!selectedFiles.length)return;clearNotice();setBusy(true);$('phase').textContent='Читаем выбранные файлы…';try{const bytes=selectedFiles.reduce((n,f)=>n+f.size,0);if(bytes>25*1024*1024)throw Error('Выберите файлы общим размером до 25 МБ.');const files=await Promise.all(selectedFiles.map(async f=>({name:f.name,content:await f.text()})));const r=await api('/api/import',{files});await follow(r.job_id);}catch(err){setBusy(false);showError(err.message);}});
async function watchServer(){
 try{
  if(!busy&&!document.hidden){
   const update=await api('/api/state'),job=update.active_job,key=job?job.id+':'+job.status:'';
   if(!state){await refresh();return;}state.integration=update.integration;state.active_job=update.active_job;renderAutomation();
   if(job?.status==='running')await follow(job.id);
   else if(key!==watchedJob){watchedJob=key;await loadHistory({showLatest:!currentView||currentView.type==='report'});}
  }
 }catch{/* Preserve the readable report while the server restarts. */}
 finally{setTimeout(watchServer,15000);}
}
(async()=>{try{await refresh();if(state.active_job?.status==='running')await follow(state.active_job.id);else if(state.active_job?.result)renderGenerated(state.active_job);else if(state.active_job?.report)renderReport(state.active_job.report,state.active_job.id);}catch(e){showError(e.message);}finally{setTimeout(watchServer,15000);}})();
