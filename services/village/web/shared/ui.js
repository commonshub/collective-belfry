// Page shell shared by the directory and the marketplace: header, sign-in, the own-signer page,
// links into the chat and the wallet, and the boot sequence (callback, restore, silent sign-in).
import * as session from "./session.js"
import { npub } from "./nostr.js"
import { avatarUrl } from "./avatar.js"
import { SSO_CALLBACK_PATH, endSsoSession, finishSso, shouldTrySilentSso, ssoEnabled, startSso } from "./sso.js"

const CHAT_URL = (import.meta.env.VITE_CHAT_URL || "").replace(/\/$/, "")
const WALLET_URL = (import.meta.env.VITE_WALLET_URL || "").replace(/\/$/, "")
const app = document.getElementById("app")

export const esc = s =>
  String(s ?? "").replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c])

export const render = html => (app.innerHTML = html)

export function notice(text, kind = "info") {
  const el = document.getElementById("notice")
  if (el) {
    el.className = `notice ${kind}`
    el.textContent = text
    el.hidden = !text
  }
}

export function header(title, back = null) {
  return `
    <header class="top">
      ${back ? `<a class="back" href="${back}" aria-label="Back">&larr;</a>` : ""}
      <h1>${esc(title)}</h1>
      ${session.currentPubkey() ? '<button class="link" id="signout">Log out</button>' : ""}
    </header>
    <p id="notice" class="notice" hidden></p>`
}

export function signInCard(why) {
  if (!ssoEnabled) return '<div class="card"><p>Sign-in is not configured.</p></div>'
  return `
    <div class="card signin">
      <p>${esc(why)}</p>
      <button class="primary" id="signin">Sign in with Open Source Village</button>
      <p class="small"><a href="#/signer">Use your own signer</a> (if you took your key into your own custody)</p>
    </div>`
}

/** The participant's picture, or the default avatar drawn from their key (shared/avatar.js);
 * a picture that fails to load falls back to the default as well. */
export const avatar = (profile, size = 48, pubkey = "") => {
  const fallback = avatarUrl(pubkey)
  return profile?.picture
    ? `<img class="avatar" src="${esc(profile.picture)}" alt="" width="${size}" height="${size}" loading="lazy" referrerpolicy="no-referrer" data-fallback="${esc(fallback)}" />`
    : `<img class="avatar" src="${esc(fallback)}" alt="" width="${size}" height="${size}" />`
}
// one listener for every picture that fails (error events do not bubble; the CSP allows no inline handlers)
document.addEventListener(
  "error",
  e => {
    const img = e.target
    if (img instanceof HTMLImageElement && img.dataset.fallback && img.src !== img.dataset.fallback) img.src = img.dataset.fallback
  },
  true,
)

/** The participant's profile in the chat (Coracle), where "Message" starts a private chat. */
export const chatLink = pk => (CHAT_URL ? `${CHAT_URL}/people/${npub(pk)}` : "")

/** The wallet's send dialog, prefilled (wallet patch 0003); it comes back to `returnTo`. */
export function walletPayLink(pk, amount, memo, ref, returnTo = location.href) {
  if (!WALLET_URL) return ""
  const p = new URLSearchParams({ pay: npub(pk), amount: String(amount) })
  if (memo) p.set("memo", memo)
  if (ref) p.set("ref", ref)
  if (returnTo) p.set("return", returnTo)
  return `${WALLET_URL}/?${p}`
}

export function bindCommon(onChange) {
  const si = document.getElementById("signin")
  if (si) si.onclick = () => startSso(false)
  const so = document.getElementById("signout")
  if (so)
    so.onclick = async () => {
      await session.signOut()
      await endSsoSession()
      onChange()
    }
}

/** Sign in with a browser extension (NIP-07) or a pasted bunker URL. */
export function signerPage(title, onDone, selfCustody = false) {
  render(`${header(title, "#/")}
    <div class="card stack">
      ${selfCustody ? '<p class="notice info">You hold your own key now, so this app signs in with your own signer.</p>' : ""}
      ${session.hasExtension() ? '<button class="primary" id="ext">Use my browser extension</button>' : '<p class="muted">No Nostr browser extension found.</p>'}
      <label>Or a bunker URL from your signer app<input id="bunker" placeholder="bunker://…" /></label>
      <button id="connect">Connect</button>
    </div>`)
  const ext = document.getElementById("ext")
  if (ext)
    ext.onclick = async () => {
      try {
        await session.connectExtension()
        onDone()
      } catch (e) {
        notice(`Could not use the extension: ${e.message}`, "bad")
      }
    }
  const btn = document.getElementById("connect")
  btn.onclick = async () => {
    btn.disabled = true
    btn.textContent = "Connecting…"
    try {
      await session.connect(document.getElementById("bunker").value.trim())
      onDone()
    } catch (e) {
      btn.disabled = false
      btn.textContent = "Connect"
      notice(`Could not connect: ${e.message}`, "bad")
    }
  }
}

/** Callback handling, restoring the signer, one silent sign-in per tab; then `route` on every hash. */
export async function boot(route) {
  if (location.pathname === SSO_CALLBACK_PATH) {
    render('<p class="muted center">Signing in…</p>')
    const result = await finishSso(location.search)
    history.replaceState(null, "", `/${result.description === "self_custody" ? "#/self-custody" : result.returnTo}`)
    if (result.ok) {
      try {
        await session.connect(result.bunkerUrl)
      } catch (e) {
        console.warn("NIP-46 connect failed", e)
      }
    }
  } else if (!session.restore() && shouldTrySilentSso()) {
    return startSso(true)
  }
  window.addEventListener("hashchange", route)
  route()
  const foot = document.createElement("footer")
  foot.textContent = "Open Source Village, run by Belfry"
  document.body.appendChild(foot)
}
