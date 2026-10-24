// Open Source Village directory: who's here, opt-in. Hash routes: #/ (everyone listed),
// #/p/<npub> (one person), #/me (your entry and the opt-in), #/signer.
import "../shared/style.css"
import { nip19 } from "nostr-tools"
import * as session from "../shared/session.js"
import { DIRECTORY_D, directoryConsent, latestByAuthor, parseProfile, profileName } from "../shared/model.js"
import { forgetProfile, isMe, loadProfiles, participants, publish, query, queryAuthors } from "../shared/nostr.js"
import { avatar, bindCommon, boot, chatLink, esc, header, notice, render, signInCard, signerPage, walletPayLink } from "../shared/ui.js"

const WHY = "The directory lists the participants who chose to be listed. Sign in to see it and to add yourself."

async function listed() {
  const active = [...(await participants())]
  const consent = latestByAuthor(await queryAuthors({ kinds: [30078], "#d": [DIRECTORY_D] }, active))
  const pubkeys = [...consent].filter(([, e]) => directoryConsent(e)).map(([pk]) => pk)
  const profiles = await loadProfiles(pubkeys)
  return pubkeys
    .map(pk => ({ pk, profile: profiles.get(pk) }))
    .sort((a, b) => profileName(a.profile, "~").localeCompare(profileName(b.profile, "~")))
}

function personCard({ pk, profile }) {
  const name = profileName(profile, "A participant")
  return `
    <a class="card person" href="#/p/${nip19.npubEncode(pk)}">
      ${avatar(profile)}
      <span class="person-text">
        <span class="person-name">${esc(name)}${isMe(pk) ? ' <span class="badge">you</span>' : ""}</span>
        ${profile?.nip05 ? `<span class="small muted">${esc(profile.nip05)}</span>` : ""}
        ${profile?.about ? `<span class="about">${esc(profile.about.slice(0, 140))}${profile.about.length > 140 ? "…" : ""}</span>` : ""}
      </span>
    </a>`
}

function failure(e) {
  if (e.status === 403) return '<p class="notice bad">The directory is for participants with an active pass.</p>'
  return `<p class="notice bad">Could not load the directory: ${esc(e.message)}</p>`
}

async function listPage() {
  render(`${header("Directory")}<div id="body"></div>`)
  bindCommon(route)
  const body = document.getElementById("body")
  if (!session.currentPubkey()) {
    body.innerHTML = signInCard(WHY)
    return bindCommon(route)
  }
  body.innerHTML = `
    <div class="row-end"><a class="button" href="#/me">Your entry</a></div>
    <input id="search" type="search" placeholder="Search names and descriptions" aria-label="Search" />
    <div id="people" class="stack"><p class="muted">Loading…</p></div>`
  let people
  try {
    people = await listed()
  } catch (e) {
    document.getElementById("people").innerHTML = failure(e)
    return
  }
  const box = document.getElementById("people")
  const show = term => {
    const t = term.trim().toLowerCase()
    const hits = people.filter(({ profile: p }) => !t || [p?.name, p?.displayName, p?.about, p?.nip05].some(v => v?.toLowerCase().includes(t)))
    box.innerHTML = hits.length
      ? `<p class="small muted">${hits.length} of ${people.length} listed</p>${hits.map(personCard).join("")}`
      : people.length
        ? '<p class="muted">Nobody matches.</p>'
        : '<p class="muted">Nobody is listed yet. <a href="#/me">Add yourself</a>.</p>'
  }
  show("")
  document.getElementById("search").oninput = e => show(e.target.value)
}

async function personPage(npubStr) {
  render(`${header("Participant", "#/")}<div id="body"><p class="muted">Loading…</p></div>`)
  bindCommon(route)
  const body = document.getElementById("body")
  if (!session.currentPubkey()) {
    body.innerHTML = signInCard(WHY)
    return bindCommon(route)
  }
  let pk
  try {
    pk = nip19.decode(npubStr).data
    if (!(await participants()).has(pk)) throw Object.assign(new Error("not an active participant"), { status: 404 })
  } catch (e) {
    body.innerHTML = e.status === 403 ? failure(e) : '<p class="notice bad">This is not a participant with an active pass.</p>'
    return
  }
  const profile = (await loadProfiles([pk])).get(pk)
  const chat = chatLink(pk)
  const pay = walletPayLink(pk, 10, "")
  body.innerHTML = `
    <div class="card stack">
      <div class="person">${avatar(profile, 72)}<span class="person-text"><span class="person-name big">${esc(profileName(profile, "A participant"))}</span>
      ${profile?.nip05 ? `<span class="small muted">${esc(profile.nip05)}</span>` : ""}</span></div>
      ${profile?.about ? `<p class="pre">${esc(profile.about)}</p>` : ""}
      ${profile?.website ? `<p><a href="${esc(profile.website)}" rel="noopener noreferrer" target="_blank">${esc(profile.website)}</a></p>` : ""}
      ${
        isMe(pk)
          ? '<a class="button" href="#/me">Edit your entry</a>'
          : `<div class="actions">${chat ? `<a class="primary button" href="${esc(chat)}">Message</a>` : ""}${pay ? `<a class="button" href="${esc(pay)}">Send tokens</a>` : ""}</div>`
      }
    </div>`
}

