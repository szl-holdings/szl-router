// SPDX-License-Identifier: Apache-2.0
// Local-source browser qualification. Provider APIs use synthetic fixtures;
// no completion, paid inference, provider activation, or external verification.
const assert = require('node:assert/strict');
const {mkdirSync,readFileSync,writeFileSync}=require('node:fs');
const {resolve}=require('node:path');
const {spawn}=require('node:child_process');
const {createHash}=require('node:crypto');
const {chromium}=require('playwright');
const root=resolve(__dirname,'..');
const output=resolve(process.env.ROUTER_BROWSER_EVIDENCE || '/tmp/szl-router-browser');
mkdirSync(output,{recursive:true});
const origins={public:'http://127.0.0.1:7892',control:'http://127.0.0.1:7893'};
const results=[],observations=[],children=[];
const sha=bytes=>createHash('sha256').update(bytes).digest('hex');
function check(name,ok,detail=null){results.push({name,ok:Boolean(ok),detail});}
async function preview(kind){
 const env={...process.env};for(const k of Object.keys(env))if(k.startsWith('SZL_ROUTER_'))delete env[k];
 env.SZL_ROUTER_ENABLE_EGRESS='0';env.SOURCE_REVISION='UNAVAILABLE';
 const args=kind==='public'?['-c','import functools; from pathlib import Path; from http.server import ThreadingHTTPServer; from server import HardenedHandler; ThreadingHTTPServer(("127.0.0.1",7892),functools.partial(HardenedHandler,directory=str(Path.cwd()))).serve_forever()']:['-m','uvicorn','router_control.app:app','--host','127.0.0.1','--port','7893','--no-access-log'];
 const child=spawn(process.env.ROUTER_TEST_PYTHON || 'python',args,{cwd:kind==='public'?resolve(root,'space'):root,env,stdio:['ignore','pipe','pipe']});children.push(child);let diagnostic='';
 child.stdout.on('data',b=>{diagnostic+=b;});child.stderr.on('data',b=>{diagnostic+=b;});
 for(let n=0;n<120;n++){if(child.exitCode!==null)throw new Error(`Local ${kind} preview exited: ${diagnostic}`);try{const r=await fetch(origins[kind],{signal:AbortSignal.timeout(1000)});if(r.ok)return;}catch{}await new Promise(r=>setTimeout(r,100));}
 throw new Error(`Local ${kind} preview unavailable: ${diagnostic}`);
}
async function fixture(page,kind,mode='normal'){
 const writes=[],external=[],errors=[];page.on('pageerror',e=>errors.push(e.message));
 await page.route('**/*',async route=>{
  const req=route.request(),u=new URL(req.url());
  if(u.origin!==origins[kind]){external.push(u.origin+u.pathname);return route.abort();}
  if(!['GET','HEAD'].includes(req.method())){
   writes.push({method:req.method(),path:u.pathname});
   if(kind==='control'&&u.pathname==='/api/plan')return route.fulfill({contentType:'application/json',body:JSON.stringify({selected:'fixture-provider',candidates:[{provider_id:'fixture-provider',public_model:'fixture-model',upstream_model:'fixture-upstream',credential_state:'UNAVAILABLE',sovereignty:2,priority:1,cost_tier:0}],receipt:{digest:'a'.repeat(64)}})});
   return route.abort();
  }
  if(u.pathname.startsWith('/api/')||u.pathname==='/readyz'||u.pathname.startsWith('/.well-known/')){
   if(mode==='unavailable')return route.fulfill({status:503,contentType:'application/json',body:'{"state":"UNAVAILABLE"}'});
   if(kind==='public'){
    const name=u.pathname.split('/').pop();
    if(['health','models','provenance'].includes(name)){
     const data=JSON.parse(readFileSync(resolve(root,`space/assets/snapshot-router-${name}.json`),'utf8'));
     data.captured_at=mode==='stale'?'2000-01-01T00:00:00Z':new Date().toISOString();
     return route.fulfill({contentType:'application/json',headers:{'X-SZL-Evidence-State':'SNAPSHOT'},body:JSON.stringify(data)});
    }
    return route.fulfill({status:503,contentType:'application/json',body:'{"state":"UNAVAILABLE"}'});
   }
   const data=u.pathname==='/api/routes'?{state:'VALIDATED',egress_enabled:false,providers:[{id:'fixture-provider',models:['fixture-model'],sovereignty:2,priority:1,cost_tier:0,classifications:['public'],enabled:false,credential_state:'UNAVAILABLE'}]}:
    u.pathname==='/api/source'?{revision:'UNAVAILABLE',fixture:true,controlled_files:{}}:
    u.pathname==='/readyz'?{status:'ready',inference:{ready_for_requests:false,basis:'LOCAL_CONFIGURATION_ONLY',checks:{egress:false}}}:
    u.pathname==='/api/local-models'?{observation_state:'NO_PROBE',providers:[]}:{state:'UNAVAILABLE'};
   return route.fulfill({contentType:'application/json',body:JSON.stringify(data)});
  }
  if(mode==='unavailable'&&u.pathname.includes('snapshot-router-'))return route.fulfill({status:503,body:'unavailable fixture'});
  return route.continue();
 });return {writes,external,errors};
}

