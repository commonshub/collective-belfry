// Encrypted conversations, format 3 (docs/encrypted.md §16–§20), over a local relay with the §18.1
// rule on: Sam and Lina, each one device; the device lists (8570) signed here by a test belfry key.
// Test build: VITE_TEST_RELAYS=1 VITE_MAIN_RELAY=VITE_GROUPS_RELAY=ws://127.0.0.1:4481
// VITE_DISCOVERY=/belfry.json, with dist-enctest/belfry.json = {nostr:{pubkey: <test belfry>}, encrypted:{format:3}}.
//   node enc.cjs [dist folder]     (starts its own relay on 4481 and http on 4482)
// (a copy of ~/agents/belfry/site/.shots/chat/enc.cjs; run it from that folder: profiles live there)
const { spawn } = require("child_process"); const fs = require("fs"); const crypto = require("crypto");
const chat = process.env.HOME + "/agents/belfry-webapp/work/chat";
const { finalizeEvent, getPublicKey, nip59 } = require(chat + "/node_modules/nostr-tools");
const dir = __dirname, dist = process.argv[2] || chat + "/dist-enctest", base = "http://127.0.0.1:4482/", relayHttp = "http://127.0.0.1:4481";
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
let bad = 0; const check = (n, ok, got) => { if (!ok) bad++; console.log((ok ? "ok   " : "FAIL ") + n + (ok ? "" : " " + String(JSON.stringify(got)).slice(0, 500))); };
const sk = (name) => new Uint8Array(crypto.createHash("sha256").update("belfry-test:" + name).digest());
const belfrySk = sk("belfry"), belfry = getPublicKey(belfrySk);
const procs = [];
const relay = spawn("node", [dir + "/sealedrelay.cjs", "4481"], { stdio: "ignore" }); procs.push(relay);
const web = spawn("python3", ["-m", "http.server", "4482", "-d", dist], { stdio: "ignore" }); procs.push(web);
const removedFor = {}; // browser name → the /sessions/removed answer (faked here: nothing reaches a real belfry)
const counters = {}, lastIds = {};
async function publishList(account, devices, prevId = lastIds[account]) {
  counters[account] = (counters[account] || 0) + 1
  const n = counters[account]
  const e = finalizeEvent({ kind: 8570, created_at: Math.floor(Date.now() / 1000), tags: [["p", account], ["n", String(n)], ...(prevId ? [["prev", prevId]] : []), ...[...devices].sort().map((d) => ["device", d])], content: "" }, belfrySk)
  await fetch(relayHttp + "/publish", { method: "POST", body: JSON.stringify([e]) })
  lastIds[account] = e.id
  return e.id
}
const dump = async () => (await (await fetch(relayHttp + "/dump")).json());

