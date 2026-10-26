/* Widget polling and explicit outages, without a server or real bank requests. */
const fs=require('node:fs'),vm=require('node:vm'),assert=require('node:assert/strict');
const code=fs.readFileSync(require('node:path').join(__dirname,'../src/integration/web_static/currency-widget.js'),'utf8');
const nodes=new Map(['h-conv','conv-head','conv-tag','conv-body','conv-note','w-conv'].map(id=>[id,{setAttribute(){}}]));
const timers=new Map(),listeners={},responses=[],calls=[];let tid=0;
const document={hidden:false,getElementById:id=>nodes.get(id),addEventListener:(k,f)=>listeners[k]=f};
const context={window:{},document,Date,Intl,Map,Set,Number,String,AbortSignal,
  setTimeout(fn,ms){timers.set(++tid,{fn,ms});return tid;},clearTimeout(id){timers.delete(id);},
  async fetch(url,opts){calls.push({url,opts});const result=responses.shift();if(result instanceof Error)throw result;return {ok:true,json:async()=>result};}};
const snapshot={status:'ok',rates:[{code:'USD',sell_to_bank:'80.5',buy_from_bank:'82.7',rate_at:'2026-10-23T16:14:00Z'}],fetched_at:new Date().toISOString()};
const settle=()=>new Promise(resolve=>setImmediate(resolve));
(async()=>{
  responses.push({status:'loading',rates:[],refreshing:true});
  vm.createContext(context);vm.runInContext(code,context);await settle();
  assert.match(nodes.get('conv-note').innerHTML,/Загружаем/);
  assert.equal([...timers.values()][0].ms,2000);
  const first=[...timers.values()][0];responses.push(snapshot);first.fn();await settle();
  const html=nodes.get('conv-body').innerHTML;
  assert.ok(html.indexOf('80,50')<html.indexOf('82,70'));
  assert.match(html,/19:14 МСК/);assert.match(nodes.get('conv-note').innerHTML,/Наличные/);
  assert.match(nodes.get('conv-note').innerHTML,/https:\/\/alfabank.ru\/currency\//);
  assert.equal([...timers.values()].at(-1).ms,60000);
  responses.push(new Error('outage'));await context.window.CurrencyWidget.refresh();
  assert.match(nodes.get('conv-note').innerHTML,/последние полученные/);
  assert.match(nodes.get('conv-body').innerHTML,/80,50/);
  responses.push({...snapshot,fetched_at:'2020-01-01T00:00:00Z'});await context.window.CurrencyWidget.refresh();
  responses.push(new Error('outage'));await context.window.CurrencyWidget.refresh();
  assert.doesNotMatch(nodes.get('conv-body').innerHTML,/80,50/);
  assert.match(nodes.get('conv-note').innerHTML,/недоступны/);
  const n=calls.length;document.hidden=true;await context.window.CurrencyWidget.refresh();assert.equal(calls.length,n);
  assert.ok(calls.every(c=>c.url==='/api/currency-rates'&&c.opts.cache==='no-store'));
  console.log('Currency widget: client direction, Moscow quote time, polling, stale warning, expiry and hidden-tab pause passed.');
})().catch(e=>{console.error(e);process.exitCode=1;});
