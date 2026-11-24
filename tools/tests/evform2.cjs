// Event form canvas v16 (Xavier approved, 24 Nov), demo: the Event website fills in the form ("Fetching event
// details…", "✓ Filled in from …"), Where decides Place / Join online, the page address only for Public with
// its taken line, and the event screen's rows: Join online (not going: how to get it), Event website, Our page.
const { spawn } = require("child_process"); const fs = require("fs");
const dir = __dirname, port = 9395, prof = dir + "/prof-evform2", base = (process.argv[2] || "http://127.0.0.1:4392") + "/";
fs.rmSync(prof, { recursive: true, force: true });
const ch = spawn("chromium-browser", ["--headless=new", "--disable-gpu", `--remote-debugging-port=${port}`, "--user-data-dir=" + prof, "about:blank"]);
const sleep = ms => new Promise(r => setTimeout(r, ms));
let bad = 0; const check = (name, ok, got) => { if (!ok) bad++; console.log(ok ? "ok  " : "FAIL", name, got === undefined ? "" : JSON.stringify(got)); };
(async () => {
  let t; for (let i = 0; i < 40; i++) { try { t = await (await fetch(`http://127.0.0.1:${port}/json`)).json(); break; } catch { await sleep(300); } }
  const ws = new WebSocket(t.find(x => x.type === "page").webSocketDebuggerUrl); let id = 0; const wait = {};
  ws.onmessage = m => { const d = JSON.parse(m.data); if (wait[d.id]) { wait[d.id](d.result || d); delete wait[d.id]; } };
  await new Promise(r => ws.onopen = r);
  const send = (method, params = {}) => new Promise(r => { const i = ++id; wait[i] = r; ws.send(JSON.stringify({ id: i, method, params })); });
  const ev = async e => { const r = await send("Runtime.evaluate", { expression: e, returnByValue: true, awaitPromise: true }); if (r.exceptionDetails) console.log("JS:", r.exceptionDetails.exception?.description?.split("\n")[0]); return r.result?.value; };
  const shot = async n => { const s = await send("Page.captureScreenshot", { format: "png" }); fs.writeFileSync(`${dir}/evform2-${n}.png`, Buffer.from(s.data, "base64")); };
  const click = async (sel, scope = "document") => ev(`(() => { const b = [...${scope}.querySelectorAll('button, [role=button]')].find(b => (b.getAttribute('aria-label')||'') === ${JSON.stringify(sel)} || b.textContent.trim() === ${JSON.stringify(sel)}) || [...${scope}.querySelectorAll('button')].find(b => b.textContent.includes(${JSON.stringify(sel)})); if (!b) return 'no ' + ${JSON.stringify(sel)}; b.click(); return 'ok' })()`);
  const setv = async (sel, v) => ev(`(() => { const i = document.querySelector(${JSON.stringify(sel)}); if (!i) return 'none'; const proto = i.tagName === 'TEXTAREA' ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype; Object.getOwnPropertyDescriptor(proto, 'value').set.call(i, ${JSON.stringify(v)}); i.dispatchEvent(new Event('input', { bubbles: true })); return 'ok' })()`);
  const text = sel => ev(`document.querySelector(${JSON.stringify(sel)})?.innerText.replace(/\\s+/g, ' ').trim()`);
  const noOverflow = () => ev("document.documentElement.scrollWidth <= innerWidth && ![...document.querySelectorAll('.screen *')].some(e => { const r = e.getBoundingClientRect(); return r.width && r.right > innerWidth + 1 && getComputedStyle(e).position !== 'fixed' && !e.closest('.chead-panel[aria-hidden=true]') && !e.closest('[aria-hidden=true]') })");
  await send("Emulation.setDeviceMetricsOverride", { width: 375, height: 812, deviceScaleFactor: 2, mobile: true });
  await send("Page.navigate", { url: base + "?backend=demo" }); await sleep(3000);
  await setv('input[type=email]', 'ana@example.org'); await ev(`document.querySelector('form').requestSubmit()`); await sleep(2500); await ev("if (!location.hash || location.hash === '#/' || location.hash === '#/home') location.hash = '#/c/general'"); await sleep(800); // since the places (2026-11-16) the app starts on Home

  await ev("window.demoRoles(true)"); await sleep(300);
  await ev("location.hash = '#/events'"); await sleep(900);
  await click("New event"); await sleep(600);
  // the website first; pasting it fills in the rest
  await setv('.sheet input[type=url]', 'https://riverside-openday.example'); await sleep(900);
  check("Fetching event details…", /Fetching event details/.test(await text('.sheet .evf')), await text('.sheet .evf'));
  await sleep(1800);
  check("✓ Filled in from riverside-openday.example", /Filled in from riverside-openday\.example\. Check it and change anything\./.test(await text('.sheet .evf')), await text('.sheet .evf'));
  check("…the title, the place and the description filled in", await ev("document.querySelector('.sheet input[aria-label=Title]').value") === 'Riverside Open Day 2027' && /Rue Haute 12/.test(await ev("document.querySelector('.sheet .place-input input').value")) && /workshops/.test(await ev("document.querySelector('.sheet textarea').value")));
  check("…the date and times from the site (Brussels time)", await ev("document.querySelector('.sheet input[aria-label=Date]').value") === '2027-01-28' && await ev("document.querySelector('.sheet input[aria-label=Starts]').value") === '10:00' && /Brussels time/.test(await text('.sheet .evf-tz')), [await ev("document.querySelector('.sheet input[aria-label=Date]').value"), await ev("document.querySelector('.sheet input[aria-label=Starts]').value")]);
  check("Join online is never filled in (In person: not even shown)", !(await ev("!!document.querySelector('.sheet input[placeholder=\"Paste a meeting link\"]')")));
  await shot("1-fetched");
  // Where: Online → Join online only; Both → Place then Join online; "or create a Jitsi link"
  await click("Online"); await sleep(200);
  check("Online: Join online, no Place", await ev("!!document.querySelector('.sheet input[placeholder=\"Paste a meeting link\"]') && !document.querySelector('.sheet .place-input')") && /Only people going see it, from 1 hour before the start\./.test(await text('.sheet .evf')));
  await click("Both"); await sleep(200);
  check("Both: Place and Join online", await ev("!!document.querySelector('.sheet input[placeholder=\"Paste a meeting link\"]') && !!document.querySelector('.sheet .place-input')"));
  await click("create a Jitsi link"); await sleep(100);
  check("or create a Jitsi link fills it", /^https:\/\/meet\.jit\.si\//.test(await ev("document.querySelector('.sheet input[placeholder=\"Paste a meeting link\"]').value")));
  // the page address: none for Members; Public shows it
  check("Members: no page address", !(await ev("!!document.querySelector('.sheet .url-field')")));
  await click("Public"); await sleep(1200);
  check("Public: Page on …'s website, ✓ Available", /Page on .*website/.test(await text('.sheet .evf-page')) && /✓ Available/.test(await text('.sheet .evf-slug')), await text('.sheet .evf-page'));
  await click("Hidden"); await sleep(200);
  check("Hidden: no page address", !(await ev("!!document.querySelector('.sheet .url-field')")));
  await click("Public"); await sleep(400);
  await shot("2-public");
  await click("Create the event"); await sleep(2000);
  check("created: its screen", /^#\/events\//.test(await ev("location.hash")), await ev("location.hash"));
  const half = await text('.evh');
  check("the three rows, in order: Join online · Event website · Our page", /Join online[\s\S]*Event website[\s\S]*riverside-openday\.example[\s\S]*Our page/.test(half), half);
  check("Join online: the host isn't 'going' yet: how to get the link, no link", /For people going: answer Going to get the link/.test(half) && !(await ev("!!document.querySelector('.evr-join')")), half);
  check("Event website: ↗ opens it outside", await ev("document.querySelector('.evr a[aria-label=\"Open the event website\"]')?.getAttribute('target')") === '_blank');
  await click("Copy the link"); await sleep(200);
  check("Our page: Copy says Copied", /Copied/.test(await text('.evr-rows')));
  await click("Show the link as a QR code"); await sleep(500);
  check("Our page: the QR sheet", await ev("!!document.querySelector('.sheet img, dialog[open] img, .sheet canvas, .sheet svg')"));
  await ev("document.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true })); [...document.querySelectorAll('button')].find((x) => x.getAttribute('aria-label') === 'Close')?.click()"); await sleep(400);
  await shot("3-event");
  // a second event wanting the same address: "Already used by … · See that event ›", a free one offered
  await ev("location.hash = '#/events'"); await sleep(900);
  await click("New event"); await sleep(600);
  await setv('.sheet input[aria-label=Title]', 'Riverside Open Day 2027'); await click("Public"); await sleep(1500);
  const taken = await text('.sheet .evf-page');
  check("taken: Already used by Riverside Open Day 2027 · See that event ›", /Already used by Riverside Open Day 2027/.test(taken) && /See that event ›/.test(taken), taken);
  check("…and a free one: Use it", /is free · Use it/.test(taken), taken);
  await click("Use it"); await sleep(1500);
  check("Use it: ✓ Available", /✓ Available/.test(await text('.sheet .evf-slug')), await text('.sheet .evf-slug'));
  await shot("4-taken");
  // the laptop (UI-26, evform-laptop): two columns, Save top right with its line; no bottom button
  await ev("document.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true })); [...document.querySelectorAll('button')].find((x) => x.getAttribute('aria-label') === 'Close')?.click()"); await sleep(400);
  await send("Emulation.setDeviceMetricsOverride", { width: 1280, height: 900, deviceScaleFactor: 1, mobile: false }); await sleep(600);
  await ev("location.hash = '#/events'"); await sleep(900);
  await click("New event"); await sleep(800);
  const lap = await ev(`(() => { const t = document.querySelector('.evf-top .btn'), b = document.querySelector('.evf .btn.evf-bottom'), f = document.querySelector('.evf'); const r = t?.getBoundingClientRect(), fr = f?.getBoundingClientRect(); return { top: !!t && getComputedStyle(t).display !== 'none' && r.top < fr.top + 80 && r.right > fr.right - 60, bottom: !!b && getComputedStyle(b).display === 'none', cols: getComputedStyle(f).gridTemplateColumns.split(' ').length } })()`);
  check("laptop: Save top right, no bottom button, two columns", lap.top && lap.bottom && lap.cols === 2, lap);
  await shot("5-laptop");
  console.log(bad ? `== evform2: ${bad} failed` : "== evform2: all ok");
  ws.close(); ch.kill(); await sleep(300); fs.rmSync(prof, { recursive: true, force: true }); process.exit(bad ? 1 : 0);
})();