async function browser(name, port, email, noJson = false) {
  const prof = `${dir}/prof-enc-${name}`; fs.rmSync(prof, { recursive: true, force: true });
  const ch = spawn("chromium-browser", ["--headless=new", "--disable-gpu", `--remote-debugging-port=${port}`, "--user-data-dir=" + prof, "about:blank"]); procs.push(ch);
  let t; for (let i = 0; i < 40; i++) { try { t = await (await fetch(`http://127.0.0.1:${port}/json`)).json(); break; } catch { await sleep(300); } }
  const ws = new WebSocket(t.find((x) => x.type === "page").webSocketDebuggerUrl); let id = 0; const wait = {};
  ws.onmessage = (m) => { const d = JSON.parse(m.data); if (wait[d.id]) { wait[d.id](d.result || d); delete wait[d.id]; } };
  await new Promise((r) => (ws.onopen = r));
  const send = (method, params = {}) => new Promise((r) => { const i = ++id; wait[i] = r; ws.send(JSON.stringify({ id: i, method, params })); });
  const ev = async (e) => (await send("Runtime.evaluate", { expression: e, returnByValue: true, awaitPromise: true })).result?.value;
  await send("Network.enable"); await send("Network.setBypassServiceWorker", { bypass: true });
  ws.addEventListener("message", (m) => { const d = JSON.parse(m.data); if (d.method !== "Fetch.requestPaused") return
    const u = d.params.request.url, cors = [{ name: "Content-Type", value: "application/json" }, { name: "Access-Control-Allow-Origin", value: "*" }, { name: "Access-Control-Allow-Headers", value: "*" }]
    if (/belfry\.json/.test(u)) send("Fetch.fulfillRequest", { requestId: d.params.requestId, responseCode: 404, responseHeaders: cors, body: Buffer.from("{}").toString("base64") }) // noJson: the format stays unknown
    else send("Fetch.fulfillRequest", { requestId: d.params.requestId, responseCode: 200, responseHeaders: cors, body: Buffer.from(JSON.stringify(removedFor[name] || { removed: false })).toString("base64") }) });
  await send("Fetch.enable", { patterns: [{ urlPattern: "*sessions/removed*" }, ...(noJson ? [{ urlPattern: "*belfry.json*" }] : [])] });
  await send("Emulation.setDeviceMetricsOverride", { width: 390, height: 844, deviceScaleFactor: 1, mobile: true });
  await send("Page.navigate", { url: base + "?backend=demo" }); await sleep(2500);
  await ev(`(() => { const i = document.querySelector('input[type=email]'); Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value').set.call(i, ${JSON.stringify(email)}); i.dispatchEvent(new Event('input', { bubbles: true })); document.querySelector('form').requestSubmit() })()`)
  await sleep(3000);
  const shot = async (n) => { const s = await send("Page.captureScreenshot", { format: "png" }); if (s?.data) fs.writeFileSync(`${dir}/enc-${n}.png`, Buffer.from(s.data, "base64")); };
  return { name, ev, send, shot };
}
const until = async (f, ms = 20000) => { const t0 = Date.now(); for (;;) { const v = await f(); if (v || Date.now() - t0 > ms) return v; await sleep(500); } };
const typeSend = async (b, text) => b.ev(`(async () => { const i = document.querySelector('.composer textarea, textarea'); Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype, 'value').set.call(i, ${JSON.stringify(text)}); i.dispatchEvent(new Event('input', { bubbles: true })); await new Promise((r) => setTimeout(r, 200)); const btn = [...document.querySelectorAll('button')].find((x) => /^Send/i.test(x.getAttribute('aria-label') || '') || x.textContent.trim() === '↑'); btn && btn.click() })()`);
const has = (b, text) => b.ev(`document.body.innerText.includes(${JSON.stringify(text)})`);

