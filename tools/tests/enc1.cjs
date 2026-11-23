// FORMAT 1 (the lead's ask after the full review): enc-v3 built for a belfry whose belfry.json says
// encrypted.format 1, against a relay that HAS 8570 lists: everything goes out exactly as chat-source does.
// Encrypted conversations, format 3 (docs/encrypted.md §16–§20), over a local relay with the §18.1
// rule on: Sam and Lina, each one device; the device lists (8570) signed here by a test belfry key.
// Test build: VITE_TEST_RELAYS=1 VITE_MAIN_RELAY=VITE_GROUPS_RELAY=ws://127.0.0.1:4481
// VITE_GROUPS_RELAY=ws://localhost:4481 (the same relay, its own socket: the app keeps one per URL) VITE_DISCOVERY=/belfry.json, with dist-enctest/belfry.json = {nostr:{pubkey: <test belfry>}, encrypted:{format:3}}.
//   node enc.cjs [dist folder]     (starts its own relay on 4481 and http on 4482)
const { spawn } = require("child_process"); const fs = require("fs"); const crypto = require("crypto");
const chat = process.env.HOME + "/agents/belfry-webapp/work/chat";
const { finalizeEvent, getPublicKey, nip59 } = require(chat + "/node_modules/nostr-tools");
// the web's own rules, for the parts the test plays itself (the groups key's posts and answers)
require(chat + "/node_modules/esbuild").buildSync({ entryPoints: [chat + "/src/core/enc/core.ts"], bundle: true, platform: "node", format: "cjs", outfile: __dirname + "/.enc-core.cjs", logLevel: "error" });
const EC = require(__dirname + "/.enc-core.cjs");
const dir = __dirname, dist = process.argv[2] || chat + "/dist-enc1", base = "http://127.0.0.1:4482/", relayHttp = "http://127.0.0.1:4481";
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
  const prof = `${dir}/prof-enc1-${name}`; fs.rmSync(prof, { recursive: true, force: true });
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
  const shot = async (n) => { const s = await send("Page.captureScreenshot", { format: "png" }); if (s?.data) fs.writeFileSync(`${dir}/enc1-${n}.png`, Buffer.from(s.data, "base64")); };
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
  check("format 1 here: the runtime reads, doesn't write format 3", sL && sS && sL.on === false && sS.on === false, { sL, sS });
  await publishList(pkL, [sL.device]); await publishList(pkS, [sS.device]);
  // a private group, as on belfry.sh
  const gid = "pod-format1-private-1a2b", relaySk = sk("groups-relay");
  const meta = finalizeEvent({ kind: 39000, created_at: Math.floor(Date.now() / 1000), tags: [["d", gid], ["name", "Plot committee"], ["closed"], ["private"]], content: "" }, relaySk);
  const mem = finalizeEvent({ kind: 39002, created_at: Math.floor(Date.now() / 1000), tags: [["d", gid], ["p", pkS], ["p", pkL]], content: "" }, relaySk);
  await fetch(relayHttp + "/publish", { method: "POST", body: JSON.stringify([meta, mem]) });
  await S.ev(`location.hash = '#/c/${gid}'`); await sleep(3500);
  await S.ev(`(async () => { const i = document.querySelector('.composer textarea'); Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype, 'value').set.call(i, 'Plain as before'); i.dispatchEvent(new Event('input', { bubbles: true })); await new Promise((r) => setTimeout(r, 200)); const b = [...document.querySelectorAll('.composer button')].find((x) => /^Send/i.test(x.getAttribute('aria-label') || '') || x.classList.contains('send')); b && b.click() })()`);
  const sent = await until(async () => (await dump()).some((e) => e.kind === 9 && e.pubkey === pkS && e.content === "Plain as before"), 20000);
  let d = await dump();
  check("private group, format 1: a clear kind 9 by Sam, exactly as chat-source", !!sent);
  check("…no 8571 / 8572 anywhere", !d.some((e) => e.kind === 8571 || e.kind === 8572), d.filter((e) => e.kind === 8571 || e.kind === 8572).length);
  check("…no lock in the group's header", !(await S.ev("!!document.querySelector('.chead-bar .seal-lock')")));
  // a photo: a plain upload (PNG header), imeta without decryption fields
  const upBefore = (await (await fetch(relayHttp + "/uploads")).json()).length;
  await S.ev(`(async () => { const c = document.createElement('canvas'); c.width = 8; c.height = 8; const x = c.getContext('2d'); x.fillStyle = '#a73'; x.fillRect(0, 0, 8, 8);
    const b = await new Promise((r) => c.toBlob(r, 'image/png')); const dt = new DataTransfer(); dt.items.add(new File([b], 'plot.png', { type: 'image/png' }));
    document.querySelector('.composer textarea').dispatchEvent(new ClipboardEvent('paste', { clipboardData: dt, bubbles: true })); await new Promise((r) => setTimeout(r, 600));
    const s = [...document.querySelectorAll('.composer button')].find((x) => /^Send/i.test(x.getAttribute('aria-label') || '') || x.classList.contains('send')); s && s.click() })()`);
  const ups = await until(async () => { const u = (await (await fetch(relayHttp + "/uploads")).json()).slice(upBefore); return u.length ? u : null }, 30000);
  check("the photo: a plain upload (PNG header), as chat-source", !!ups && ups.every((u) => (u.head || "").startsWith("89504e47")), ups);
  const pic = await until(async () => (await dump()).find((e) => e.kind === 9 && e.pubkey === pkS && e.tags.some((t) => t[0] === "imeta")), 20000);
  check("…its kind 9 in clear, imeta without decryption-key", !!pic && !pic.tags.some((t) => t[0] === "imeta" && t.some((x) => /^decryption-/.test(x))), pic && pic.tags);
  // a clear message from another member: shown, and still there after a reload (never refused or deleted on format 1)
  const linaSk = new Uint8Array(crypto.createHash("sha256").update("osv-demo:lina@example.org").digest());
  const clear9 = finalizeEvent({ kind: 9, created_at: Math.floor(Date.now() / 1000), tags: [["h", gid]], content: "From Lina, the usual way" }, linaSk);
  await fetch(relayHttp + "/publish", { method: "POST", body: JSON.stringify([clear9]) });
  check("Lina's clear message shows", await until(() => has(S, "From Lina, the usual way"), 15000));
  await S.ev("location.reload()"); await sleep(5000);
  await S.ev(`location.hash = '#/c/${gid}'`); await sleep(2500);
  check("…and is still there after a reload (not deleted)", await has(S, "From Lina, the usual way"), await S.ev("document.body.innerText.slice(-300)"));
  // a DM: the version-1 / NIP-17 way, without delay
  await S.ev(`location.hash = '#/dm/' + encodeURIComponent(${JSON.stringify("n:" + pkL)})`); await sleep(1200);
  const t0 = Date.now();
  await typeSend(S, "A DM on format 1");
  const dm = await until(async () => (await dump()).some((e) => e.kind === 1059 && e.tags.some((t) => t[0] === "p" && t[1] === pkL)), 15000);
  const took = Date.now() - t0;
  d = await dump();
  check("the DM: a 1059 (v1 / NIP-17), no 8571 / 8572", !!dm && !d.some((e) => e.kind === 8571 || e.kind === 8572));
  check("…without delay (out within 3 s)", !!dm && took < 3000, took);
  await L.ev(`location.hash = '#/dm/' + encodeURIComponent(${JSON.stringify("n:" + pkS)})`);
  check("Lina reads the DM", await until(() => has(L, "A DM on format 1"), 20000));

  console.log(bad ? `== enc1: ${bad} failed` : "== enc1: all ok");
  for (const p of procs) try { p.kill() } catch {}
  await sleep(500); for (const n of ["lina", "sam"]) try { fs.rmSync(`${dir}/prof-enc1-${n}`, { recursive: true, force: true }) } catch {}
  process.exit(bad ? 1 : 0);
})();
