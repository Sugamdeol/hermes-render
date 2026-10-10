const fs=require('node:fs'),assert=require('node:assert/strict');
const {chromium}=require('playwright');
(async()=>{
 const browser=await chromium.launch({headless:true});
 try {
  const source=fs.readFileSync(process.argv[2],'utf8');
  const css=fs.readFileSync(process.argv[3],'utf8');
  const reveal=source.slice(source.indexOf('function observeAnswerBlocks('),source.indexOf('  function MessageViewBase('));
  for (const width of [390,1440]) {
   const page=await browser.newPage({viewport:{width,height:800},reducedMotion:'reduce'});
   await page.setContent(`<style>html,body{margin:0;height:100%;background:#071616}.hcd{height:100vh} ${css}</style><div class="hcd"><div class="hcd-main"><div class="hcd-topbar"><b>Hermes</b><div class="hcd-topbar-right"><button>Follow reply</button><button>Details</button></div></div><div class="hcd-chat"><div class="hcd-scroll reader-scroll-view" tabindex="0" role="region" aria-label="Conversation"><article class="hcd-message hcd-assistant"><div class="hcd-bubble"><div class="hcd-markdown answer-markdown" id="answer"><p>Explore with Hermes.</p></div><div class="hcd-actions"><button>Copy</button></div></div></article></div><div class="hcd-composer"><textarea aria-label="Message">Explain chemical equilibrium</textarea><div class="hcd-composer-foot"><button>Send</button></div></div></div></div></div>`);
   await page.addScriptTag({content:reveal+';window.cleanup=observeAnswerBlocks(document.querySelector("#answer"));'});
   await page.waitForFunction(()=>document.querySelector('#answer p').dataset.answerReveal==='visible');
   assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),true);
   assert.equal(await page.locator('#answer p').evaluate(e=>getComputedStyle(e).opacity),'1');
   await page.evaluate(()=>{const p=document.createElement('p');p.textContent='Another block';document.querySelector('#answer').append(p)});
   await page.waitForFunction(()=>document.querySelector('#answer p:last-child').dataset.answerReveal==='visible');
   await page.evaluate(()=>window.cleanup());
   assert.equal(await page.locator('[data-answer-reveal]').count(),0);
   await page.close();
  }
  console.log('OpenIntelligentUI: mobile/desktop overflow, visible answers, new blocks, reduced motion and observer cleanup passed');
 } finally {await browser.close()}
})().catch(e=>{console.error(e);process.exit(1)});