(async () => {
  await sleep(800);
  await fetch(relayHttp + "/prule/on");
  const L = await browser("lina", 9491, "lina@example.org");
  const S = await browser("sam", 9492, "sam@example.org");
  const st = (b) => b.ev("window.__enc && window.__enc.state()");
  const pkL = await L.ev("window.__demoMe()"), pkS = await S.ev("window.__demoMe()");
  const sL = await st(L), sS = await st(S);
  if (!sL) console.log("debug", await L.ev("JSON.stringify([Object.keys(window).filter((k) => k.startsWith('__')), location.href, document.body.innerText.slice(0, 200)])"));
  if (!sL || !sS) { console.log("no __enc"); for (const p of procs) try { p.kill() } catch {}; process.exit(1) }
  check("both run format 3 with their own device key (not the account key)", sL?.on && sS?.on && sL.device && sS.device && sL.device !== pkL && sS.device !== pkS, { sL, sS });
  // the belfry lists each device (§3)
  await publishList(pkL, [sL.device]); await publishList(pkS, [sS.device]);
  check("Sam's device finds itself on its list", await until(async () => (await st(S)).listed === "yes", 15000), await st(S));

  // Sam writes to Lina
  await S.ev(`location.hash = '#/dm/' + encodeURIComponent(${JSON.stringify("n:" + pkL)})`); await sleep(1500);
  await typeSend(S, "Bread on Saturday?");
  const sent = await until(async () => (await dump()).filter((e) => e.kind === 8572).length >= 2, 15000);
  let d = await dump();
  const env = d.filter((e) => e.kind === 8571), msg = d.filter((e) => e.kind === 8572);
  check("a new key: one envelope per person (p sam, p lina), by one-time keys", env.length === 2 && new Set(env.map((e) => e.tags.find((t) => t[0] === "p")[1])).size === 2 && env.every((e) => e.pubkey !== pkS && e.pubkey !== sS.device), env.map((e) => e.tags));
  check("one 8572 per person, Sam's own copy silent, dated now (no backdating)", sent && msg.length === 2 && msg.some((e) => e.tags.some((t) => t[0] === "silent") && e.tags.some((t) => t[0] === "p" && t[1] === pkS)) && msg.every((e) => Math.abs(e.created_at - Date.now() / 1000) < 120), msg.map((e) => [e.tags, e.created_at]));
  check("nothing the old ways to Lina (no 1059)", !d.some((e) => e.kind === 1059), d.filter((e) => e.kind === 1059).length);
  check("the text is nowhere in clear on the relay", !JSON.stringify(d).includes("Bread on Saturday"));
  check("part 9: each device said once that it reads format 3 (30078 d=belfry:device:reads, v 3, by the device)", [sS.device, sL.device].every((dev) => d.filter((e) => e.kind === 30078 && e.pubkey === dev && e.tags.some((t) => t[0] === "d" && t[1] === "belfry:device:reads") && e.tags.some((t) => t[0] === "v" && t[1] === "3")).length === 1), d.filter((e) => e.kind === 30078).map((e) => [e.pubkey.slice(0, 6), e.tags]));
  // Lina reads it
  await L.ev(`location.hash = '#/dm/' + encodeURIComponent(${JSON.stringify("n:" + pkS)})`);
  check("Lina reads it", await until(() => has(L, "Bread on Saturday?"), 20000), await L.ev("document.body.innerText.slice(0, 300)"));
  await L.shot("lina-read");
  // Lina answers: with a key of her own (§17.8)
  await typeSend(L, "Yes, 9 at the hall");
  check("Sam reads Lina's answer", await until(() => has(S, "Yes, 9 at the hall"), 20000));
  d = await dump();
  const Ks = new Set(d.filter((e) => e.kind === 8571).map((e) => e.tags.find((t) => t[0] === "d")[1]))
  check("Lina wrote with a key she made (4 envelopes: two keys, never adopting Sam's)", d.filter((e) => e.kind === 8571).length === 4 && Ks.size === 4, d.filter((e) => e.kind === 8571).length);
  await S.shot("sam-thread");
  // part 5: the lock next to the name, and Who can read this (encrypted/conversation, who-can-read)
  check("the lock next to the name", await until(() => S.ev("!!document.querySelector('.topbar .seal-lock')"), 8000));
  await S.ev("document.querySelector('.topbar .seal-lock').click()"); await sleep(1500);
  let w = await S.ev("document.body.innerText");
  check("Who can read this: the line, Others with a count, my devices by name, this device marked", /Who can read this/.test(w) && /Only .+’s devices and yours can read this conversation\./.test(w) && /Others/.test(w) && /1 device/.test(w) && /This device/.test(w) && /You see how many devices others have, never which\./.test(w) && /How it works/.test(w), w.slice(0, 600));
  check("…no trash on this device (sign-out stays in Settings)", !(await S.ev("!!document.querySelector('.who-trash')")));
  // Xavier's v19: a second device of mine gets a trash icon that asks first
  await publishList(pkS, [sS.device, getPublicKey(sk("sam-laptop"))]); await sleep(2500);
  check("a second device of mine: one trash icon (never on this device)", await until(async () => (await S.ev("document.querySelectorAll('.who-trash').length")) === 1, 8000), await S.ev("document.querySelectorAll('.who-trash').length"));
  await S.ev("document.querySelector('.who-trash').click()"); await sleep(500);
  check("…it asks first (Remove · Cancel)", /Remove .+\?/.test(await S.ev("document.body.innerText")) && /Cancel/.test(await S.ev("document.body.innerText")));
  await S.ev("[...document.querySelectorAll('button')].find((b) => b.textContent.trim() === 'Cancel').click()"); await sleep(300);
  await S.shot("who-can-read");
  await S.ev("history.back()"); await sleep(800);

  // §17.1: someone with no list: held with its text, never sent the old way
  const nobody = getPublicKey(sk("nolist"));
  await S.ev(`location.hash = '#/dm/' + encodeURIComponent(${JSON.stringify("n:" + nobody)})`); await sleep(1200);
  await typeSend(S, "Are you there?");
  await sleep(4000);
  d = await dump();
  check("to someone with no list: nothing published for it", !d.some((e) => (e.kind === 8572 || e.kind === 1059) && e.tags.some((t) => t[0] === "p" && t[1] === nobody)));
  check("…and it stays in the conversation with its text", await has(S, "Are you there?"));
  check("…saying why, with Try again (encrypted/state-held)", await has(S, "couldn’t check who can read this conversation") && await has(S, "Try again"), await S.ev("document.body.innerText.slice(-300)"));
  // the list comes: it goes out by itself (held messages are tried again every 15 s)
  await publishList(nobody, [getPublicKey(sk("nolist-device"))]);
  check("once their list exists, it goes out", await until(async () => (await dump()).some((e) => e.kind === 8572 && e.tags.some((t) => t[0] === "p" && t[1] === nobody)), 40000));

  // the review's P1.5: on a format-3 belfry version 1 writes nothing (no rosters, hand-overs, requests, anchors)
  d = await dump();
  check("no version-1 writes on a format-3 belfry", !d.some((e) => [8560, 8561, 8562, 8563].includes(e.kind) || (e.kind === 30078 && e.tags.some((t) => t[0] === "d" && t[1] === "belfry:sealed"))), d.filter((e) => [8560, 8561, 8562, 8563, 30078].includes(e.kind)).map((e) => e.kind));
  // the review's P1.4: a clear NIP-17 message from Lina's account key, after the conversation's first key: shown marked "Not encrypted"; a clear reaction doesn't act
  const linaSk = new Uint8Array(crypto.createHash("sha256").update("osv-demo:lina@example.org").digest());
  const clearMsg = nip59.wrapEvent({ kind: 14, content: "Sent the old way", tags: [["p", pkS]], created_at: Math.floor(Date.now() / 1000) }, linaSk, pkS);
  await fetch(relayHttp + "/publish", { method: "POST", body: JSON.stringify([clearMsg]) });
  await S.ev(`location.hash = '#/dm/' + encodeURIComponent(${JSON.stringify("n:" + pkL)})`);
  check("a clear message after the first key shows, marked Not encrypted", await until(async () => (await has(S, "Sent the old way")) && (await has(S, "Not encrypted")), 15000), await S.ev("document.body.innerText.slice(-300)"));
  await S.shot("sam-clear");
  // the third read's N1: an envelope refused for good is rebuilt for the same key at once (and the message with it)
  const pkO = getPublicKey(sk("other2")); await publishList(pkO, [getPublicKey(sk("other2-device"))]);
  await fetch(relayHttp + "/refuse/8571/1");
  await S.ev(`location.hash = '#/dm/' + encodeURIComponent(${JSON.stringify("n:" + pkO)})`); await sleep(1200);
  await typeSend(S, "Repaired?");
  const repaired = await until(async () => { const x = await dump(); return x.some((e) => e.kind === 8571 && e.tags.some((t) => t[0] === "p" && t[1] === pkO)) && x.some((e) => e.kind === 8572 && e.tags.some((t) => t[0] === "p" && t[1] === pkO)) }, 40000);
  check("a refused envelope: rebuilt, so the recipient has an envelope and the message", !!repaired);
  // a clear reaction from Lina's account key on Sam's encrypted message: never acts (§17.2, the startup race)
  await S.ev(`location.hash = '#/dm/' + encodeURIComponent(${JSON.stringify("n:" + pkL)})`); await sleep(1000);
  const target = (await dump()).find((e) => e.kind === 8572 && e.tags.some((t) => t[0] === "p" && t[1] === pkS)); // (its rumor id isn't known here: a reaction on any id of the conversation)
  const clearReact = nip59.wrapEvent({ kind: 7, content: "👎", tags: [["e", "f".repeat(64)], ["p", pkS], ["k", "14"]], created_at: Math.floor(Date.now() / 1000) }, linaSk, pkS);
  await fetch(relayHttp + "/publish", { method: "POST", body: JSON.stringify([clearReact]) });
  await S.ev("location.reload()"); await sleep(5000);
  await S.ev(`location.hash = '#/dm/' + encodeURIComponent(${JSON.stringify("n:" + pkL)})`); await sleep(2500);
  check("after a reload, a clear reaction in the encrypted conversation shows nowhere", !(await has(S, "👎")) && !!target);
  // the format unknown (no belfry.json answer): a message is held with words, never sent the old way
  const N = await browser("nojson", 9493, "nojson@example.org", true);
  const pkN = await N.ev("window.__demoMe()");
  await N.ev(`location.hash = '#/dm/' + encodeURIComponent(${JSON.stringify("n:" + pkL)})`); await sleep(1500);
  const toLina = async () => (await dump()).filter((e) => (e.kind === 8572 || e.kind === 1059) && e.tags.some((t) => t[0] === "p" && t[1] === pkL)).length;
  const beforeN = await toLina();
  await typeSend(N, "Before the format is known");
  await sleep(8000);
  check("format unknown: nothing published to Lina for it (no 8572, no 1059)", (await toLina()) === beforeN, { beforeN, after: await toLina() });
  check("…held in the conversation, with words", await has(N, "Before the format is known") && await has(N, "Not sent"), await N.ev("document.body.innerText.slice(-200)"));
  // part 6–7: a new device of Sam's (S2, listed after S): "Ask for them"; S's card shares; S2 reads the earlier messages
  const S2 = await browser("sam2", 9494, "sam@example.org");
  const s2 = await st(S2);
  check("S2 is Sam's account with its own device key", (await S2.ev("window.__demoMe()")) === pkS && s2.device && s2.device !== sS.device);
  await publishList(pkS, [sS.device, getPublicKey(sk("sam-laptop")), s2.device]);
  await S2.ev("location.hash = '#/dm'"); await sleep(3000);
  check("S2: 'Earlier messages are on your other devices' · Ask for them", await until(() => has(S2, "Earlier messages are on your other devices"), 20000) && await has(S2, "Ask for them"), await S2.ev("document.body.innerText.slice(-300)"));
  await S2.shot("new-list");
  await S2.ev("[...document.querySelectorAll('button')].find((b) => b.textContent.trim() === 'Ask for them').click()"); await sleep(1500);
  check("S2: an 8574 signed by its device", (await dump()).some((e) => e.kind === 8574 && e.pubkey === s2.device), null);
  check("S2: 'Asked your other devices'", await until(() => has(S2, "Asked your other devices"), 8000));
  await S.ev("location.hash = '#/dm'"); await sleep(3000);
  check("S: a card for the new device, with Share earlier messages", await until(() => has(S, "Share earlier messages"), 20000), await S.ev("document.body.innerText.slice(0, 400)"));
  await S.shot("card");
  await S.ev("[...document.querySelectorAll('button')].filter((b) => b.textContent.trim() === 'Share earlier messages').forEach((b) => b.click())"); 
  check("S: key packs to S2 and the answer", await until(async () => { const x = await dump(); return x.some((e) => e.kind === 8573 && e.tags.some((t) => t[0] === "d" && t[1] === s2.device)) && x.some((e) => e.kind === 8575 && e.pubkey === sS.device) }, 30000));
  check("S: 'Earlier messages shared with …'", await until(() => has(S, "Earlier messages shared with"), 10000));
  await S2.ev(`location.hash = '#/dm/' + encodeURIComponent(${JSON.stringify("n:" + pkL)})`);
  check("S2 reads the earlier messages (Bread on Saturday?)", await until(() => has(S2, "Bread on Saturday?"), 30000), await S2.ev("document.body.innerText.slice(0, 300)"));
  check("…and one quiet line says where they came from", await has(S2, "Earlier messages shared by"));
  await S2.shot("arrived");
  // encrypted/removed-device: another device removed S2: its data goes, one screen
  removedFor.sam2 = { removed: true, at: Math.floor(Date.now() / 1000) - 60, by_name: "Safari on Mac" };
  await S2.ev("location.reload()"); await sleep(5000);
  check("S2 removed: 'This device was removed · Safari on Mac removed it …' · Sign in again", await until(() => has(S2, "This device was removed"), 15000) && await has(S2, "Safari on Mac removed it") && await has(S2, "Sign in again"), await S2.ev("document.body.innerText.slice(0, 300)"));
  check("…and this device's data is gone (no belfry keys in its localStorage but the device-wide ones)", await S2.ev("Object.keys(localStorage).filter((k) => /^belfry[-:]/.test(k) && !/^belfry-(accounts?|accounts-ls:|other-belfries|community-name|enc-format|enc-seen)/.test(k)).length === 0"), await S2.ev("JSON.stringify(Object.keys(localStorage))"));
  await S2.shot("removed");
  // part 8: Me › Devices on Sam's first device (encrypted/devices, device)
  await S.ev("location.hash = '#/me/devices'"); await sleep(2500);
  w = await S.ev("document.body.innerText");
  check("Me › Devices: the line, This device, and a trash icon on each other device", /These devices can read your encrypted messages/.test(w) && /This device/.test(w) && (await S.ev("document.querySelectorAll('.who-trash').length")) === 2, [w.slice(0, 300), await S.ev("document.querySelectorAll('.who-trash').length")]);
  await S.shot("devices");
  await S.ev("[...document.querySelectorAll('.enc-devices .row-btn')].at(-1).click()"); await sleep(800);
  w = await S.ev("document.body.innerText");
  check("one device: Added, Last active, Earlier messages Shared, Remove this device", /Added/.test(w) && /Last active/.test(w) && /Earlier messages/.test(w) && /Shared/.test(w) && /Remove this device/.test(w), w.slice(0, 300));
  // (a) Android: a connection authenticated with a DEVICE key, not the account, gets no 8572 (§18.1)
  const got = await new Promise((res) => {
    const w = new WebSocket("ws://127.0.0.1:4481"); const out = [];
    w.onmessage = (m) => { const x = JSON.parse(m.data);
      if (x[0] === "AUTH") { const a = finalizeEvent({ kind: 22242, created_at: Math.floor(Date.now() / 1000), tags: [["relay", "ws://127.0.0.1:4481"], ["challenge", x[1]]], content: "" }, sk("a-device")); w.send(JSON.stringify(["AUTH", a])); setTimeout(() => w.send(JSON.stringify(["REQ", "x", { kinds: [8572, 8571], "#p": [pkL] }])), 100) }
      if (x[0] === "EVENT") out.push(x[2]); if (x[0] === "EOSE") { w.close(); res(out) } };
    setTimeout(() => res(out), 5000);
  });
  check("(a) a connection authenticated as a device key gets none of Lina's 8571/8572", got.length === 0, got.length);
  // §18.1: the relay refuses one dated more than 5 minutes from now
  const old = finalizeEvent({ kind: 8572, created_at: Math.floor(Date.now() / 1000) - 400, tags: [["p", pkL], ["d", "0".repeat(64)]], content: "x" }, sk("once"));
  const ok = await new Promise((res) => { const w = new WebSocket("ws://127.0.0.1:4481"); w.onopen = () => w.send(JSON.stringify(["EVENT", old])); w.onmessage = (m) => { const x = JSON.parse(m.data); if (x[0] === "OK") { w.close(); res(x[2]) } }; setTimeout(() => res(null), 4000) });
  check("an 8572 dated 400 s ago is refused by the relay (§18.1)", ok === false, ok);

  console.log(bad ? `== enc: ${bad} failed` : "== enc: all ok");
  for (const p of procs) try { p.kill() } catch {}
  await sleep(500); for (const n of ["lina", "sam", "nojson", "sam2"]) try { fs.rmSync(`${dir}/prof-enc-${n}`, { recursive: true, force: true }) } catch {}
  process.exit(bad ? 1 : 0);
})();
