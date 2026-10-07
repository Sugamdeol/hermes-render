const fs=require('node:fs'),vm=require('node:vm'),assert=require('node:assert/strict');
const source=fs.readFileSync(process.argv[2],'utf8');
const helper=source.slice(source.indexOf('  function modelSwitchInput('),source.indexOf('  function ModelPicker('));
const change=source.slice(source.indexOf('    const changeModel = useCallback'),source.indexOf('    const changePinnedModels'));
function mount({busy=false,sid='chat1',failure=null}={}) {
 const calls=[], notices=[];
 const ctx={useCallback:f=>f,modelValue:'old',generating:busy,caps:{models:[{id:'custom:new:free',model:'new:free',provider:'custom'}]},toast:x=>notices.push(x),str:String,
 selRef:{current:{model:'old'}},pendingModelRef:{current:null},gwSidRef:{current:sid},setModelValue:x=>ctx.value=x,setSelected:f=>ctx.selected=f(ctx.selected),selected:{model:'old'},
 gwRef:{current:{request:async (method,params)=>{calls.push({method,params});if(failure)throw Error(failure);return {};}}}};
 vm.createContext(ctx);vm.runInContext(helper+change+'\nthis.change=changeModel;',ctx);return {ctx,calls,notices};
}
(async()=>{
 const good=mount();await good.ctx.change('custom:new:free');assert.equal(good.calls[0].params.value,'new:free --provider custom');assert.equal(good.calls[0].params.session_id,'chat1');assert.equal(good.ctx.selected.model,'custom:new:free');
 const busy=mount({busy:true});await busy.ctx.change('custom:new:free');assert.equal(busy.calls.length,0);assert.equal(busy.ctx.pendingModelRef.current,'custom:new:free');assert.equal(busy.ctx.selected.model,'old');
 const detached=mount({sid:null});await detached.ctx.change('custom:new:free');assert.equal(detached.calls.length,0);
 const rejected=mount({failure:'Invalid provider'});await rejected.ctx.change('custom:new:free');assert.equal(rejected.ctx.value,'old');assert.equal(rejected.ctx.selected.model,'old');assert.equal(rejected.ctx.pendingModelRef.current,null);
 const racing=mount({failure:'4009 session busy'});await racing.ctx.change('custom:new:free');assert.equal(racing.ctx.pendingModelRef.current,'custom:new:free');
 console.log('Model switching: provider routing, active session, busy queue, detached chat and errors passed');
})();
