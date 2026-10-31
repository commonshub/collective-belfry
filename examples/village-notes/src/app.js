// Village notes, the page. Follows the visitor skill of the Belfry
// (pass.<domain>/skills/open-source-village.md): the invite API (section 2), a bunker URL or a
// key held by this app (sections 6 and 8), /api/me, notes on the community relay.

import { finalizeEvent, getPublicKey, verifyEvent } from "nostr-tools/pure";
import { Relay } from "nostr-tools/relay";
import { BunkerSigner, parseBunkerInput } from "nostr-tools/nip46";
import * as nip19 from "nostr-tools/nip19";
import { generateMnemonic, mnemonicToSeedSync } from "@scure/bip39";
import { wordlist } from "@scure/bip39/wordlists/english.js";
import { hkdf } from "@noble/hashes/hkdf.js";
import { sha256 } from "@noble/hashes/sha2.js";
import { bytesToHex, hexToBytes, concatBytes, utf8ToBytes } from "@noble/hashes/utils.js";
import { secp256k1 } from "@noble/curves/secp256k1.js";

const $ = (id) => document.getElementById(id);
const now = () => Math.floor(Date.now() / 1000);
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

const store = {
  get(k) { try { return JSON.parse(localStorage.getItem("vn." + k)); } catch { return null; } },
  set(k, v) { try { localStorage.setItem("vn." + k, JSON.stringify(v)); } catch {} },
  del(k) { try { localStorage.removeItem("vn." + k); } catch {} },
};

let cfg; // the Belfry's /.well-known/belfry.json, plus its domain
let signer = null; // { pubkey, sign(template), close() }
let relay = null;

function show(section) {
  for (const s of ["signed-out", "invite", "signed-in"]) $(s).hidden = s !== section;
  $("logout").hidden = section !== "signed-in";
}

function el(tag, attrs = {}, ...children) {
  const e = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (k === "class") e.className = v;
    else e.setAttribute(k, v);
  }
  for (const c of children) e.append(c);
  return e;
}

// --- Keys and signers ---------------------------------------------------------------------

/** A Belfry key from a BIP-39 mnemonic (skill section 8): one key per Belfry. */
function deriveKey(mnemonic) {
  const seed = mnemonicToSeedSync(mnemonic);
  for (let v = 0; ; v++) {
    const sk = hkdf(sha256, seed, utf8ToBytes("belfry/member/v1"),
      utf8ToBytes(`belfry/nostr/v1/${cfg.domain}/${v}`), 32);
    if (secp256k1.utils.isValidSecretKey(sk)) return sk;
  }
}

function localSigner(mnemonic) {
  const sk = deriveKey(mnemonic);
  return { pubkey: getPublicKey(sk), sign: async (t) => finalizeEvent(t, sk), close() {} };
}

/** The bunker secret binds to the first client key that connects: keep one and reuse it. */
function clientKey() {
  let hex = store.get("clientKey");
  if (!hex) {
    hex = bytesToHex(secp256k1.utils.randomSecretKey());
    store.set("clientKey", hex);
  }
  return hexToBytes(hex);
}

async function bunkerSigner(url) {
  const bp = await parseBunkerInput(url);
  if (!bp) throw new Error("That is not a bunker:// URL.");
  const bunker = BunkerSigner.fromBunker(clientKey(), bp, {
    onauth: (authUrl) => window.open(authUrl, "_blank", "noopener"),
  });
  await bunker.connect();
  const pubkey = await bunker.getPublicKey();
  return {
    pubkey,
    async sign(t) {
      const ev = await bunker.signEvent(t);
      if (!verifyEvent(ev)) throw new Error("the signer returned a bad signature");
      return ev;
    },
    close: () => bunker.close(),
  };
}

async function nip07Signer() {
  const pubkey = await window.nostr.getPublicKey();
  return { pubkey, sign: (t) => window.nostr.signEvent(t), close() {} };
}

async function restore(session) {
  if (session.type === "local") return localSigner(session.mnemonic);
  if (session.type === "bunker") return bunkerSigner(session.bunker);
  if (session.type === "nip07") return nip07Signer();
  throw new Error("unknown session");
}

