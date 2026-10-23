// Browser check of the Open Source Village calendar (infra/apps/calendar): headless Chromium
// (Playwright's build in ~/.cache/ms-playwright) signs in with a Keycast bunker URL, optionally
// creates a calendar (CALENDAR=<name>) and an event (CREATE=1 TITLE=<title>), and prints the
// relay frames (OK/CLOSED/AUTH) and console errors. Screenshots: <out prefix>-*.png.
// Usage: node calendar-check.mjs <app url> <file with a bunker URL> <out prefix>
import { spawn } from 'node:child_process';
import { readFileSync, writeFileSync } from 'node:fs';
const [url, bunkerFile, out] = process.argv.slice(2);
const bunker = readFileSync(bunkerFile, 'utf8').trim();
const chrome = process.env.CHROME || process.env.HOME + '/.cache/ms-playwright/chromium-1223/chrome-linux64/chrome';
const port = 9344;
const p = spawn(chrome, ['--headless=new','--no-sandbox','--hide-scrollbars','--window-size=1280,900',`--remote-debugging-port=${port}`,'about:blank'], {stdio:'ignore'});
const sleep = ms => new Promise(r => setTimeout(r, ms));
let targets; for (let i=0;i<50;i++){ try { targets = await (await fetch(`http://127.0.0.1:${port}/json`)).json(); break; } catch { await sleep(200);} }
const ws = new WebSocket(targets.find(x=>x.type==='page').webSocketDebuggerUrl);
await new Promise(r=>ws.onopen=r);
let id=0; const pending={}; const logs=[];
ws.onmessage = e => { const m=JSON.parse(e.data); if(m.id && pending[m.id]) { pending[m.id](m); delete pending[m.id]; }
  if(m.method==='Runtime.exceptionThrown') logs.push('EXC '+(m.params.exceptionDetails.exception?.description||m.params.exceptionDetails.text).slice(0,300));
  if(m.method==='Runtime.consoleAPICalled' && ['error','warning'].includes(m.params.type)) logs.push(m.params.type+' '+JSON.stringify(m.params.args.map(a=>a.value||a.description)).slice(0,300)); };
