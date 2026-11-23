// A small in-memory NIP-01/NIP-42 relay for sealedwire.cjs (no npm ws). Keeps every event, answers
// REQs then keeps them live; GET /dump gives every event as JSON (the test looks at what crossed
// the wire). Like belfry's relay it ignores multi-letter tag filters (#lb): never filter on them.
//   node sealedrelay.cjs <port>
const http = require("http"), crypto = require("crypto");
const [, , port] = process.argv;
const events = []; const subs = new Set(); const socks = new Set();
let silent = false;
let nolive = false; // GET /nolive/on|off: published events are stored but not pushed to open subscriptions (a sleeping tab's missed events, catchup.cjs)
const closeReq = { n: 0, reason: '' }; // GET /closereq/<n>?reason=…: the next n REQs get CLOSED with that reason (notin.cjs)
const ratelimit = {};
// GET /prule/on|off (docs/encrypted.md §18.1): kinds 8571–8573 go only to a connection authenticated as the event's p
// account, and one dated more than 5 minutes from now is refused (enc.cjs)
let prule = false;
const PRULE = new Set([8571, 8572, 8573]);
// (a group's 8571/8572 carry h and live on the groups relay, which has no such rule: §22)
const hidden = (e, who) => prule && PRULE.has(e.kind) && !e.tags.some((t) => t[0] === "h") && !e.tags.some((t) => t[0] === "p" && t[1] === who);
const blobs = new Map(); const uploads = [];
const refuse = {}; // GET /refuse/<kind or 'anchor'>/<n>: the next n EVENTs of that kind (anchor: 30078 d belfry:sealed) get OK false // GET /silent/on: connections stay open, REQs get no answer (no EOSE), EVENTs no OK
function match(e, f) {
  if (f.ids && !f.ids.includes(e.id)) return false;
  if (f.kinds && !f.kinds.includes(e.kind)) return false;
  if (f.authors && !f.authors.includes(e.pubkey)) return false;
  if (f.since && e.created_at < f.since) return false;
  if (f.until && e.created_at > f.until) return false;
  for (const k of Object.keys(f)) if (k[0] === "#" && k.length === 2 && !e.tags.some((t) => t[0] === k.slice(1) && f[k].includes(t[1]))) return false;
  return true;
}
// replaceable events (NIP-01): one per pubkey+kind (0, 3, 10000–19999) or pubkey+kind+d (30000–39999):
// the newer created_at stays, the lower id on a tie; an older one is refused
const replKey = (e) => (e.kind === 0 || e.kind === 3 || (e.kind >= 10000 && e.kind < 20000)) ? `${e.pubkey}:${e.kind}`
  : (e.kind >= 30000 && e.kind < 40000) ? `${e.pubkey}:${e.kind}:${(e.tags.find((t) => t[0] === "d") || [])[1] || ""}` : null;