// --- HTTP: the Belfry's APIs through our proxy, NIP-98 signed -------------------------------

function b64(text) {
  return btoa(String.fromCharCode(...new TextEncoder().encode(text)));
}

async function nip98(sign, url, method, body) {
  const tags = [["u", url], ["method", method]];
  if (body) tags.push(["payload", bytesToHex(sha256(utf8ToBytes(body)))]);
  const ev = await sign({ kind: 27235, created_at: now(), tags, content: "" });
  return "Nostr " + b64(JSON.stringify(ev));
}

/** Calls a Belfry API (its public URL) through /proxy; returns { status, data }. */
async function api(url, { method = "GET", json, sign } = {}) {
  const body = json === undefined ? undefined : JSON.stringify(json);
  const headers = { accept: "application/json" };
  if (body) headers["content-type"] = "application/json";
  if (sign) headers.authorization = await nip98(sign, url, method, body);
  const res = await fetch("/proxy?url=" + encodeURIComponent(url), { method, headers, body });
  let data = null;
  try { data = await res.json(); } catch {}
  return { status: res.status, data };
}

const errorText = (r) =>
  r.data?.message || r.data?.error || (r.status === 429 ? "Too many requests, wait a minute." : `Error ${r.status}`);

// --- Invite and order ----------------------------------------------------------------------

let invite = null; // { token, url, timer }

