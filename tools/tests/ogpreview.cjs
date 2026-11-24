// Previews of outside pages (Channels canvas v10, channels/og-*), demo mode: the demo stands in for the
// belfry's GET /link-preview (toolshare.example: picture; repair.example: no picture; others: nothing).
const { spawn } = require("child_process"); const fs = require("fs");
const dir = __dirname, port = 9476, web = 4476, prof = dir + "/prof-og";
fs.rmSync(prof, { recursive: true, force: true });
const srv = spawn("python3", ["-m", "http.server", String(web), "-d", process.env.DIST || process.env.HOME + "/agents/belfry-webapp/work/chat/dist"], { stdio: "ignore" });
const ch = spawn("chromium-browser", ["--headless=new", "--disable-gpu", `--remote-debugging-port=${port}`, "--user-data-dir=" + prof, "about:blank"]);
const sleep = ms => new Promise(r => setTimeout(r, ms));
let ok = 0, fail = 0; const check = (c, m) => { if (c) ok++; else { fail++; console.log("FAIL " + m); } };
(async () => {
  await sleep(800);
  let t; for (let i = 0; i < 40; i++) { try { t = await (await fetch(`http://127.0.0.1:${port}/json`)).json(); break; } catch { await sleep(300); } }
  const ws = new WebSocket(t.find(x => x.type === "page").webSocketDebuggerUrl); let id = 0; const wait = {};
  ws.onmessage = m => { const d = JSON.parse(m.data); if (wait[d.id]) { wait[d.id](d.result || d); delete wait[d.id]; } };
  await new Promise(r => ws.onopen = r);
  const send = (method, params = {}) => new Promise(r => { const i = ++id; wait[i] = r; ws.send(JSON.stringify({ id: i, method, params })); });
  const ev = async e => (await send("Runtime.evaluate", { expression: e, returnByValue: true, awaitPromise: true })).result?.value;
  const shot = async n => { const s = await send("Page.captureScreenshot", { format: "png" }); fs.writeFileSync(dir + "/" + n + ".png", Buffer.from(s.data, "base64")); };
  const text = () => ev(`document.body.innerText`);
  const open = async (hash) => { await send("Page.navigate", { url: "about:blank" }); await sleep(200); await send("Page.navigate", { url: `http://127.0.0.1:${web}/?backend=demo${hash}` }); await sleep(2200); };
  const fresh = async () => { await send("Page.navigate", { url: "about:blank" }); await sleep(300); await send("Storage.clearDataForOrigin", { origin: `http://127.0.0.1:${web}`, storageTypes: "all" }); await sleep(300); };
  const type = (sel, v) => ev(`(()=>{const i=document.querySelector(${JSON.stringify(sel)});const p=i.tagName==='TEXTAREA'?HTMLTextAreaElement.prototype:HTMLInputElement.prototype;Object.getOwnPropertyDescriptor(p,'value').set.call(i,${JSON.stringify(v)});i.dispatchEvent(new Event('input',{bubbles:true}))})()`);
  const field = label => `[...document.querySelectorAll('label.field')].find(l=>l.querySelector('span').textContent.startsWith(${JSON.stringify(label)}))`;
  const typeIn = (label, v) => ev(`(()=>{const i=${field(label)}.querySelector('input,textarea');const p=i.tagName==='TEXTAREA'?HTMLTextAreaElement.prototype:HTMLInputElement.prototype;Object.getOwnPropertyDescriptor(p,'value').set.call(i,${JSON.stringify(v)});i.dispatchEvent(new Event('input',{bubbles:true}))})()`);
  const btn = label => ev(`(()=>{const b=[...document.querySelectorAll('button')].find(b=>b.textContent.trim()===${JSON.stringify(label)});return b?(b.disabled?'disabled':'on'):'none'})()`);
  const click = label => ev(`[...document.querySelectorAll('button')].find(b=>b.textContent.trim()===${JSON.stringify(label)}).click()`);
  await send("Emulation.setDeviceMetricsOverride", { width: 390, height: 844, deviceScaleFactor: 2, mobile: true });


  const PK = "7e7e9c42a91bfef19fa929e5fda1b72e0ebc1a4c1141673e2794234d86addf4e";
  const L1 = "https://toolshare.example/stories/street-library", L2 = "https://repair.example/guide";
  const compose = (v) => type('.composer textarea', v);
  const sendIt = () => ev("document.querySelector('.composer .send').click()");
  const lastMsg = (sel) => ev(`(()=>{const m=[...document.querySelectorAll(${JSON.stringify(sel)})].at(-1);return m?{text:m.innerText,og:m.querySelectorAll('.og').length,load:m.querySelectorAll('.og.load').length,img:m.querySelectorAll('.og .og-img').length,noimg:m.querySelectorAll('.og.noimg').length,href:m.querySelector('.og')?.getAttribute('href')||''}:null})()`);
  await fresh(); await send("Page.navigate", { url: `http://127.0.0.1:${web}/?backend=demo` }); await sleep(3000);
  await type('input[type=email]', 'demo@example.org'); await ev(`document.querySelector('form').requestSubmit()`); await sleep(2500);
  await ev(`location.hash = '#/c/general'`); await sleep(1500);
  // og-composer: the preview above the field, then ✕
  await compose("This is what we could become: " + L1); await sleep(900);
  let s = await ev(`document.querySelector('.og-pre')?.innerText||''`);
  check(/How a street tool library/.test(s) && /ToolShare Journal/.test(s), "composer: the preview above the field: " + s);
  check(await ev(`!!document.querySelector('.og-pre .og-x[aria-label="Don’t show a preview"]')`), "composer: ✕ Don’t show a preview");
  await shot("og-composer");
  // og-message: sent with its preview (og-loading on the way)
  await sendIt(); await sleep(1200);
  let m = await lastMsg('.msg');
  // (v2: the sender builds it before sending; the message carries it, readers fetch nothing)
  check(m && m.load === 0, "v2: no loading box for readers (the preview comes with the message)");
  check(m && m.og === 1 && m.load === 0 && m.img === 1 && m.href === L1 && /How a street tool library/.test(m.text) && m.text.includes("toolshare.example/stories/street-library"), "og-message: the picture, site, title, description; the link stays in the text: " + JSON.stringify(m));
  check(!(await ev(`!!document.querySelector('.og-pre')`)), "composer: the preview went with the message");
  check(await ev(`(()=>{const ms=[...document.querySelectorAll('.msg')];const x=ms.find(e=>/This is what we could become/.test(e.innerText));return x?x.querySelectorAll('img').length:-1})()`) === 1, "v2: its picture shows once (in the box, not also as an attachment)");
  await shot("og-message");
  // ✕: sent without a preview, and it stays without one
  await compose("Without a box: " + L1); await sleep(900);
  await ev(`document.querySelector('.og-pre .og-x').click()`); await sleep(200);
  check(!(await ev(`!!document.querySelector('.og-pre')`)), "composer: ✕ hides the preview");
  await sendIt(); await sleep(1200);
  m = await lastMsg('.msg');
  check(m && /Without a box/.test(m.text) && m.og === 0, "✕: the message has no preview: " + JSON.stringify(m));
  // og-no-image, og-several, og-no-preview
  await compose("Two good reads: " + L2 + " and " + L1); await sleep(300); await sendIt(); await sleep(1300);
  m = await lastMsg('.msg');
  check(m && m.og === 1 && m.noimg === 1 && m.href === L2, "og-several + og-no-image: one preview, the first link's, no picture: " + JSON.stringify(m));
  await compose("Nothing here: https://nothing.example/page"); await sleep(900);
  check(!(await ev(`!!document.querySelector('.og-pre')`)), "composer: nothing found, no preview");
  await sendIt(); await sleep(1300);
  m = await lastMsg('.msg');
  check(m && m.og === 0 && m.load === 0 && /nothing\.example/.test(m.text), "og-no-preview: just the link: " + JSON.stringify(m));
  // a link to the belfry itself keeps its own card, never an outside preview
  await compose("Our channel: https://chat." + await ev("location.hostname") + "/#/c/general"); await sleep(300); await sendIt(); await sleep(1000);
  m = await lastMsg('.msg'); check(m && m.og === 0, "a belfry link: no outside preview " + JSON.stringify(m));
  // og-thread
  await ev(`(()=>{const ms=[...document.querySelectorAll('.msg')];const x=ms.find(e=>/This is what we could become/.test(e.innerText));x.dispatchEvent(new MouseEvent('contextmenu',{bubbles:true}))})()`); await sleep(400);
  const replied = await ev(`(()=>{const b=[...document.querySelectorAll('button')].find(b=>/^Reply/.test(b.textContent.trim()));if(b){b.click();return true}return false})()`); await sleep(900);
  if (replied) {
    await compose("Mine, and here's how: " + L1); await sleep(300);
    await ev(`(()=>{const c=[...document.querySelectorAll('.composer')].at(-1);c.querySelector('.send').click()})()`); await sleep(1300);
    s = await ev(`[...document.querySelectorAll('.og')].map(o=>o.getAttribute('href')).length`);
    check(s >= 1, "og-thread: the reply has its preview (" + s + " boxes)");
  } else check(false, "og-thread: couldn't open the thread's Reply");
  // og-dm
  await ev(`location.hash = '#/dm/' + encodeURIComponent('n:${PK}')`); await sleep(1200);
  await compose(L1); await sleep(900);
  check(await ev(`!!document.querySelector('.og-pre')`), "DM composer: the preview above the field");
  await sendIt(); await sleep(1500);
  m = await lastMsg('.dm');
  check(m && m.og === 1 && m.img === 1, "og-dm: the preview under the bubble: " + JSON.stringify(m));
  await shot("og-dm");
  await compose("No box please " + L1); await sleep(900);
  await ev(`document.querySelector('.og-pre .og-x').click()`); await sleep(200); await sendIt(); await sleep(1500);
  m = await lastMsg('.dm'); check(m && m.og === 0, "DM ✕: no preview: " + JSON.stringify(m));
  // a sealed conversation (the lock): no preview under its messages, none above the field (the lead: the
  // belfry can't read it, so asking for a preview would tell it which links were sent)
  const sc = await ev(`window.demoSealed().then((r) => r && r.conv.lina)`);
  await ev(`location.hash = '#/dm/' + encodeURIComponent(${JSON.stringify(sc)})`); await sleep(1500);
  check(!!sc && await ev(`!!document.querySelector('.seal-lock, .topbar .lock, [aria-label*="ncrypted"]')`), "sealed: the conversation shows its lock (" + sc + ")");
  await compose("Sealed link " + L1); await sleep(900);
  check(!(await ev(`!!document.querySelector('.og-pre')`)), "sealed: no composer preview");
  await sendIt(); await sleep(1500);
  m = await lastMsg('.dm');
  check(m && /Sealed link/.test(m.text) && m.og === 0 && m.load === 0, "sealed: no preview under the message: " + JSON.stringify(m));
  // og-dark
  await send("Emulation.setEmulatedMedia", { features: [{ name: "prefers-color-scheme", value: "dark" }] });
  await ev(`location.hash = '#/c/general'`); await sleep(1500);
  const bg = await ev(`(()=>{const o=[...document.querySelectorAll('.og')].at(-1);return o?getComputedStyle(o).backgroundColor:''})()`);
  check(bg && bg !== "rgb(255, 255, 255)", "og-dark: the box follows dark mode: " + bg);
  await shot("og-dark");
  await send("Emulation.setEmulatedMedia", { features: [{ name: "prefers-color-scheme", value: "light" }] });
  // og-laptop
  await send("Emulation.setDeviceMetricsOverride", { width: 1280, height: 800, deviceScaleFactor: 1, mobile: false }); await sleep(800);
  const w = await ev(`(()=>{const o=[...document.querySelectorAll('.og:not(.noimg)')].at(-1);return o?Math.round(o.getBoundingClientRect().width):0})()`);
  check(w > 300 && w <= 440, "og-laptop: a readable width (≤ 440): " + w);
  await shot("og-laptop");

  console.log(`== ogpreview: ${ok} ok, ${fail} fail`);
  ws.close(); ch.kill(); srv.kill(); await sleep(300); fs.rmSync(prof, { recursive: true, force: true });
  process.exit(fail ? 1 : 0);
})();
