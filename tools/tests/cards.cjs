// Printed cards (canvas "Belfry · Group cards"), demo mode: K7Q2MX a free group card, T9PC4H a
// free topic card, CXAWMS claimed, RETRDX retired, ARCH9V archived; code 123456.
const { spawn } = require("child_process"); const fs = require("fs");
const dir = __dirname, port = 9474, web = 4474, prof = dir + "/prof-cards";
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

  // A. a visitor starts a group (Cards v87: onb-group-form → onb-code → onb-group-done)
  await fresh(); await open('#/g/K7Q2MX');
  let s = await text();
  check(/Start a group/.test(s) && /Nobody has scanned this card yet\. The group starts with you\./.test(s), "A: Start a group, the lead " + s.slice(0, 160));
  check(await ev(`document.querySelector('.card-chip')?.textContent`) === 'Card K7Q2MX', "A: the chip 'Card K7Q2MX'");
  check(await ev(`location.hash===''`), "A: the address is cleaned");
  const orderA = await ev(`[...document.querySelectorAll('.card-form .field > span:first-child')].map((x) => x.textContent.trim()).join('|')`);
  check(orderA === 'Group name|Description|Your name|Your e-mail', "A: Group name, Description, Your name, Your e-mail: " + orderA);
  check(/What you wrote on the card/.test(s) && /You’ll look after this group\. We’ll send a 6-digit code to confirm your e-mail\./.test(s), "A: the help and the foot");
  check(await btn('Create the group') === 'disabled', "A: Create locked until the form is filled");
  await typeIn('Group name', 'Sourdough starters'); await typeIn('Description', 'Swap starters, bake on Saturdays.'); await typeIn('Your name', 'Lina'); await typeIn('Your e-mail', 'lina@example.org'); await sleep(200);
  await shot("cards-start");
  check(await btn('Create the group') === 'on', "A: Create on once filled");
  await click('Create the group'); await sleep(800);
  s = await text();
  check(/Check your e-mail/.test(s) && /To create Sourdough starters, enter the 6-digit code we sent to lina@example\.org\./.test(s), "A: onb-code's words " + s.slice(0, 200));
  check(/or click Confirm in the e-mail\. This page moves on by itself once you have\./.test(s), "A: the Confirm-in-the-e-mail line");
  check(/Send a new code \(in \d+ s\)/.test(s) && /Use another address/.test(s) && /Not there\? Look in your spam folder\./.test(s), "A: resend in N s, another address, the spam hint");
  await type('.code-boxes input', '111111'); await sleep(800);
  check(/2 tries left/.test(await text()), "A: a wrong code says how many tries are left");
  // a reload keeps what was typed
  await open('#/g/K7Q2MX');
  check(await ev(`${field('Group name')}.querySelector('input').value`) === 'Sourdough starters', "A: the form is kept over a reload");
  await click('Create the group'); await sleep(800);
  await type('.code-boxes input', '123456'); await sleep(3500);
  // (the demo signs a visitor into Townsquare, not into a new group: the group's done screen is checked in A2)
  check(/You created it and you look after it/.test(await ev(`document.querySelector('.gmid-welcome')?.textContent||''`)), "A: in, half pulled down, the creator's welcome");
  await shot("cards-started");
  // the same card again (signed in now): it is claimed: its guest link, no form
  await open('#/g/K7Q2MX'); await sleep(800);
  check(!/Start a group/.test(await text()), "A: a claimed card has no form");

  // A2. signed in: a group card (claim-signed-in): no name or e-mail, the account box, Xavier's footnote; straight in (onb-group-done)
  await open('#/g/G2RDNS');
  s = await text();
  check(/Start a group/.test(s) && /You’ll be the group’s first steward\./.test(s) && /without having to share your email address publicly/.test(s), "A2: Start a group, Xavier's footnote " + s.slice(-300));
  check(await ev(`!!document.querySelector('.card-account')`) && !(await ev(`!!${field('Your name')}`)), "A2: the account box, no name or e-mail");
  await typeIn('Group name', 'Repair cafe'); await typeIn('Description', 'Fix things together on Sundays.'); await sleep(200);
  await click('Create the group'); await sleep(2500);
  const wA2 = await ev(`document.querySelector('.gmid-welcome')?.textContent||''`);
  check(/Welcome to Repair cafe!/.test(wA2) && /You created it and you look after it\. Say hi and share something to get it going\./.test(wA2), "A2: onb-group-done's welcome: " + wA2);
  check(/^Steward: you · 1 person$/.test(await ev(`document.querySelector('.gmid-facts')?.textContent||''`)), "A2: Steward: you · 1 person: " + await ev(`document.querySelector('.gmid-facts')?.textContent||''`));
  check(/Card G2RDNS: anyone who scans it joins here/.test(await ev(`document.querySelector('.gmid-card')?.textContent||''`)), "A2: Card G2RDNS: anyone who scans it joins here");
  await shot("cards-group-done");

  // B. signed in: a topic card (onb-topic-signed-in): Hashtag (asked directly), Description, Create, the account box
  await open('#/g/T9PC4H');
  s = await text();
  check(/Start a topic/.test(s) && /The topic starts with you\./.test(s), "B: Start a topic " + s.slice(0, 160));
  check(await ev(`document.querySelector('.card-chip')?.textContent`) === 'Card T9PC4H · topic', "B: the chip 'Card T9PC4H · topic'");
  check(!(await ev(`!!${field('Your e-mail')}`)), "B: no e-mail field when signed in");
  const orderB = await ev(`[...document.querySelectorAll('.card-form .field > span:first-child')].map((x) => x.textContent.trim()).join('|')`);
  check(orderB === 'Hashtag|Description', "B: Hashtag, Description (no topic in words): " + orderB);
  const acct = await ev(`document.querySelector('.card-account')?.innerText||''`);
  check(/Log out/.test(acct) && !/^You\b/.test(acct) && await ev(`!!document.querySelector('.card-form .btn + .card-account')`), "B: the account box below Create, with Log out: " + acct);
  check(/You’ll look after this topic\./.test(s) && !/6-digit code/.test(s), "B: the foot (no code when signed in)");
  // Xavier (24 Nov): the hashtag cleans itself as it's typed or pasted
  const hv = () => ev(`${field('Hashtag')}.querySelector('input').value`);
  await typeIn('Hashtag', 'Café Crème & Co!'); await sleep(100);
  check(await hv() === 'cafe-creme-co', "B: accents folded, spaces → '-', punctuation dropped: " + await hv());
  await typeIn('Hashtag', ' - Straße   der  Tools--x'); await sleep(100);
  check(await hv() === 'strasse-der-tools-x', "B: no '-' first, ß → ss, no '--': " + await hv());
  await typeIn('Hashtag', 'tools-'); await sleep(100);
  check(await hv() === 'tools-', "B: a '-' at the end stays while typing");
  await ev(`${field('Hashtag')}.querySelector('input').dispatchEvent(new FocusEvent('focusout', { bubbles: true }))`); await sleep(200);
  check(await hv() === 'tools', "B: …and goes when leaving the field: " + await hv());
  await typeIn('Hashtag', 'a'.repeat(40)); await sleep(100);
  check((await hv()).length === 30, "B: 30 at most");
  await typeIn('Hashtag', 'general'); await sleep(900);
  check(/Taken/.test(await ev(`document.querySelector('.card-tag').textContent`)), "B: #general is taken");
  await typeIn('Hashtag', 'Sourdough'); await sleep(900);
  check(await ev(`document.querySelector('.card-tag').textContent`) === '✓ Available · this is the channel’s name', "B: ✓ Available · this is the channel's name");
  await typeIn('Description', 'Bring your starter');
  check(!(await ev(`!!document.querySelector('.card-form .seg2')`)), "B: no Summary by e-mail on the start form");
  await shot("cards-topic");
  await click('Create the topic'); await sleep(2500);
  check(await ev(`location.hash`) === '#/c/sourdough', "B: into #sourdough: " + await ev(`location.hash`));
  const wB = await ev(`document.querySelector('.gmid-welcome')?.textContent||''`);
  check(/#sourdough is open!/.test(wB) && /You created it and you look after it\. Share the first tip, link or question\./.test(wB), "B: onb-topic-done's welcome: " + wB);
  check(/^Steward: you · 1 following$/.test(await ev(`document.querySelector('.gmid-facts')?.textContent||''`)), "B: Steward: you · 1 following: " + await ev(`document.querySelector('.gmid-facts')?.textContent||''`));
  s = await text();
  check(/#\s*sourdough/.test(s) && /Bring your starter/.test(s), "B: the channel shows as #sourdough, its description what was written: " + s.slice(0, 300));
  await ev(`location.hash='#/c/general'`); await sleep(1500);
  s = await ev(`[...document.querySelectorAll('.msg')].map((m) => m.innerText).join(' || ').slice(-600)`);
  check(/created the topic #sourdough/.test(s) && /New topic/i.test(s) && /Bring your starter/.test(s) && /Steward: /.test(s) && /1 following/.test(s) && !/# New topic/.test(s), "B: in Town square (onb-town-topic): 'created the topic #sourdough', the card with Steward: … · 1 following: " + s);
  await shot("cards-townsquare-topic");
  await ev(`[...document.querySelectorAll('.ntc button')].find((b) => b.textContent === 'Open').click()`); await sleep(800);
  check(await ev(`location.hash`) === '#/c/sourdough', "B: its Open goes to #sourdough: " + await ev(`location.hash`));

  // B2. the steward's settings: the card, its QR, Retire (then the card says closed)
  await ev(`location.hash='#/c/sourdough/settings'`); await sleep(1500);
  s = await text();
  check(/The card/.test(s) && /Card T9PC4H/.test(s), "B2: settings show the card " + s.slice(0, 300));
  await click('Show the card’s QR'); await sleep(300);
  check(await ev(`!!document.querySelector('.card-qr img')`), "B2: the QR shows");
  await shot("cards-steward");
  await click('Retire this card'); await sleep(300);
  await click('Retire'); await sleep(600);
  check(/is retired/.test(await text()), "B2: retired");
  await open('#/g/T9PC4H');
  check(/closed to new people/.test(await text()), "B2: the retired card says closed");

  // C0. the e-mail's Confirm button: the token leaves the address at once (never in history); a link that
  // can't be confirmed (the demo has no server) says so
  await fresh(); await send("Page.navigate", { url: "about:blank" }); await sleep(200);
  await send("Page.navigate", { url: `http://127.0.0.1:${web}/?backend=demo&confirm=tok_ABCdef123456#/g/K7Q2MX` }); await sleep(2500);
  check(!(await ev("location.href")).includes("confirm=") && !(await ev("location.href")).includes("tok_ABC"), "C0: the token is gone from the address: " + await ev("location.href"));
  check(/This link didn’t work/.test(await text()), "C0: a link that can't be confirmed says so: " + (await text()).slice(0, 160));

  // C. dead ends
  await open('#/g/RETRDX'); s = await text();
  check(/Sourdough starters is closed to new people/.test(s) && /Visit /.test(s), "C: retired " + s.slice(0, 100));
  check(await ev(`localStorage.getItem('belfry-card-form')`) === null, "C: a retired card drops what was typed");
  await open('#/g/ARCH9V'); s = await text();
  check(/Book club has ended/.test(s), "C: archived");
  await open('#/g/<b>x</b>'); s = await text();
  check(/isn’t known here/.test(s) && !/<b>/.test(s), "C: a malformed id: not known, nothing of it shown: " + s.slice(0, 200) + await ev('location.href'));
  await click('Close'); await sleep(600);
  check(!/isn’t known here/.test(await text()), "C: Close leaves the card page");

  // D. a visitor scans a claimed card: the guest link, the group with "Say hi"
  await fresh(); await open('#/g/CXAWMS'); await sleep(2500);
  check(/Welcome to/.test(await ev(`document.querySelector('.gmid-welcome')?.textContent||''`)), "D: a claimed card opens its group with the welcome: " + (await text()).slice(0, 200) + " " + await ev('location.hash'));
  await shot("cards-joined");

  // E. a visitor scans a claimed TOPIC card: may post at once, without an e-mail (the lead's approved list)
  await fresh(); await open('#/g/TPCHAT'); await sleep(2500);
  s = await text();
  check(/Sending as a guest/.test(s) && /Log in/.test(s), "E: the composer with 'Sending as a guest · Log in' " + s.slice(-200));
  check(await ev(`document.querySelector('textarea')?.placeholder`) === 'Share a link, a message, a photo', "E: the placeholder");
  check(await ev(`document.querySelector('.composer input[type=file]')?.accept`) === 'image/*', "E: photos only from the +");
  await type('textarea', 'Spare cordless drill, ask me at no. 7'); await sleep(200);
  await ev(`[...document.querySelectorAll('button')].find((b) => /^Send/i.test(b.getAttribute('aria-label') || ''))?.click()`); await sleep(1200);
  s = await text();
  check(/Spare cordless drill/.test(s), "E: the post shows");
  check(/Shared\. Don’t lose your account/.test(s) && /Not now/.test(s), "E: then the save card");
  // the gate: anything but a plain message in that topic is refused (it opens the Join sheet)
  const refused = async (t) => ev(`(async () => { try { await window.__demoEnqueue('groups', ${JSON.stringify(t)}); return 'sent' } catch (e) { return 'refused' } })()`);
  check(await refused({ kind: 7, content: '+', tags: [['h', 'general'], ['e', 'a'.repeat(64)]] }) === 'refused', "E: a reaction is refused");
  check(await refused({ kind: 9, content: 'hi', tags: [['h', 'general'], ['p', 'b'.repeat(64)]] }) === 'refused', "E: a message with a mention tag is refused");
  check(await refused({ kind: 1111, content: 'hi', tags: [['h', 'general'], ['e', 'a'.repeat(64)]] }) === 'refused', "E: a thread reply is refused");
  check(await refused({ kind: 9, content: 'hi', tags: [['h', 'other-channel']] }) === 'refused', "E: another channel is refused");
  // cards/follow-channel-form: Save → name, e-mail and Summary by e-mail (Weekly by default)
  await click('Save'); await sleep(600);
  check(await ev(`document.querySelector('.join-sheet .seg2 [aria-checked="true"]')?.textContent`) === 'Weekly', "E: the follow form has Summary by e-mail, Weekly by default");
  await ev(`document.querySelector('dialog [aria-label="Close"], .sheet [aria-label="Close"]')?.click()`); await sleep(400);
  // no preview of outside links in the guest composer (its message can't carry the ✕ tag)
  await type('textarea', 'See https://toolshare.example/stories/street-library'); await sleep(900);
  check(!(await ev(`!!document.querySelector('.og-pre')`)), "E: no composer preview for a guest in a topic");
  await type('textarea', '');
  await click('Not now'); await sleep(300);
  check(!/Don’t lose your account/.test(await text()), "E: Not now hides the card");
  await shot("cards-topic-guest");
  // F. the same visitor through a GROUP card (CXAWMS): reading only, Join, as before
  await fresh(); await open('#/g/CXAWMS'); await sleep(2500);
  check(!/Sending as a guest/.test(await text()) && await ev(`!!document.querySelector('.guest-bar')`), "F: a group card: the guest bar, no composer");

  console.log(`== cards: ${ok} ok, ${fail} fail`);
  ch.kill(); srv.kill(); await sleep(500); try { fs.rmSync(prof, { recursive: true, force: true }) } catch {} process.exit(fail ? 1 : 0);
})();
