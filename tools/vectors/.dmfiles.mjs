// src/core/dmfiles.ts
var FILE_KIND = 15;
var MAX_FILE = 25e6;
var MAX_FILE_GUEST = 5e6;
var UNREADABLE = "This file can't be opened";
var tooLarge = (max) => `This file is too large (${Math.round(max / 1e6)} MB at most)`;
var hex = (b) => Array.from(b, (x) => x.toString(16).padStart(2, "0")).join("");
var unhex = (s) => new Uint8Array((s.match(/../g) ?? []).map((h) => parseInt(h, 16)));
var isHex = (s, bytes) => typeof s === "string" && s.length === bytes * 2 && /^[0-9a-f]+$/.test(s);
var buf = (b) => b.buffer.slice(b.byteOffset, b.byteOffset + b.byteLength);
async function sha256hex(b) {
  return hex(new Uint8Array(await crypto.subtle.digest("SHA-256", buf(b))));
}
async function gcm(key, nonce, data, how) {
  const k = await crypto.subtle.importKey("raw", buf(key), "AES-GCM", false, [how]);
  const out = how === "encrypt" ? await crypto.subtle.encrypt({ name: "AES-GCM", iv: buf(nonce) }, k, buf(data)) : await crypto.subtle.decrypt({ name: "AES-GCM", iv: buf(nonce) }, k, buf(data));
  return new Uint8Array(out);
}
async function encryptFile(plain, key, nonce) {
  const k = key ? unhex(key) : crypto.getRandomValues(new Uint8Array(32));
  const n = nonce ? unhex(nonce) : crypto.getRandomValues(new Uint8Array(12));
  const cipher = await gcm(k, n, plain, "encrypt");
  return { cipher, key: hex(k), nonce: hex(n), x: await sha256hex(cipher), ox: await sha256hex(plain) };
}
async function encryptThumb(plain, key, nonce) {
  const n = nonce ? unhex(nonce) : crypto.getRandomValues(new Uint8Array(12));
  return { cipher: await gcm(unhex(key), n, plain, "encrypt"), nonce: hex(n) };
}
function fileTags(others, s, m) {
  const t = [
    ...others.map((p) => ["p", p]),
    ["file-type", m.mime],
    ["encryption-algorithm", "aes-gcm"],
    ["decryption-key", s.key],
    ["decryption-nonce", s.nonce],
    ["x", s.x],
    ["ox", s.ox],
    ["size", String(m.size)]
  ];
  if (m.dim) t.push(["dim", m.dim]);
  if (m.blurhash) t.push(["blurhash", m.blurhash]);
  if (m.thumb && m.thumbNonce) t.push(["thumb", m.thumb], ["thumb-nonce", m.thumbNonce]);
  if (m.duration !== void 0) t.push(["duration", String(Math.round(m.duration))]);
  if (m.group) t.push(["group", m.group]);
  return t;
}
var tagOf = (tags, k) => tags.find((t) => t[0] === k)?.[1];
function fileRefOf(r) {
  const t = r.tags;
  const key = tagOf(t, "decryption-key"), nonce = tagOf(t, "decryption-nonce"), x = tagOf(t, "x"), ox = tagOf(t, "ox");
  if (tagOf(t, "encryption-algorithm") !== "aes-gcm" || !isHex(key, 32) || !isHex(nonce, 12) || !isHex(x, 32) || !isHex(ox, 32)) return null;
  const url = r.content.trim();
  if (!/^https?:\/\/\S+$/.test(url)) return null;
  const size = Number(tagOf(t, "size"));
  const dimM = /^(\d{1,5})x(\d{1,5})$/.exec(tagOf(t, "dim") ?? "");
  const dur = Number(tagOf(t, "duration"));
  const thumb = tagOf(t, "thumb"), thumbNonce = tagOf(t, "thumb-nonce");
  return {
    url,
    mime: (tagOf(t, "file-type") || "application/octet-stream").toLowerCase(),
    key,
    nonce,
    x,
    ox,
    size: Number.isFinite(size) && size >= 0 ? size : 0,
    ...dimM ? { dim: [Number(dimM[1]), Number(dimM[2])] } : {},
    ...Number.isFinite(dur) && dur > 0 ? { duration: dur } : {},
    ...thumb && /^https:\/\//.test(thumb) && isHex(thumbNonce, 12) ? { thumb, thumbNonce } : {},
    ...isHex(tagOf(t, "group"), 8) ? { group: tagOf(t, "group") } : {}
  };
}
async function openFile(tags, downloaded, thumb = false) {
  const key = tagOf(tags, "decryption-key"), nonce = tagOf(tags, thumb ? "thumb-nonce" : "decryption-nonce"), x = tagOf(tags, "x"), ox = tagOf(tags, "ox");
  if (tagOf(tags, "encryption-algorithm") !== "aes-gcm" || !isHex(key, 32) || !isHex(nonce, 12) || !isHex(x, 32) || !isHex(ox, 32)) return { ok: false };
  if (!thumb && await sha256hex(downloaded) !== x) return { ok: false };
  let plain;
  try {
    plain = await gcm(unhex(key), unhex(nonce), downloaded, "decrypt");
  } catch {
    return { ok: false };
  }
  if (!thumb && await sha256hex(plain) !== ox) return { ok: false };
  return { ok: true, plain, mime: (tagOf(tags, "file-type") || "application/octet-stream").toLowerCase() };
}
function bubbles(rumors) {
  const out = /* @__PURE__ */ new Map();
  const order = (a, b) => a.created_at - b.created_at || (a.id < b.id ? -1 : a.id > b.id ? 1 : 0);
  for (const r of [...rumors].sort(order)) {
    const k = r.group ? `${r.pubkey}:${r.group}` : r.id;
    const b = out.get(k) ?? { files: [], caption: null, at: r.created_at, first: r.id };
    if (r.kind === FILE_KIND) b.files.push(r);
    else if (!b.caption) b.caption = r;
    out.set(k, b);
  }
  return [...out.values()].sort((a, b) => a.at - b.at || (a.first < b.first ? -1 : 1)).map(({ files, caption, at }) => ({ files, caption, at }));
}
function fileLabel(mime) {
  const sub = mime.split("/")[1] ?? "";
  if (mime === "application/pdf") return "PDF";
  if (mime.startsWith("video/")) return "Video";
  if (mime.startsWith("audio/")) return "Recording";
  if (mime.startsWith("image/")) return "Picture";
  if (/word|document/.test(sub)) return "Document";
  if (/sheet|excel|csv/.test(sub)) return "Spreadsheet";
  if (/presentation|powerpoint/.test(sub)) return "Slides";
  if (/zip|compressed|tar/.test(sub)) return "Archive";
  if (mime.startsWith("text/")) return "Text";
  return "File";
}
var sizeLabel = (n) => n >= 1e6 ? `${(n / 1e6).toFixed(1)} MB` : `${Math.max(1, Math.round(n / 1e3))} kB`;
export {
  FILE_KIND,
  MAX_FILE,
  MAX_FILE_GUEST,
  UNREADABLE,
  bubbles,
  encryptFile,
  encryptThumb,
  fileLabel,
  fileRefOf,
  fileTags,
  openFile,
  sha256hex,
  sizeLabel,
  tooLarge
};