const frames=[];
const sendS=(sessionId,method,params={})=>new Promise(r=>{ const i=++id; pending[i]=r; ws.send(JSON.stringify({id:i,method,params,sessionId})); });
ws.addEventListener('message', async e => { const m=JSON.parse(e.data);
  if (m.method==='Target.attachedToTarget') { const sid=m.params.sessionId; await sendS(sid,'Network.enable'); await sendS(sid,'Runtime.runIfWaitingForDebugger'); }
  if (m.method==='Target.targetCreated' && /worker/.test(m.params.targetInfo.type)) { await send('Target.attachToTarget',{targetId:m.params.targetInfo.targetId, flatten:true}); }
  if (m.method==='Network.webSocketFrameSent' || m.method==='Network.webSocketFrameReceived') { const d=m.params.response.payloadData; if (!/^\["(REQ|CLOSE)"/.test(d) || process.env.ALLFRAMES) frames.push((m.method.endsWith('Sent')?'> ':'< ')+d.slice(0,220)); }
  if (m.method==='Network.webSocketCreated') frames.push('WS '+m.params.url); });
const send=(method,params={})=>new Promise(r=>{ const i=++id; pending[i]=r; ws.send(JSON.stringify({id:i,method,params})); });
const ev = async (expr) => (await send('Runtime.evaluate',{expression: expr, returnByValue:true, awaitPromise:true})).result?.result?.value;
const shot = async (name) => { const r = await send('Page.captureScreenshot',{format:'png'}); writeFileSync(`${out}-${name}.png`, Buffer.from(r.result.data,'base64')); };
const clickText = (t) => ev(`(() => { const el=[...document.querySelectorAll('button,[role=button],a,li,div')].reverse().find(e=>e.textContent.trim().startsWith(${JSON.stringify(t)}) && e.offsetParent); if(!el) return false; el.click(); return true; })()`);
await send('Page.enable'); await send('Runtime.enable'); await send('Network.enable');
await send('Target.setDiscoverTargets',{discover:true});
await send('Target.setAutoAttach',{autoAttach:true, waitForDebuggerOnStart:false, flatten:true});
await send('Page.navigate',{url}); await sleep(5000);
console.log('open remote signer:', await clickText('Connect with Remote Signer'));
await sleep(800);
// type the bunker URL into the first text input of the panel (React-controlled: use the native setter)
console.log('fill:', await ev(`(() => { const i=[...document.querySelectorAll('input,textarea')].find(x=>x.offsetParent && /bunker/i.test((x.placeholder||'')+(x.name||'')+(x.getAttribute('aria-label')||''))) || [...document.querySelectorAll('input')].find(x=>x.offsetParent); if(!i) return 'no input'; const set=Object.getOwnPropertyDescriptor(Object.getPrototypeOf(i),'value').set; set.call(i, ${JSON.stringify(bunker)}); i.dispatchEvent(new Event('input',{bubbles:true})); return i.placeholder||'input'; })()`));
await shot('1-filled');
console.log('connect:', await ev(`(() => { const b=[...document.querySelectorAll('button')].filter(b=>b.offsetParent && /^(connect|verbinden)$/i.test(b.textContent.trim())); if(!b.length) return [...document.querySelectorAll('button')].filter(b=>b.offsetParent).map(b=>b.textContent.trim()).join('|'); b[b.length-1].click(); return 'clicked'; })()`));
await sleep(Number(process.env.WAIT || 15000));
await shot('2-after-login');
console.log('dialog still open:', await ev(`!!document.querySelector('[role=dialog]')`));
const fill = (sel, val) => ev(`(() => { const i=[...document.querySelectorAll(${JSON.stringify(sel)})].find(x=>x.offsetParent); if(!i) return false; const set=Object.getOwnPropertyDescriptor(Object.getPrototypeOf(i),'value').set; set.call(i, ${JSON.stringify(val)}); i.dispatchEvent(new Event('input',{bubbles:true})); i.dispatchEvent(new Event('change',{bubbles:true})); return true; })()`);
if (process.env.CALENDAR) {
  console.log('create calendar:', await clickText('Create Calendar')); await sleep(1200); await shot('2b-calform');
  console.log('cal inputs:', await ev(`[...document.querySelectorAll('input,textarea,button')].filter(x=>x.offsetParent && x.closest('[role=dialog]')).map(x=>x.tagName+':'+(x.placeholder||x.getAttribute('aria-label')||x.textContent.trim()).slice(0,40)).join(' | ')`));
  console.log('name:', await fill('[role=dialog] input', process.env.CALENDAR));
  console.log('save:', await ev(`(() => { const b=[...document.querySelectorAll('[role=dialog] button')].filter(b=>/^(create|save)/i.test(b.textContent.trim())); if(!b.length) return 'none'; b[b.length-1].click(); return b[b.length-1].textContent.trim(); })()`));
  await sleep(8000); await shot('2c-calendar');
}
if (process.env.CREATE) {
  console.log('new event:', await clickText('+ New event')); await sleep(1500); await shot('3-form');
  if (process.env.TITLE) {
    console.log('title:', await fill('input[placeholder="Enter event title"]', process.env.TITLE));
    console.log('save event:', await ev(`(() => { const b=[...document.querySelectorAll('button')].find(b=>b.offsetParent && b.textContent.trim()==='Save Event'); if(!b) return 'none'; b.click(); return 'clicked'; })()`));
    await sleep(10000); await shot('4-saved');
  }
  console.log('inputs:', await ev(`[...document.querySelectorAll('input,textarea,[role=checkbox],[role=switch],button')].filter(x=>x.offsetParent && x.closest('[role=dialog],form,.MuiDrawer-root,.MuiModal-root')).map(x=>x.tagName+':'+(x.placeholder||x.getAttribute('aria-label')||x.name||x.textContent.trim()).slice(0,40)).join(' | ')`));
}
console.log('frames:\n'+frames.filter(f=>!/"EOSE"/.test(f)).slice(-40).join('\n'));
console.log('logs:', logs.slice(-12).join('\n'));
ws.close(); p.kill();
