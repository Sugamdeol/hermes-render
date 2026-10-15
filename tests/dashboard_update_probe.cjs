const fs=require('node:fs'),vm=require('node:vm'),assert=require('node:assert/strict');
const source=fs.readFileSync(process.argv[2],'utf8');
const component=source.slice(source.indexOf('  function AgentUpdatePanel('), source.indexOf('  function SettingsModal('));
function mount({state={available:true,phase:'idle'},pending=false,confirming=false,fail=false}={}) {
 let n=0,calls=0,resolve; const states=[state,'',pending,confirming];
 const waiting=new Promise(r=>resolve=r);const changes=[];
 const ctx={BASE:'/api/plugins/hermes-chat-dashboard',str:(v,d='')=>v==null?d:String(v),
  useState:()=>{const i=n++;return [states[i],v=>changes.push([i,v])]},useRef:v=>({current:v}),useEffect:()=>{},
  h:(type,props,...children)=>({type,props:props||{},children}),
  fetchJSON:async(url,options)=>{assert.equal(typeof options.body,'string','SDK body must be serialized JSON');assert.equal(options.headers['Content-Type'],'application/json');assert.equal(typeof JSON.parse(options.body),'object');calls++;ctx.sent=options;await waiting;if(fail)throw Error('Request refused');return {available:true,phase:'queued'}}};
 vm.createContext(ctx);vm.runInContext(component+';this.panel=AgentUpdatePanel;',ctx);
 const tree=ctx.panel();const nodes=[];function visit(x){if(!x||typeof x!=='object')return;if(Array.isArray(x))return x.forEach(visit);nodes.push(x);x.children.forEach(visit)}visit(tree);
 return {ctx,changes,nodes,resolve,calls:()=>calls,button:text=>nodes.find(x=>x.type==='button'&&x.children[0]===text)};
}
(async()=>{
 const unavailable=mount({state:{available:false,phase:'unavailable'}});assert.equal(unavailable.button('Check for updates').props.disabled,true);
 const busy=mount({state:{available:true,phase:'running'}});assert.equal(busy.button('Working…').props.disabled,true);
 const checked={available:true,phase:'succeeded',action:'check',result:{current:'b'.repeat(40),target:'a'.repeat(40),update_available:true}};
 const normal=mount({state:checked});normal.button('Update Hermes').props.onClick();assert.equal(normal.calls(),0);assert.equal(normal.changes.at(-1)[1],true);
 const confirmed=mount({state:checked,confirming:true});const first=confirmed.button('Back up and update').props.onClick();await confirmed.button('Back up and update').props.onClick();assert.equal(confirmed.calls(),1);assert.equal(JSON.parse(confirmed.ctx.sent.body).ref,'a'.repeat(40));assert.equal(confirmed.ctx.sent.headers['X-Hermes-Update'],'1');assert.equal(confirmed.ctx.sent.headers['Content-Type'],'application/json');confirmed.resolve();await first;
 const failure=mount({fail:true});const request=failure.button('Check for updates').props.onClick();failure.resolve();await request;assert(failure.changes.some(([i,v])=>i===1&&v==='Request refused'));
 assert(source.includes('h(AgentUpdatePanel)'));
 console.log('Dashboard update: unavailable, busy, confirmation, exact commit, duplicate clicks and failure feedback passed');
})().catch(e=>{console.error(e);process.exit(1)});