async function mePage() {
  render(`${header("Your entry", "#/")}<div id="body"><p class="muted">Loading…</p></div>`)
  bindCommon(route)
  const body = document.getElementById("body")
  const me = session.currentPubkey()
  if (!me) {
    body.innerHTML = signInCard(WHY)
    return bindCommon(route)
  }
  let profileEvent, consentEvent
  try {
    await participants()
    ;[profileEvent] = [...latestByAuthor(await query({ kinds: [0], authors: [me] })).values()]
    ;[consentEvent] = [...latestByAuthor(await query({ kinds: [30078], authors: [me], "#d": [DIRECTORY_D] })).values()]
  } catch (e) {
    body.innerHTML = failure(e)
    return
  }
  const p = parseProfile(profileEvent)
  const listedNow = directoryConsent(consentEvent)
  body.innerHTML = `
    <form class="card stack" id="entry">
      <label class="check"><input type="checkbox" id="listed" ${listedNow ? "checked" : ""} /> List me in the directory</label>
      <p class="small muted">Only participants with an active pass see the directory. Unticking takes you out of it again.
      Your profile is also what the chat and the other apps show.</p>
      <label>Name<input id="name" maxlength="80" value="${esc(p.displayName || p.name)}" /></label>
      <label>About you<textarea id="about" rows="4" maxlength="1000" placeholder="What you work on, what you'd like to talk about">${esc(p.about)}</textarea></label>
      <label>Picture (link to an image)<input id="picture" type="url" placeholder="https://…" value="${esc(p.picture)}" /></label>
      <label>Website<input id="website" type="url" placeholder="https://…" value="${esc(p.website)}" /></label>
      <button class="primary" type="submit">Save</button>
    </form>`
  document.getElementById("entry").onsubmit = async ev => {
    ev.preventDefault()
    const btn = ev.target.querySelector("button[type=submit]")
    btn.disabled = true
    try {
      // keep every field of the existing kind-0 content that this form does not edit
      let content = {}
      try {
        content = JSON.parse(profileEvent?.content || "{}") || {}
      } catch {
        content = {}
      }
      const val = id => document.getElementById(id).value.trim()
      const next = { ...content, display_name: val("name"), about: val("about"), picture: val("picture"), website: val("website") }
      if (!content.name && val("name")) next.name = val("name")
      for (const k of ["display_name", "about", "picture", "website"]) if (!next[k]) delete next[k]
      const now = Math.floor(Date.now() / 1000)
      if (JSON.stringify(next) !== JSON.stringify(content)) {
        profileEvent = await publish({ kind: 0, created_at: now, tags: profileEvent?.tags || [], content: JSON.stringify(next) })
        forgetProfile(me)
      }
      const wantListed = document.getElementById("listed").checked
      if (wantListed !== directoryConsent(consentEvent)) {
        consentEvent = await publish({ kind: 30078, created_at: now, tags: [["d", DIRECTORY_D]], content: JSON.stringify({ listed: wantListed }) })
      }
      notice(wantListed ? "Saved. You are listed in the directory." : "Saved. You are not listed in the directory.", "good")
    } catch (e) {
      notice(`Could not save: ${e.message}`, "bad")
    } finally {
      btn.disabled = false
    }
  }
}

function route() {
  const hash = location.hash || "#/"
  let m
  if ((m = hash.match(/^#\/p\/(npub1\w+)$/))) return personPage(m[1])
  if (hash === "#/me") return mePage()
  if (hash === "#/signer") return signerPage("Your own signer", () => (location.hash = "#/"))
  if (hash === "#/self-custody") return signerPage("Your own signer", () => (location.hash = "#/"), true)
  return listPage()
}

boot(route)
