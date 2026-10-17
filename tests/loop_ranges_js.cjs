const fs = require('fs');
const vm = require('vm');
const assert = require('assert/strict');
const path = require('path');
const source = fs.readFileSync(path.join(__dirname,'../static/js/score.js'),'utf8');
async function run(state, responses, stored, leaving) {
  let calls=0, reloads=0, timers=[], saved=new Map(stored || []);
  const box={dataset:{auto:'1',state,since:'2026-10-17 14:42:00',start:'2026-10-01',end:'2026-10-17'},textContent:''};
  const location={pathname:'/score',search:'?start=2026-07-01&end=2026-09-30&q=凯马&theater=华东战区&region=x&dq=y',reload(){reloads++;}};
  const original=location.search;
  let submit;
  const context={document:{getElementById:id=>id==='llm-status'?box:null,addEventListener:(name,fn)=>{if(name==='submit')submit=fn;}},location,window:{location,addEventListener(){}},sessionStorage:{getItem:k=>saved.get(k)??null,setItem:(k,v)=>saved.set(k,v)},AbortSignal:{timeout(){}},setTimeout:f=>{timers.push(f);return timers.length;},clearTimeout(){},encodeURIComponent,
    fetch:async()=>{const data=responses[Math.min(calls++,responses.length-1)];if(data instanceof Error)throw data;return {ok:!data.http,status:data.http||200,json:async()=>data};}};
  vm.runInNewContext(source,context);
  if (leaving) submit();
  async function settle(){for(let i=0;i<12;i++)await Promise.resolve();}
  await settle();
  let guard=0;
  while(timers.length && guard++<150){timers.shift()();await settle();}
  assert(guard<150,'unbounded polling');assert.equal(location.search,original,'query lost');
  return {calls,reloads,saved};
}
(async()=>{
  assert.equal((await run('done',[{status:'done',updated_at:'different'}])).calls,0);
  assert.equal((await run('error',[{status:'error'}])).calls,0);
  let finished=await run('running',[{status:'running'},{status:'done',updated_at:'2026-10-17 14:42:01'}]);
  assert.equal(finished.calls,2);assert.equal(finished.reloads,1);
  assert.equal((await run('running',[{status:'done'}],finished.saved)).reloads,0);
  assert.equal((await run('running',[{http:401}])).calls,1);
  assert.equal((await run('running',[new Error('offline')])).calls,3);
  assert.equal((await run('running',[{status:'running'}])).calls,120);
  assert.equal((await run('running',[{status:'idle'}])).reloads,0);
  assert.equal((await run('running',[{status:'done'}],null,true)).reloads,0,'late poll overrode a GET submit');
  const c={document:{readyState:'loading',addEventListener(){}},window:{},Date};
  vm.runInNewContext(fs.readFileSync(path.join(__dirname,'../static/js/range-picker.js'),'utf8'),c);
  const error=c.window.StcRangePicker.error;
  assert.equal(error('2026-07-01','2026-09-30','2026-10-17',false),'');
  for(const a of ['bad','2026-02-30','20260701','2025-12-31','2026-10-18'])assert(error(a,'2026-10-17','2026-10-17',false));
  assert(error('2026-01-01','2027-01-02','2027-01-02',false));
  assert.equal(error('','','2026-10-17',true),'');
  console.log('9 polling cases and range validation passed');
})().catch(e=>{console.error(e);process.exit(1);});
