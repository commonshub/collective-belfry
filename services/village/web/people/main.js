// Open Source Village directory: who's here, opt-in. Hash routes: #/ (everyone listed),
// #/p/<npub> (one person), #/me (your entry and the opt-in), #/activity (what's new in the
// apps), #/signer.
import "../shared/style.css"
import { nip19 } from "nostr-tools"
import * as session from "../shared/session.js"
import { ACTIVITY_KINDS, DIRECTORY_D, LISTING_KIND, activityFromEvent, deletedIds, directoryConsent, isHidden, latestByAuthor, parseProfile, profileName } from "../shared/model.js"
import { forgetProfile, isMe, loadProfiles, participants, publish, query, queryAuthors } from "../shared/nostr.js"
import { avatar, bindCommon, boot, chatLink, esc, header, notice, render, signInCard, signerPage, walletPayLink } from "../shared/ui.js"
import { bindReports, hiddenRefs, reportLink } from "../shared/report.js"

const WHY = "The directory lists the participants who chose to be listed. Sign in to see it and to add yourself."
const appUrl = name => (import.meta.env[`VITE_${name}_URL`] || "").replace(/\/$/, "")
const URLS = { chat: appUrl("CHAT"), market: appUrl("MARKET"), photos: appUrl("PHOTOS"), calendar: appUrl("CALENDAR"), programme: appUrl("PROGRAMME") }
const ACTIVITY_DAYS = 14
// the polls app's key: its notes (a poll opened, a result) show in What's new
const POLLS_KEY = (import.meta.env.VITE_POLLS_PUBKEY || "").trim()

async function listed() {
  const active = [...(await participants())]
  const [consent, hidden] = await Promise.all([
    queryAuthors({ kinds: [30078], "#d": [DIRECTORY_D] }, active).then(latestByAuthor),
    hiddenRefs(),
  ])
  const pubkeys = [...consent].filter(([pk, e]) => directoryConsent(e) && !hidden.has(pk)).map(([pk]) => pk)
  const profiles = await loadProfiles(pubkeys)
  return pubkeys
    .map(pk => ({ pk, profile: profiles.get(pk) }))
    .sort((a, b) => profileName(a.profile, "~").localeCompare(profileName(b.profile, "~")))
}

