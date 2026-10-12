const fs=require('fs'),vm=require('vm'),assert=require('assert');
const source=fs.readFileSync(process.argv[2],'utf8');
const start=source.indexOf('    const refreshSessions = useCallback(');
const end=source.indexOf('    }, [query, sessionSource]);',start)+'    }, [query, sessionSource]);'.length;
let rows=Array.from({length:170},(_,i)=>({id:String(i),title:'old'})), loading=[],offset=[],more=[],errors=[],calls=[];
const ctx={query:'',sessionSource:'',BASE:'/api',sessionListRequest:{current:0},sessionListBusy:{current:false},
useCallback:f=>f,num:Number,str:String,fetchJSON:url=>new Promise((resolve,reject)=>calls.push({url,resolve,reject})),
setSessions:f=>{rows=f(rows)},setSessionsLoading:v=>loading.push(v),setSessionsError:v=>errors.push(v),setSessionsOffset:v=>offset.push(v),setHasMoreSessions:v=>more.push(v)};
vm.createContext(ctx);vm.runInContext(source.slice(start,end)+'\nglobalThis.refresh=refreshSessions;',ctx);
(async()=>{
let run=ctx.refresh({silent:true});calls.shift().resolve({sessions:Array.from({length:120},(_,i)=>({id:String(i),title:'updated'})),next_offset:120,has_more:true});await run;
assert.equal(rows.length,170);assert.equal(rows[0].title,'updated');assert.equal(rows[169].id,'169');assert.equal(loading.length,0);assert.equal(offset.length,0);assert.equal(more.length,0);
run=ctx.refresh({});const foreground=calls.shift();await ctx.refresh({silent:true});assert.equal(calls.length,0);foreground.resolve({sessions:[{id:'fresh'}],next_offset:120,has_more:false});await run;assert.equal(rows[0].id,'fresh');assert.equal(loading.at(-1),false);
const quiet=ctx.refresh({silent:true}),old=calls.shift();run=ctx.refresh({q:'new'});const latest=calls.shift();latest.resolve({sessions:[{id:'new'}]});await run;old.resolve({sessions:[{id:'stale'}]});await quiet;assert.equal(rows[0].id,'new');
run=ctx.refresh({silent:true});calls.shift().reject(new Error('offline'));await run;assert.equal(rows[0].id,'new');assert.equal(errors.at(-1),'');
console.log('Session refresh: paging preserved, no spinner, overlap skipped, stale reply discarded, failure preserves history');
})().catch(e=>{console.error(e);process.exit(1)});