async function inspect(page, {contrast = true, targets = true} = {}) {
  return page.evaluate(({contrast,targets}) => {
    const viewport = innerWidth;
    const overflowingText = [], contrastFailures = [], targetFailures = [];
    const context = document.createElement('canvas').getContext('2d', {willReadFrequently:true});
    context.canvas.width=1; context.canvas.height=1;
    const colorCache=new Map(),groundCache=new WeakMap(),styleCache=new WeakMap();
    function style(el) {if(!styleCache.has(el))styleCache.set(el,getComputedStyle(el));return styleCache.get(el);}
    function rgba(color) {if(!colorCache.has(color)){context.clearRect(0,0,1,1);context.fillStyle=color;context.fillRect(0,0,1,1);colorCache.set(color,[...context.getImageData(0,0,1,1).data].map((x,i)=>i===3?x/255:x));}return colorCache.get(color);}
    function over(f,b) { const a=f[3]+b[3]*(1-f[3]);return [0,1,2].map(i=>a?(f[i]*f[3]+b[i]*b[3]*(1-f[3]))/a:0).concat(a); }
    function ground(el) {if(!groundCache.has(el))groundCache.set(el,over(rgba(style(el).backgroundColor),el.parentElement?ground(el.parentElement):[255,255,255,1]));return groundCache.get(el);}
    function luminance(c) {return c.slice(0,3).map(v=>v/255).map(v=>v<=.04045?v/12.92:((v+.055)/1.055)**2.4).reduce((sum,v,i)=>sum+v*[.2126,.7152,.0722][i],0);}
    function visible(el) {return el.checkVisibility({checkOpacity:true,checkVisibilityCSS:true}) && !el.closest('[hidden],[aria-hidden="true"]');}
    const walker=document.createTreeWalker(document.body,NodeFilter.SHOW_TEXT);let node;let lowestContrast=Infinity,textNodes=0;
    while((node=walker.nextNode())) {
      const el=node.parentElement;
      if(!node.textContent.trim() || !el || el.closest('script,style,svg,.skip-link,.skip,.sr-only') || !visible(el))continue;
      const range=document.createRange();range.selectNodeContents(node);
      const rect=range.getBoundingClientRect();if(!rect.width||!rect.height)continue;
      textNodes++;
      if(rect.left < -2 || rect.right > viewport+2)overflowingText.push({text:node.textContent.trim().slice(0,90),class:el.className,box:rect.toJSON()});
      if(contrast) {
        const s=style(el), b=ground(el), f=over(rgba(s.color),b);
        const [a,z]=[luminance(f),luminance(b)].sort((a,b)=>b-a),ratio=(a+.05)/(z+.05);
        const large=parseFloat(s.fontSize)>=24 || (parseFloat(s.fontSize)>=18.666 && parseFloat(s.fontWeight)>=700);
        lowestContrast=Math.min(lowestContrast,ratio);
        if(ratio+.005 < (large?3:4.5))contrastFailures.push({text:node.textContent.trim().slice(0,100),class:el.className,ratio,threshold:large?3:4.5,color:s.color,background:b});
      }
    }
    if(targets)for(const el of document.querySelectorAll('a,button,summary')) {
      if(!visible(el)||el.closest('.skip-link,.skip'))continue;
      const b=el.getBoundingClientRect();
      if(b.width<43.9||b.height<43.9)targetFailures.push({text:el.textContent.trim().slice(0,90),width:b.width,height:b.height});
    }
    return {viewport,scrollWidth:document.documentElement.scrollWidth,bodyOverflow:getComputedStyle(document.body).overflowX,
      overflowingText,contrastFailures,targetFailures,lowestContrast:Number.isFinite(lowestContrast)?lowestContrast:null,textNodes};
  },{contrast,targets});
}
async function main(){
 let browser;
 try{
  await Promise.all([preview('public'),preview('control')]);
  browser=await chromium.launch({executablePath:process.env.CHROME_PATH,args:['--no-sandbox','--disable-dev-shm-usage'],headless:true});
  for(const kind of ['public','control']){
   for(const spec of [{width:320},{width:375},{width:768},{width:1440},{width:1440,zoom:2},{width:1440,zoom:4},{width:375,forced:true},{width:320,js:false},...(kind==='control'?[{width:1440,print:true}]:[])]){
    const label=`${kind}-${spec.width}${spec.zoom?'-zoom'+spec.zoom:''}${spec.forced?'-forced':''}${spec.js===false?'-nojs':''}${spec.print?'-print':''}`;
    const page=await browser.newPage({viewport:{width:spec.width,height:1000},reducedMotion:'reduce',forcedColors:spec.forced?'active':'none',javaScriptEnabled:spec.js!==false});
    if(spec.print)await page.emulateMedia({media:'print'});
    const observed=await fixture(page,kind);await page.goto(origins[kind],{waitUntil:'load'});
    if(spec.js!==false)await page.waitForFunction(kind=>kind==='public'?document.querySelector('.source-label').textContent!=='connecting…':!document.querySelector('#runtime').textContent.includes('CHECKING'),kind);
    if(spec.zoom)await page.evaluate(z=>document.documentElement.style.zoom=String(z),spec.zoom);
    if(kind==='public'&&spec.js!==false)check(`${label}: snapshot limits visible`,/not measured|unmeasured/i.test(await page.locator('#data-source-note').innerText()));
    if(kind==='control'&&spec.js!==false)check(`${label}: separate configuration`,/UNAVAILABLE.*LOCAL_CONFIGURATION_ONLY/.test(await page.locator('#inference-state').innerText()));
    const geometry=await inspect(page,{contrast:!spec.forced,targets:!spec.print});observations.push({label,...geometry});
    check(`${label}: reflow`,geometry.scrollWidth<=geometry.viewport+2&&!geometry.overflowingText.length,geometry.overflowingText);
    check(`${label}: text pairs`,!geometry.contrastFailures.length,{minimum:geometry.lowestContrast,failures:geometry.contrastFailures});
    if(!spec.print)check(`${label}: targets`,!geometry.targetFailures.length,geometry.targetFailures);
    if(spec.print)check(`${label}: paper background`,await page.evaluate(()=>getComputedStyle(document.body).backgroundColor==='rgb(255, 255, 255)'));
    check(`${label}: no passive writes`,observed.writes.length===0,observed.writes);
    check(`${label}: no external requests`,observed.external.length===0,observed.external);
    check(`${label}: no page errors`,observed.errors.length===0,observed.errors);
    if(spec.width===320&&!spec.zoom&&!spec.forced){
      const links=page.locator(kind==='public'?'.topbar-nav a':'.workspace-nav a');
      check(`${label}: navigation available`,await links.count()>0&&await links.first().isVisible());
      await links.last().click();check(`${label}: section navigation`,new URL(page.url()).hash==='#source');
      if(spec.js!==false){await page.keyboard.press('Tab');check(`${label}: keyboard focus`,await page.evaluate(()=>getComputedStyle(document.activeElement).outlineStyle!=='none'));}
      await page.evaluate(()=>scrollTo(0,0));
    }
    if(spec.js!==false&&spec.width===1440&&!spec.zoom&&!spec.print){
     if(kind==='public'){
      check('public: receipt inputs named',await page.getByRole('textbox',{name:'Receipt JSON or signed envelope'}).count()===1&&await page.getByRole('textbox',{name:'Public receipt URL'}).count()===1);
      await page.locator('.szlv-go').click();check('public: empty receipt stays local',/paste a receipt/i.test(await page.locator('.szlv-out').innerText())&&observed.writes.length===0);
     }else{
      await page.locator('#model').fill('fixture-model');await page.locator('#plan-form button').click();await page.locator('#plan-state').filter({hasText:'FIRST RANKED'}).waitFor();
      check('control: explicit fixture plan',observed.writes.length===1&&observed.writes[0].path==='/api/plan');
      check('control: planning does not execute inference',await page.locator('#completion-state').innerText()==='NOT RUN'&&await page.locator('#caller-token').inputValue()==='');
      const actions=await inspect(page);check('control: populated plan reflows',!actions.overflowingText.length,actions.overflowingText);
     }
    }
    if((spec.width===1440&&!spec.zoom)||spec.width===320||spec.forced){await page.evaluate(()=>scrollTo(0,0));await page.screenshot({path:resolve(output,`${label}.png`)});}
    if(spec.width===1440&&!spec.zoom&&!spec.print){await page.locator(kind==='public'?'#verify':'#plan').scrollIntoViewIfNeeded();await page.screenshot({path:resolve(output,`${kind}-workspace.png`)});}
    await page.close();
   }
   for(const mode of ['unavailable',...(kind==='public'?['stale']:[])]){
    const page=await browser.newPage({viewport:{width:375,height:1000},reducedMotion:'reduce'});const obs=await fixture(page,kind,mode);await page.goto(origins[kind],{waitUntil:'load'});
    await page.waitForFunction(kind=>kind==='public'?document.querySelector('.source-label').textContent!=='connecting…':!document.querySelector('#runtime').textContent.includes('CHECKING'),kind);
    const label=kind==='public'?await page.locator('.source-label').innerText():await page.locator('#registry-state').innerText();
    check(`${kind}: ${mode} remains explicit`,mode==='stale'?/STALE/.test(label):/OFFLINE|UNAVAILABLE/.test(label),label);
    const geometry=await inspect(page);check(`${kind}: ${mode} reflow`,!geometry.overflowingText.length,geometry.overflowingText);check(`${kind}: ${mode} text pairs`,!geometry.contrastFailures.length,geometry.contrastFailures);check(`${kind}: ${mode} no page errors or writes`,!obs.errors.length&&!obs.writes.length,obs);
    await page.close();
   }
  }
 }finally{
  if(browser)await browser.close();for(const child of children)child.kill('SIGTERM');
  const files=['space/index.html','space/assets/style.css','space/assets/app.js','space/assets/szl_verify_widget.js','router_control/app.py','router_control/static/index.html','router_control/static/styles.css','router_control/static/app.js','router_control/static/free-options.js','router_control/static/free-options.css'];
  writeFileSync(resolve(output,'results.json'),JSON.stringify({observed_at:new Date().toISOString(),scope:'LOCAL_SOURCE_WITH_SYNTHETIC_PROVIDER_FIXTURES',limits:['Chromium only','Computed text pairs exclude decorative SVGs and do not certify all gradient pixels or WCAG conformance','No real provider or paid inference invoked; no live-deployment verdict'],files:Object.fromEntries(files.map(p=>[p,sha(readFileSync(resolve(root,p)))])),results,observations},null,2)+'\n');
 }
 const failed=results.filter(r=>!r.ok);console.log(`${results.length-failed.length}/${results.length} local browser checks passed`);if(failed.length){console.log(JSON.stringify(failed,null,2));process.exitCode=1;}
}
main().catch(e=>{console.error(e);for(const child of children)child.kill('SIGTERM');process.exitCode=1;});