function personCard({ pk, profile }) {
  const name = profileName(profile, "A participant")
  return `
    <a class="card person" href="#/p/${nip19.npubEncode(pk)}">
      ${avatar(profile, 48, pk)}
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
    <div class="row-end"><a class="button" href="#/activity">What's new</a><a class="button" href="#/me">Your entry</a></div>
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
    if ((await hiddenRefs()).has(pk) && !isMe(pk)) throw Object.assign(new Error("hidden"), { status: 404 })
  } catch (e) {
    body.innerHTML = e.status === 403 ? failure(e) : '<p class="notice bad">This is not a participant with an active pass.</p>'
    return
  }
  const profile = (await loadProfiles([pk])).get(pk)
  const chat = chatLink(pk)
  const pay = walletPayLink(pk, 10, "")
  body.innerHTML = `
    <div class="card stack">
      <div class="person">${avatar(profile, 72, pk)}<span class="person-text"><span class="person-name big">${esc(profileName(profile, "A participant"))}</span>
      ${profile?.nip05 ? `<span class="small muted">${esc(profile.nip05)}</span>` : ""}</span></div>
      ${profile?.about ? `<p class="pre">${esc(profile.about)}</p>` : ""}
      ${profile?.website ? `<p><a href="${esc(profile.website)}" rel="noopener noreferrer" target="_blank">${esc(profile.website)}</a></p>` : ""}
      ${
        isMe(pk)
          ? '<a class="button" href="#/me">Edit your entry</a>'
          : `<div class="actions">${chat ? `<a class="primary button" href="${esc(chat)}">Message</a>` : ""}${pay ? `<a class="button" href="${esc(pay)}">Send tokens</a>` : ""}</div>
             <p class="small">${reportLink({ kind: "profile", ref: pk, pubkey: pk, excerpt: [profileName(profile, ""), profile?.about || ""].join(" — ") })}</p>`
      }
    </div>`
  bindReports(body)
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

// -- what's new ------------------------------------------------------------------------------

const APP_NAMES = { chat: "Chat", calendar: "Calendar", programme: "Programme", market: "Market", photos: "Photos", vote: "Polls", people: "Directory" }

function activityLink(event, item) {
  switch (item.app) {
    case "chat":
      if (!URLS.chat) return ""
      return event.kind === 1
        ? `${URLS.chat}/notes/${nip19.neventEncode({ id: event.id, author: event.pubkey })}`
        : `${URLS.chat}/notes/${nip19.naddrEncode({ kind: event.kind, pubkey: event.pubkey, identifier: event.tags.find(t => t[0] === "d")?.[1] || "" })}`
    case "photos":
      return URLS.photos ? `${URLS.photos}/#/p/${item.photoId || event.id}` : ""
    case "market":
      return URLS.market ? `${URLS.market}/#/l/${nip19.naddrEncode({ kind: LISTING_KIND, pubkey: event.pubkey, identifier: item.listing.d })}` : ""
    case "calendar":
      // a public event opens in the programme, where it can be answered and commented on
      if (event.kind === 31923 && URLS.programme)
        return `${URLS.programme}/#/s/${nip19.naddrEncode({ kind: 31923, pubkey: event.pubkey, identifier: event.tags.find(t => t[0] === "d")?.[1] || "" })}`
      return URLS.calendar ? `${URLS.calendar}/` : ""
    case "programme": {
      const [, pubkey, ...d] = (item.session || "").split(":")
      return URLS.programme && pubkey ? `${URLS.programme}/#/s/${nip19.naddrEncode({ kind: 31923, pubkey, identifier: d.join(":") })}` : ""
    }
    case "vote":
      return item.url
    case "people":
      return `#/p/${nip19.npubEncode(event.pubkey)}`
    default:
      return ""
  }
}

function dayLabel(ts) {
  const d = new Date(ts * 1000)
  const today = new Date()
  const yesterday = new Date(Date.now() - 86400000)
  if (d.toDateString() === today.toDateString()) return "Today"
  if (d.toDateString() === yesterday.toDateString()) return "Yesterday"
  return d.toLocaleDateString(undefined, { weekday: "long", day: "numeric", month: "long" })
}

