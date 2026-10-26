/* Async company onboarding contract; fake jobs and clock, no server or paid API. */
const fs=require('node:fs'),vm=require('node:vm'),assert=require('node:assert/strict');
const source=fs.readFileSync(require('node:path').join(__dirname,'../src/integration/web_static/site-adapter.js'),'utf8');
function harness({failure=false,enabled=true}={}) {
  let polls=0,posts=0;const saved=new Map();
  const state={companies:[{id:'U_1'}],today:'2026-10-25',settings:{},integration:{selected_company_id:'U_1',can_add_company:enabled}};
  const timers=[];
  const ctx={URL,Intl,Date,AbortSignal,document:{hidden:false,addEventListener(){}},
    sessionStorage:{getItem:k=>saved.get(k)||null,setItem:(k,v)=>saved.set(k,v),removeItem:k=>saved.delete(k)},
    window:{addEventListener(){}},setTimeout:(fn,ms)=>{timers.push(fn);return 1;},clearTimeout(){},
    async fetch(url,options) {
      let body;
      if(url==='/api/state') body=state;
      else if(url==='/api/select-company') body={selected_company_id:JSON.parse(options.body).company_id};
      else if(url==='/api/add-company') {posts++;body={job_id:'job-1'};}
      else if(url==='/api/jobs/job-1') {
        polls++;
        if(polls===1) body={status:'running',phase:'Ожидаем завершения отчёта'};
        else if(failure) body={status:'failed',error:'Компания не найдена в CSV'};
        else {state.companies.push({id:'U_2'});body={status:'complete',company_result:{company_id:'U_2'}};}
      } else {
        const id=new URL(url,'http://localhost').searchParams.get('company');
        body={company:{id,profile:'Test',facts:{},payments:{incoming:[],outgoing:[]}},today:'2026-10-25',news:[],history:[]};
      }
      return {ok:true,json:async()=>structuredClone(body)};
    }};
  vm.createContext(ctx);vm.runInContext(source,ctx);
  return {adapter:ctx.window.SiteAdapter,saved,timers,get posts(){return posts;}};
}
async function flush() {await new Promise(setImmediate);}
(async()=>{
  const h=harness(),api=await h.adapter.init(),phases=[];
  const task=api.addCompany('2',phase=>phases.push(phase));
  await flush();assert.equal(api.pendingCompany(),'job-1');assert.equal(api.company.id,'U_1');
  h.timers.shift()();assert.equal(await task,'U_2');assert.equal(api.pendingCompany(),null);
  await api.selectCompany('U_2');assert.equal(api.company.id,'U_2');
  assert.deepEqual(Array.from(api.roles,x=>x.key),['U_1','U_2']);assert.equal(h.posts,1);
  assert.equal(phases[0],'Ожидаем завершения отчёта');

  const resumed=harness(),recovery=await resumed.adapter.init();
  const job=recovery.addCompany('',()=>{},'job-1');await flush();resumed.timers.shift()();
  assert.equal(await job,'U_2');assert.equal(resumed.posts,0);

  const failed=harness({failure:true}),retry=await failed.adapter.init();
  const rejected=retry.addCompany('999');const rejection=assert.rejects(rejected,/не найдена/);
  await flush();failed.timers.shift()();await rejection;assert.equal(retry.pendingCompany(),null);
  assert.equal(retry.company.id,'U_1');

  const old=harness({enabled:false}),legacy=await old.adapter.init();
  await assert.rejects(legacy.addCompany('2'),/перезапуска/);assert.equal(old.posts,0);
  console.log('Company onboarding: progress, resume without duplicate POST, failure, selection and old-server compatibility passed.');
})().catch(error=>{console.error(error);process.exitCode=1;});
