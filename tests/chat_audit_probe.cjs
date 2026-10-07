const fs=require('node:fs'),vm=require('node:vm'),assert=require('node:assert/strict');
const source=fs.readFileSync(process.argv[2],'utf8');
const context={str:x=>String(x??''),esc:x=>String(x).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;'),setTimeout,clearTimeout,setInterval,clearInterval};
vm.createContext(context);
const md=source.slice(source.indexOf('  function inlineMd('),source.indexOf('  function renderMarkdown('));
vm.runInContext(md,context);
assert.equal((context.inlineMd('[site](https://example.com)').match(/<a /g)||[]).length,1);
assert.equal((context.inlineMd('`https://example.com`').match(/<a /g)||[]).length,0);
assert.equal((context.inlineMd('<img src=x onerror=alert(1)>').match(/<img/g)||[]).length,0);
assert.equal(context.highlightCode('<'.repeat(50000),'js'),'&lt;'.repeat(50000));
let gateway=source.slice(source.indexOf('  class Gateway {'),source.indexOf('  class ErrorBoundary',source.indexOf('  class Gateway {')));
vm.runInContext(gateway+'\nthis.Gateway=Gateway;',context);
(async()=>{
 const gw=new context.Gateway();gw.ws={readyState:1,send(){throw new Error('closed while sending');}};
 await assert.rejects(gw.request('session.status',{},60000),/closed while sending/);
 assert.equal(gw.pending.size,0);
 console.log('Markdown links, HTML escaping, large code and failed WebSocket send checks passed');
})();