function startText(start) {
  if (!start) return ""
  if (typeof start === "number") return new Date(start * 1000).toLocaleString(undefined, { weekday: "short", day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" })
  return new Date(`${start}T12:00:00`).toLocaleDateString(undefined, { weekday: "short", day: "numeric", month: "short" })
}

/** The last two weeks of readable events from active participants, newest first. */
async function recentActivity() {
  const active = await participants()
  const since = Math.floor(Date.now() / 1000) - ACTIVITY_DAYS * 86400
  const [events, deletions, hidden] = await Promise.all([
    query({ kinds: ACTIVITY_KINDS, since, limit: 500 }),
    query({ kinds: [5], since, limit: 500 }),
    hiddenRefs(),
  ])
  const byId = new Map(events.map(e => [e.id, e]))
  const gone = deletedIds(deletions, byId)
  const goneCoords = new Set(deletions.flatMap(d => d.tags.filter(t => t[0] === "a" && t[1].split(":")[1] === d.pubkey).map(t => t[1])))
  // addressable events (offers, articles, calendar events, directory consent): the newest only
  const newest = new Map()
  for (const e of events) {
    // hidden content, and comments on hidden content
    if (!(active.has(e.pubkey) || (POLLS_KEY && e.pubkey === POLLS_KEY)) || gone.has(e.id) || isHidden(hidden, e)) continue
    if (e.kind === 1111 && e.tags.some(t => t[0] === "E" && hidden.has(t[1]))) continue
    const d = e.kind >= 30000 && e.kind < 40000 ? e.tags.find(t => t[0] === "d")?.[1] : null
    const key = d === null ? e.id : `${e.kind}:${e.pubkey}:${d}`
    if (goneCoords.has(key)) continue
    const prev = newest.get(key)
    if (!prev || e.created_at > prev.created_at) newest.set(key, e)
  }
  return [...newest.values()]
    .map(event => ({ event, item: activityFromEvent(event, { pollsKey: POLLS_KEY }) }))
    .filter(x => x.item)
    .sort((a, b) => b.event.created_at - a.event.created_at)
    .slice(0, 150)
}

async function activityPage() {
  render(`${header("What's new", "#/")}<div id="body"></div>`)
  bindCommon(route)
  const body = document.getElementById("body")
  if (!session.currentPubkey()) {
    body.innerHTML = signInCard("What's new shows the last two weeks in the village's apps. Sign in to see it.")
    return bindCommon(route)
  }
  body.innerHTML = `
    <div class="chips" id="apps"><button class="chip on" data-app="">All</button>${Object.entries(APP_NAMES)
      .map(([k, n]) => `<button class="chip" data-app="${k}">${n}</button>`)
      .join("")}</div>
    <div id="feed" class="stack"><p class="muted">Loading…</p></div>`
  let entries, profiles
  try {
    entries = await recentActivity()
    profiles = await loadProfiles([...new Set(entries.map(x => x.event.pubkey))])
  } catch (e) {
    document.getElementById("feed").innerHTML = failure(e)
    return
  }
  const feed = document.getElementById("feed")
  const show = app => {
    const hits = entries.filter(x => !app || x.item.app === app)
    if (!hits.length) {
      feed.innerHTML = `<p class="muted">Nothing in the last ${ACTIVITY_DAYS} days.</p>`
      return
    }
    let day = ""
    feed.innerHTML = hits
      .map(({ event, item }) => {
        const label = dayLabel(event.created_at)
        const heading = label !== day ? `<h3 class="day">${esc(label)}</h3>` : ""
        day = label
        const name = profileName(profiles.get(event.pubkey), "A participant")
        const href = activityLink(event, item)
        const time = new Date(event.created_at * 1000).toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit" })
        const start = startText(item.start)
        const inner = `
          ${avatar(profiles.get(event.pubkey), 36, event.pubkey)}
          <span class="activity-text">
            <span><strong>${esc(name)}</strong> ${esc(item.text)}</span>
            ${item.detail ? `<span class="about">${esc(item.detail)}</span>` : ""}
            <span class="small muted">${esc(APP_NAMES[item.app])} · ${esc(time)}${start ? ` · ${esc(start)}` : ""}</span>
          </span>
          ${item.image ? `<img class="thumb small" src="${esc(item.image)}" alt="" loading="lazy" referrerpolicy="no-referrer" />` : ""}`
        return `${heading}${href ? `<a class="card activity" href="${esc(href)}">${inner}</a>` : `<div class="card activity">${inner}</div>`}`
      })
      .join("")
  }
  show("")
  for (const b of document.querySelectorAll("#apps .chip"))
    b.onclick = () => {
      for (const x of document.querySelectorAll("#apps .chip")) x.classList.toggle("on", x === b)
      show(b.dataset.app)
    }
}

function route() {
  const hash = location.hash || "#/"
  let m
  if ((m = hash.match(/^#\/p\/(npub1\w+)$/))) return personPage(m[1])
  if (hash === "#/me") return mePage()
  if (hash === "#/activity") return activityPage()
  if (hash === "#/signer") return signerPage("Your own signer", () => (location.hash = "#/"))
  if (hash === "#/self-custody") return signerPage("Your own signer", () => (location.hash = "#/"), true)
  return listPage()
}

boot(route)