function takeReplaceable(e) {
  const k = replKey(e); if (!k) return true;
  const i = events.findIndex((x) => replKey(x) === k);
  if (i < 0) return true;
  const o = events[i];
  if (e.created_at > o.created_at || (e.created_at === o.created_at && e.id < o.id)) { events.splice(i, 1); return true; }
  return false;
}
function frame(text) {
  const p = Buffer.from(text); let h;
  if (p.length < 126) h = Buffer.from([0x81, p.length]);
  else if (p.length < 65536) { h = Buffer.alloc(4); h[0] = 0x81; h[1] = 126; h.writeUInt16BE(p.length, 2); }
  else { h = Buffer.alloc(10); h[0] = 0x81; h[1] = 127; h.writeBigUInt64BE(BigInt(p.length), 2); }
  return Buffer.concat([h, p]);
}
const srv = http.createServer((q, s) => {
  // GET /ratelimit/<kind>/<n>: the next n EVENTs of that kind get OK false "rate-limited: …" (a refusal for now, tried again)
  const cr = /^\/closereq\/(\d+)(?:\?reason=(.*))?$/.exec(q.url || "");
  if (cr) { closeReq.n = Number(cr[1]); closeReq.reason = decodeURIComponent(cr[2] || "restricted"); s.writeHead(200); s.end("ok"); return; }
  const rl = /^\/ratelimit\/(\d+)\/(\d+)$/.exec(q.url || "");
  if (rl) { ratelimit[rl[1]] = Number(rl[2]); s.writeHead(200); s.end("ok"); return; }
  const rf = /^\/refuse\/(\d+|anchor)\/(\d+)$/.exec(q.url || "");
  if (rf) { refuse[rf[1]] = Number(rf[2]); s.writeHead(200); s.end("ok"); return; }
  // back from silence like a real outage ends: every connection drops, the clients reconnect and sign in again
  if (q.url === "/prule/on" || q.url === "/prule/off") { prule = q.url.endsWith("on"); s.writeHead(200); s.end(String(prule)); return; }
  if (q.url === "/nolive/on" || q.url === "/nolive/off") { nolive = q.url.endsWith("on"); s.writeHead(200); s.end(String(nolive)); return; }
  if (q.url === "/silent/on" || q.url === "/silent/off") { silent = q.url.endsWith("on"); if (!silent) for (const k of socks) { try { k.destroy(); } catch {} } s.writeHead(200); s.end(String(silent)); return; }
  if (q.url === "/publish" && q.method === "POST") { let b = ""; q.on("data", (c) => (b += c)); q.on("end", () => { for (const e of JSON.parse(b)) if (!events.some((x) => x.id === e.id) && takeReplaceable(e)) { events.push(e); if (!nolive) for (const sub of subs) if (!hidden(e, sub.who()) && sub.fs.some((f) => match(e, f))) sub.send(["EVENT", sub.sid, e]); } s.writeHead(200); s.end("ok"); }); return; }
  // a Blossom stub (dmfiles-live.cjs): PUT /upload keeps the body under its sha256, GET /<sha256> serves it
  const cors = { "access-control-allow-origin": "*", "access-control-allow-headers": "authorization, content-type", "access-control-allow-methods": "GET, PUT, OPTIONS" };
  if (q.method === "OPTIONS") { s.writeHead(204, cors); s.end(); return; }
  if (q.method === "PUT" && q.url === "/upload") { const parts = []; q.on("data", (c) => parts.push(c)); q.on("end", () => { const b = Buffer.concat(parts); const x = crypto.createHash("sha256").update(b).digest("hex"); blobs.set(x, b); uploads.push({ x, type: q.headers["content-type"], size: b.length, head: b.subarray(0, 8).toString("hex") }); s.writeHead(200, { ...cors, "content-type": "application/json" }); s.end(JSON.stringify({ url: `http://${q.headers.host}/${x}`, sha256: x, size: b.length })); }); return; }
  const bx = /^\/([0-9a-f]{64})$/.exec(q.url || "");
  if (q.method === "GET" && bx) { const b = blobs.get(bx[1]); s.writeHead(b ? 200 : 404, { ...cors, "content-type": "application/octet-stream" }); s.end(b || ""); return; }
  if (q.url === "/uploads") { s.writeHead(200, { "content-type": "application/json" }); s.end(JSON.stringify(uploads)); return; }
  if (q.url === "/dump") { s.writeHead(200, { "content-type": "application/json" }); s.end(JSON.stringify(events)); return; }
  s.writeHead(200, { "content-type": "application/nostr+json", "access-control-allow-origin": "*" }); s.end(JSON.stringify({ name: "sealed-test", supported_nips: [1, 42] }));
});
srv.on("upgrade", (req, sock) => {
  const accept = crypto.createHash("sha1").update(req.headers["sec-websocket-key"] + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").digest("base64");
  sock.write(`HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\nSec-WebSocket-Accept: ${accept}\r\n\r\n`);
  let buf = Buffer.alloc(0); const mine = new Map(); socks.add(sock);
  let authed = ""; // the pubkey this connection authenticated as (not verified: a test relay)
  const challenge = crypto.randomBytes(8).toString("hex");
  const send = (m) => { try { sock.write(frame(JSON.stringify(m))); } catch {} };
  setTimeout(() => send(["AUTH", challenge]), 50);
  const onMsg = (m) => {
    if (silent && m[0] !== "CLOSE") return;
    if (m[0] === "AUTH") { authed = (m[1] && m[1].pubkey) || ""; send(["OK", m[1].id, true, ""]); return; }
    if (m[0] === "REQ" && closeReq.n > 0) { closeReq.n--; send(["CLOSED", m[1], closeReq.reason]); return; }
    if (m[0] === "REQ") {
      const [, sid, ...fs_] = m; const seen = new Set();
      for (const f of fs_) {
        let list = events.filter((e) => match(e, f)).sort((a, b) => b.created_at - a.created_at);
        if (f.limit) list = list.slice(0, f.limit);
        for (const e of list) if (!seen.has(e.id) && !hidden(e, authed)) { seen.add(e.id); send(["EVENT", sid, e]); }
      }
      send(["EOSE", sid]);
      const sub = { sid, fs: fs_, send, who: () => authed }; mine.set(sid, sub); subs.add(sub); return;
    }
    if (m[0] === "CLOSE") { const s = mine.get(m[1]); if (s) { subs.delete(s); mine.delete(m[1]); } return; }
    if (m[0] === "EVENT") {
      const e = m[1];
      const rk = e.kind === 30078 && e.tags.some((t) => t[0] === "d" && t[1] === "belfry:sealed") && refuse.anchor > 0 ? "anchor" : refuse[e.kind] > 0 ? e.kind : null;
      if (rk !== null) { refuse[rk]--; send(["OK", e.id, false, "blocked: refused by the test"]); return; }
      if (prule && PRULE.has(e.kind) && Math.abs(e.created_at - Date.now() / 1000) > 300) { send(["OK", e.id, false, "invalid: created_at more than 5 minutes from now"]); return; }
      if (ratelimit[e.kind] > 0) { ratelimit[e.kind]--; send(["OK", e.id, false, "rate-limited: slow down (test)"]); return; }
      if (!events.some((x) => x.id === e.id)) {
        if (!takeReplaceable(e)) { send(["OK", e.id, false, "duplicate: have a newer event"]); return; }
        events.push(e); for (const s of subs) if (!hidden(e, s.who()) && s.fs.some((f) => match(e, f))) s.send(["EVENT", s.sid, e]);
      }
      send(["OK", e.id, true, ""]); return;
    }
  };
  sock.on("data", (d) => {
    buf = Buffer.concat([buf, d]);
    while (buf.length >= 2) {
      const op = buf[0] & 0x0f; let len = buf[1] & 0x7f, off = 2;
      if (len === 126) { if (buf.length < 4) return; len = buf.readUInt16BE(2); off = 4; }
      else if (len === 127) { if (buf.length < 10) return; len = Number(buf.readBigUInt64BE(2)); off = 10; }
      const masked = buf[1] & 0x80; const need = off + (masked ? 4 : 0) + len; if (buf.length < need) return;
      let p = buf.subarray(off + (masked ? 4 : 0), need);
      if (masked) { const k = buf.subarray(off, off + 4); p = Buffer.from(p.map((b, i) => b ^ k[i % 4])); }
      buf = buf.subarray(need);
      if (op === 8) { sock.end(); return; }
      if (op === 9) { sock.write(Buffer.from([0x8a, 0])); continue; }
      if (op === 1) { try { onMsg(JSON.parse(p.toString())); } catch {} }
    }
  });
  const gone = () => { for (const s of mine.values()) subs.delete(s); mine.clear(); socks.delete(sock); };
  sock.on("close", gone); sock.on("error", gone);
});
srv.listen(Number(port), "127.0.0.1");