function inviteToken(input) {
  const s = input.trim();
  const m = s.match(/\/invite\/([^/?#\s]+)/);
  if (m) return m[1];
  return /^[A-Za-z0-9_-]+$/.test(s) ? s : null;
}

async function openInvite() {
  $("invite-error").textContent = "";
  const token = inviteToken($("invite-input").value);
  if (!token) return ($("invite-error").textContent = "That doesn't look like an invite link.");
  invite = { token, url: cfg.api.invite_offer.replace("{token}", token) };
  show("invite");
  await loadOffer();
}

function money(amount, currency) {
  return new Intl.NumberFormat(undefined, { style: "currency", currency }).format(amount);
}

async function loadOffer() {
  clearTimeout(invite?.timer);
  const r = await api(invite.url);
  const offer = $("offer");
  offer.replaceChildren();
  $("order-card").hidden = $("payment-card").hidden = true;
  const pending = store.get("pending." + invite.token);
  $("mnemonic-card").hidden = !pending;
  if (pending) $("mnemonic").textContent = pending;

  if (r.status !== 200) {
    offer.append(el("p", { class: "error" }, errorText(r)));
    return;
  }
  const o = r.data;
  if (o.state === "open") {
    offer.append(el("p", {}, `Valid until ${new Date(o.expires_at).toLocaleString()}.`));
    if (o.early_bird_until) offer.append(el("p", { class: "muted" }, `Early-bird prices until ${o.early_bird_until}.`));
    const tier = $("tier");
    tier.replaceChildren(...o.passes.map((p) =>
      el("option", { value: p.tier }, `${p.name}: ${money(p.price, o.currency)} (VAT ${o.vat_rate}% included)`)));
    $("order-card").hidden = false;
  } else if (o.state === "reserved") {
    offer.append(el("p", {}, `Ordered: ${o.pass?.name || "a pass"}.`));
    renderPayment(o);
  } else {
    offer.append(el("p", {}, `This invite is ${o.state}.`));
  }
}

async function order() {
  $("order-error").textContent = "";
  const name = $("name").value.trim(), email = $("email").value.trim();
  if (!name || !email) return ($("order-error").textContent = "Name and e-mail, please.");
  const orderUrl = invite.url + "/order";
  const body = { tier: $("tier").value, name, email };
  let mnemonic = null;
  if ($("own-key").checked) {
    mnemonic = generateMnemonic(wordlist, 128);
    const sk = deriveKey(mnemonic);
    body.nostr_pubkey = getPublicKey(sk);
    body.nostr_proof = finalizeEvent({
      kind: 27235, created_at: now(), content: "",
      tags: [["u", orderUrl], ["method", "POST"]],
    }, sk);
  }
  $("order").disabled = true;
  const r = await api(orderUrl, { method: "POST", json: body });
  $("order").disabled = false;
  if (r.status === 201 || (r.status === 409 && r.data?.error === "reserved")) {
    if (mnemonic && r.status === 201) store.set("pending." + invite.token, mnemonic);
    await loadOffer();
  } else {
    $("order-error").textContent = errorText(r);
  }
}

function renderPayment(o) {
  const p = $("payment");
  p.replaceChildren();
  $("payment-card").hidden = false;
  const inv = o.invoice;
  if (inv) p.append(el("p", {}, `Invoice ${inv.number}: `, el("b", {}, money(inv.amount_total, inv.currency))));
  if (o.payment === "paid") {
    p.append(el("p", { class: "ok" }, "Paid. Your pass is being activated."));
    waitForPass();
    return;
  }
  if (o.payment === "processing") p.append(el("p", {}, "Your payment is under way."));
  if (o.payment_url) {
    const a = el("a", { href: o.payment_url, target: "_blank", rel: "noopener" }, "Pay now");
    p.append(el("p", {}, a, " (opens the payment page; come back here afterwards)"));
  }
  invite.timer = setTimeout(loadOffer, 15000);
}

async function waitForPass() {
  const mnemonic = store.get("pending." + invite.token);
  const p = $("payment");
  if (!mnemonic) {
    p.append(el("p", {}, "Watch for the activation e-mail. Then get a bunker URL on your account page and sign in here."));
    return;
  }
  const local = localSigner(mnemonic);
  for (let i = 0; i < 60; i++) {
    const r = await api(cfg.api.me, { sign: local.sign });
    if (r.status === 200 && r.data.member) {
      store.del("pending." + invite.token);
      store.set("session", { type: "local", mnemonic });
      return enter(local);
    }
    await sleep(5000);
  }
  p.append(el("p", {}, "The pass isn't active yet. Check again in a moment."));
}

// --- Signed in -----------------------------------------------------------------------------

async function communityRelay() {
  const r = await Relay.connect(cfg.relays.community);
  for (let i = 0; i < 50 && !r.challenge; i++) await sleep(100);
  if (r.challenge) await r.auth((t) => signer.sign(t));
  r.onauth = (t) => signer.sign(t);
  return r;
}

async function enter(s) {
  signer = s;
  show("signed-in");
  const me = await loadMe();
  loadWallet(me);
  if (me?.member === false) {
    $("notes").replaceChildren(el("p", { class: "muted" }, "The village relay opens once you hold an active pass."));
    $("note-post").disabled = true;
    return;
  }
  try {
    relay = await communityRelay();
    $("note-post").disabled = false;
    loadNotes();
    loadNutzaps();
  } catch (e) {
    $("notes").replaceChildren(el("p", { class: "error" }, "Could not reach the community relay: " + e.message));
  }
}

async function loadMe() {
  const box = $("me");
  const r = await api(cfg.api.me, { sign: signer.sign });
  if (r.status !== 200) {
    const why = r.status === 404 && r.data?.detail === "Not Found"
      ? `${new URL(cfg.api.me).host} has no /api/me yet (an older Belfry), so your pass can't be shown here.`
      : errorText(r);
    box.replaceChildren(
      el("p", {}, el("b", {}, nip19.npubEncode(signer.pubkey).slice(0, 20) + "…")),
      el("p", { class: "muted" }, why),
    );
    return null;
  }
  const me = r.data;
  const pass = me.passes.find((p) => p.state === "active") || me.passes[0];
  box.replaceChildren(
    el("p", {}, el("b", {}, me.nip05 || me.npub.slice(0, 20) + "…"), me.steward ? " · steward" : ""),
    el("p", { class: me.member ? "ok" : "muted" },
      pass ? `${pass.tier} pass, ${pass.state} (${pass.date_start} – ${pass.date_end})` : "No pass on this key."),
  );
  return me;
}

// Tokens: the pass's own bundle until a wallet collects it, plus nutzaps (NIP-61) to this key
// that are still unspent at the mint. What the wallet already holds lives in its seed, which
// only the wallet app knows: for that part the page shows what the pass brought (/api/me) and
// links the wallet.
const tokens = { brought: null, pass: null, nutzaps: null, error: "" };

function renderWallet() {
  const box = $("wallet");
  const known = [tokens.pass, tokens.nutzaps].filter((x) => x !== null);
  const waiting = known.reduce((a, b) => a + b, 0);
  const collected = Math.max((tokens.brought || 0) - (tokens.pass || 0), 0);
  const walletApp = cfg.apps.find((a) => a.name === "Wallet")?.url;
  const wallet = walletApp ? el("a", { href: walletApp, target: "_blank", rel: "noopener" }, "your wallet") : "your wallet";
  box.replaceChildren(el("p", { class: "big" }, known.length ? String(waiting + collected) : "…"));
  if (waiting) {
    box.append(el("p", { class: "muted" }, "waiting to be collected: " + [
      tokens.pass ? `${tokens.pass} from your pass` : "",
      tokens.nutzaps ? `${tokens.nutzaps} sent to you` : ""].filter(Boolean).join(", ")));
  }
  if (collected) {
    box.append(el("p", { class: "muted" }, `${collected} from your pass are in `, wallet,
      " (spending and receiving there is not counted here)."));
  } else if (!waiting && known.length && !tokens.error) {
    box.append(el("p", { class: "muted" }, "No tokens waiting. What you collected is in ", wallet, "."));
  }
  if (tokens.error) box.append(el("p", { class: "error" }, "Pass tokens: " + tokens.error));
}

async function loadWallet(me) {
  const pass = me?.passes?.find((p) => p.state === "active");
  tokens.brought = pass?.tokens ?? null;
  renderWallet();
  const r = await api(cfg.api.wallet_pass_tokens, { sign: signer.sign });
  if (r.status === 200) {
    tokens.pass = r.data.tokens.reduce((a, t) => a + (t.amount || 0), 0);
  } else {
    tokens.pass = 0;
    tokens.error = errorText(r);
  }
  renderWallet();
}

/** Cashu's hash_to_curve (NUT-00): Y for a proof's secret, as the mint's checkstate wants it. */
function hashToCurve(secret) {
  const msg = sha256(concatBytes(utf8ToBytes("Secp256k1_HashToCurve_Cashu_"), utf8ToBytes(secret)));
  for (let i = 0; i < 65536; i++) {
    const counter = new Uint8Array(4);
    new DataView(counter.buffer).setUint32(0, i, true);
    try {
      return secp256k1.Point.fromBytes(concatBytes(new Uint8Array([2]), sha256(concatBytes(msg, counter)))).toHex(true);
    } catch {}
  }
  throw new Error("hash_to_curve: no point");
}

function fetchAll(filter, ms = 6000) {
  return new Promise((resolve) => {
    const out = [];
    const sub = relay.subscribe([filter], {
      onevent: (e) => out.push(e),
      oneose: () => { sub.close(); resolve(out); },
    });
    setTimeout(() => { sub.close(); resolve(out); }, ms);
  });
}

async function loadNutzaps() {
  try {
    const zaps = await fetchAll({ kinds: [9321], "#p": [signer.pubkey], limit: 500 });
    const proofs = new Map();
    for (const z of zaps) {
      const mint = z.tags.find((t) => t[0] === "u")?.[1]?.replace(/\/$/, "");
      if (mint !== cfg.mint.url.replace(/\/$/, "")) continue;
      for (const t of z.tags) {
        if (t[0] !== "proof") continue;
        try {
          const p = JSON.parse(t[1]);
          if (p.secret && p.amount) proofs.set(p.secret, p.amount);
        } catch {}
      }
    }
    let sum = 0;
    if (proofs.size) {
      const secrets = [...proofs.keys()];
      const res = await fetch(cfg.mint.url + "/v1/checkstate", {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({ Ys: secrets.map(hashToCurve) }),
      });
      const { states } = await res.json();
      states.forEach((s, i) => { if (s.state === "UNSPENT") sum += proofs.get(secrets[i]); });
    }
    tokens.nutzaps = sum;
  } catch (e) {
    console.warn("nutzaps", e);
    tokens.nutzaps = 0;
  }
  renderWallet();
}

const notes = new Map();
const names = new Map();

function renderNotes() {
  const list = [...notes.values()].sort((a, b) => b.created_at - a.created_at).slice(0, 100);
  const box = $("notes");
  if (!list.length) return box.replaceChildren(el("p", { class: "muted" }, "No notes yet. Write the first one."));
  box.replaceChildren(...list.map((n) => el("div", { class: "note" },
    el("div", { class: "row" },
      el("span", { class: "who" }, names.get(n.pubkey) || nip19.npubEncode(n.pubkey).slice(0, 16) + "…"),
      el("span", { class: "muted" }, new Date(n.created_at * 1000).toLocaleString())),
    el("div", { class: "text" }, n.content))));
}

async function loadProfiles(pubkeys) {
  const missing = pubkeys.filter((p) => !names.has(p));
  if (!missing.length) return;
  for (const e of await fetchAll({ kinds: [0], authors: missing })) {
    try {
      const m = JSON.parse(e.content);
      names.set(e.pubkey, m.display_name || m.name || m.nip05 || "");
    } catch {}
  }
  renderNotes();
}

function loadNotes() {
  let live = false;
  let closed = false;
  relay.subscribe([{ kinds: [1], limit: 50 }], {
    onevent(e) {
      notes.set(e.id, e);
      if (live) { renderNotes(); loadProfiles([e.pubkey]); }
    },
    oneose() {
      live = true;
      if (closed) return;
      renderNotes();
      loadProfiles([...new Set([...notes.values()].map((n) => n.pubkey))]);
    },
    onclose(reason) {
      closed = true;
      // e.g. "restricted: …" for a key without an active pass
      if (reason && !notes.size) $("notes").replaceChildren(el("p", { class: "error" }, "The relay said: " + reason));
    },
  });
}

async function postNote() {
  const content = $("note-text").value.trim();
  if (!content) return;
  $("note-post").disabled = true;
  $("post-status").textContent = "Signing…";
  try {
    const ev = await signer.sign({ kind: 1, created_at: now(), tags: [], content });
    $("post-status").textContent = "Publishing…";
    await relay.publish(ev);
    notes.set(ev.id, ev);
    renderNotes();
    $("note-text").value = "";
    $("post-status").textContent = "Posted.";
  } catch (e) {
    $("post-status").textContent = "Not posted: " + (e.message || e);
  }
  $("note-post").disabled = false;
}

// --- Start ---------------------------------------------------------------------------------

async function loginBunker() {
  $("login-error").textContent = "";
  const url = $("bunker-input").value.trim();
  $("bunker-login").disabled = true;
  try {
    const s = await bunkerSigner(url);
    store.set("session", { type: "bunker", bunker: url });
    $("bunker-input").value = "";
    await enter(s);
  } catch (e) {
    $("login-error").textContent = e.message || String(e);
  }
  $("bunker-login").disabled = false;
}

// "Sign in with Open Source Village": OAuth authorization code + PKCE against Keycast, as the
// community chat does. Keycast sends the participant through the Belfry's sign-in page (e-mail,
// then the mailed 6-digit code or link) and, after consent, its token response carries a fresh
// bunker:// URL with the permissions "Belfry event apps". Keycast takes any client_id; its token
// endpoint answers every origin, so this needs no proxy.

const OAUTH_CLIENT = "village-notes";
const OAUTH_SCOPE = "policy:belfry-event";
const oauthRedirect = () => location.origin + "/oauth/callback";

const b64url = (bytes) =>
  btoa(String.fromCharCode(...bytes)).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");

async function loginSso() {
  const flow = { state: b64url(crypto.getRandomValues(new Uint8Array(32))),
    verifier: b64url(crypto.getRandomValues(new Uint8Array(48))) };
  store.set("oauth", flow);
  const challenge = b64url(new Uint8Array(
    await crypto.subtle.digest("SHA-256", new TextEncoder().encode(flow.verifier))));
  const url = new URL(cfg.sign_in.keycast.replace(/\/+$/, "") + "/api/oauth/authorize");
  url.search = new URLSearchParams({
    client_id: OAUTH_CLIENT, redirect_uri: oauthRedirect(), scope: OAUTH_SCOPE,
    state: flow.state, code_challenge: challenge, code_challenge_method: "S256",
  });
  location.assign(url);
}

/** Back from Keycast at /oauth/callback: exchanges the code for a bunker URL. */
async function finishSso() {
  const params = new URLSearchParams(location.search);
  const flow = store.get("oauth");
  store.del("oauth");
  history.replaceState(null, "", "/");
  if (!flow || params.get("state") !== flow.state) throw new Error("The sign-in did not start here; try again.");
  if (params.get("error")) throw new Error("Not signed in: " + params.get("error"));
  const res = await fetch(cfg.sign_in.keycast.replace(/\/+$/, "") + "/api/oauth/token", {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({
      grant_type: "authorization_code", code: params.get("code"), client_id: OAUTH_CLIENT,
      redirect_uri: oauthRedirect(), code_verifier: flow.verifier,
    }),
  });
  const body = await res.json().catch(() => ({}));
  if (!res.ok || !body.bunker_url) throw new Error("Not signed in: " + (body.error || `token ${res.status}`));
  const s = await bunkerSigner(body.bunker_url);
  store.set("session", { type: "bunker", bunker: body.bunker_url, sso: true,
    refresh: body.refresh_token || null });
  return s;
}

async function loginNip07() {
  try {
    const s = await nip07Signer();
    store.set("session", { type: "nip07" });
    await enter(s);
  } catch (e) {
    $("login-error").textContent = e.message || String(e);
  }
}

/** Signs out here; after "Sign in with Open Source Village" also everywhere (as the chat does):
 *  revokes this app's authorization and ends the Keycast and sign-in sessions, so the next sign-in
 *  asks for the e-mail code again. Keycast then returns to this page (the origin of an app the
 *  participant signed in to, Keycast patch 0019). */
async function logout() {
  const session = store.get("session");
  store.del("session");
  signer?.close();
  relay?.close();
  signer = relay = null;
  notes.clear();
  if (!session?.sso) return location.reload();
  const keycast = cfg.sign_in.keycast.replace(/\/+$/, "");
  if (session.refresh) {
    await fetch(keycast + "/api/oauth/revoke", {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ token: session.refresh, client_id: OAUTH_CLIENT }),
    }).catch(() => {});
  }
  location.assign(keycast + "/api/auth/oidc/logout?return_to=" + encodeURIComponent(location.origin + "/"));
}

async function main() {
  cfg = await (await fetch("/belfry.json")).json();
  $("community").textContent = `${cfg.name} · ${cfg.domain}`;
  $("apps-link").href = cfg.sign_in.keycast;
  $("apps-link").textContent = new URL(cfg.sign_in.keycast).host;
  $("invite-open").onclick = openInvite;
  $("invite-back").onclick = () => { clearTimeout(invite?.timer); show("signed-out"); };
  $("order").onclick = order;
  $("payment-check").onclick = loadOffer;
  $("bunker-login").onclick = loginBunker;
  $("nip07-login").onclick = loginNip07;
  $("sso-login").onclick = loginSso;
  $("note-post").onclick = postNote;
  $("logout").onclick = logout;
  setTimeout(() => { $("nip07-login").hidden = !window.nostr; }, 300);

  if (location.pathname === "/oauth/callback") {
    try {
      return await enter(await finishSso());
    } catch (e) {
      show("signed-out");
      $("login-error").textContent = e.message || String(e);
      return;
    }
  }

  const invited = new URLSearchParams(location.search).get("invite");
  const session = store.get("session");
  if (session) {
    try {
      return await enter(await restore(session));
    } catch (e) {
      $("login-error").textContent = "Could not sign in again: " + (e.message || e);
    }
  }
  show("signed-out");
  if (invited) { $("invite-input").value = invited; openInvite(); }
}

main();
