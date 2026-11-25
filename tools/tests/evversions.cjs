// An event's co-host version made elsewhere and dated before this device's last sync (Xavier, OSV h9ptvr,
// 25 Nov): after a reload the catch-up skips it; opening the event asks every version by #d (on a signed-in
// socket) and the newest host version wins. One browser, the local relay (format-1 build dist-enc1).
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
  const prof = `${dir}/prof-evv-${name}`; fs.rmSync(prof, { recursive: true, force: true });
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
  const S = await browser("sam", 9492, "sam@example.org");
  const pkS = await S.ev("window.__demoMe()");
  const relaySk = sk("groups-relay"), co = sk("co-host"), pkC = getPublicKey(co), d = "h9ptvr", gid = "ev-" + d, now = Math.floor(Date.now() / 1000);
  const samSk = new Uint8Array(crypto.createHash("sha256").update("osv-demo:sam@example.org").digest());
  // Sam made the event (its group and its first version); the co-host is an admin of its group
  const v1 = finalizeEvent({ kind: 31922, created_at: now - 7 * 3600, tags: [["d", d], ["title", "Old title"], ["start", "2027-01-28"], ["h", gid], ["visibility", "members"]], content: "Day 4 of…" }, samSk);
  await fetch(relayHttp + "/publish", { method: "POST", body: JSON.stringify([
    finalizeEvent({ kind: 9007, created_at: now - 7 * 3600, tags: [["h", gid]], content: "" }, samSk),
    finalizeEvent({ kind: 39000, created_at: now - 7 * 3600, tags: [["d", gid], ["name", "Old title"]], content: "" }, relaySk),
    finalizeEvent({ kind: 39001, created_at: now - 7 * 3600, tags: [["d", gid], ["p", pkS, "admin"], ["p", pkC, "admin"]], content: "" }, relaySk),
    v1]) });
  await S.ev("location.reload()"); await sleep(6000);
  await S.ev(`location.hash = '#/events/${d}'`); await sleep(2500);
  check("the event shows (the first version)", /Old title/.test(await S.ev("document.querySelector('.ev-screen .topbar h1')?.innerText || ''")), await S.ev("document.body.innerText.slice(0, 200)"));
  // something newer arrives, so this device's sync mark moves past the co-host's version-to-come
  await fetch(relayHttp + "/publish", { method: "POST", body: JSON.stringify([finalizeEvent({ kind: 0, created_at: now, tags: [], content: JSON.stringify({ name: "Sam" }) }, samSk)]) });
  await sleep(2500);
  await S.ev("location.hash = '#/home'"); await sleep(500);
  // the co-host edits it elsewhere: a version dated 3 hours ago (older than this device's mark − 1 h)
  await fetch(relayHttp + "/publish", { method: "POST", body: JSON.stringify([finalizeEvent({ kind: 31922, created_at: now - 3 * 3600, tags: [["d", d], ["title", "CHAOSScon"], ["start", "2027-01-28"], ["h", gid], ["visibility", "members"], ["image", "https://example.org/c.jpg"]], content: "The full description" }, co)]) });
  // reopened straight onto the event (a home-screen app, a link): the ask goes out before the relay signed us in
  await S.ev(`location.hash = '#/events/${d}'; location.reload()`); await sleep(9000);
  const t = await S.ev("document.querySelector('.ev-screen .topbar h1')?.innerText || ''");
  check("opening it: the co-host's newer version wins (asked by #d on a signed-in socket)", /CHAOSScon/.test(t) && !/Old title/.test(t), t);
  console.log(bad ? `== evversions: ${bad} failed` : "== evversions: all ok");
  for (const p of procs) try { p.kill() } catch {}
  await sleep(500); try { fs.rmSync(`${dir}/prof-evv-sam`, { recursive: true, force: true }) } catch {}
  process.exit(bad ? 1 : 0);
})();
