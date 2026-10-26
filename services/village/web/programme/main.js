// Open Source Village programme: what is on, as a list for the phone. The village programme and
// the room bookings (public NIP-52 events of Belfry's programme key, published by the onboarding
// service's sync-programme job) and participants' own public events (kind 31923). Each session
// links what participants do with it: RSVPs (kind 31925), comments (NIP-22), photos taken there
// (kind 20 with the session's `a` tag) and, for participants' sessions, tips to the host.
// Hash routes: #/ (the day to show first), #/d/<YYYY-MM-DD>, #/s/<naddr> (one session), #/mine,
// #/search?q=, #/new (announce a session), #/edit/<naddr>, #/c/<coordinate> (a link by coordinate,
// from the polls app), #/signer.
import "../shared/style.css"
import "./style.css"
import { nip19 } from "nostr-tools"
import * as session from "../shared/session.js"
import {
  PHOTO_KIND,
  RSVP_KIND,
  SESSION_KIND,
  currentSessions,
  deletedIds,
  photoFromEvent,
  profileName,
  rsvpTemplate,
  rsvpsFor,
  sessionCommentTemplate,
  sessionDeletionTemplate,
  sessionIcs,
  sessionMatches,
  sessionTemplate,
  sessionsIcs,
  upcomingFor,
  zonedTime,
} from "../shared/model.js"
import { isMe, loadProfiles, participants, publish, query, queryAuthors } from "../shared/nostr.js"
import { avatar, bindCommon, boot, esc, header, notice, render, signInCard, signerPage, walletPayLink } from "../shared/ui.js"
import { bindReports, hiddenRefs, reportLink } from "../shared/report.js"

const WHY = "The village programme, the rooms and the sessions participants announce. Sign in to see what is on."
const PROGRAMME_KEY = (import.meta.env.VITE_PROGRAMME_PUBKEY || "").trim()
const TZ = import.meta.env.VITE_EVENT_TZ || "Europe/Brussels"
const PHOTOS_URL = (import.meta.env.VITE_PHOTOS_URL || "").replace(/\/$/, "")
const PEOPLE_URL = (import.meta.env.VITE_PEOPLE_URL || "").replace(/\/$/, "")
const VOTE_URL = (import.meta.env.VITE_VOTE_URL || "").replace(/\/$/, "")
const TRACK_KEY = "belfry-programme-track"
const TIPS = [1, 5, 10]
// the programme in the village's lime, the rooms in the calendar app's tints (calendar patch 0004)
const PROGRAMME_COLOR = "#d6ff52"
const ROOM_COLORS = ["#f89b0e", "#5ec8e5", "#c792ea", "#ff7a8a", "#7ee0a1", "#f2d06b", "#8fa8ff"]
const COMMUNITY_COLOR = "#e8ece9"
const STATUS_LABEL = { accepted: "Going", tentative: "Maybe", declined: "Can't go" }

const dayKey = t => new Intl.DateTimeFormat("en-CA", { timeZone: TZ, year: "numeric", month: "2-digit", day: "2-digit" }).format(new Date(t * 1000))
const time = t => new Intl.DateTimeFormat(undefined, { timeZone: TZ, hour: "2-digit", minute: "2-digit" }).format(new Date(t * 1000))
const dayName = (key, style = "long") =>
  new Intl.DateTimeFormat(undefined, { timeZone: "UTC", weekday: style, day: "numeric", month: style === "long" ? "long" : "short" }).format(new Date(`${key}T12:00:00Z`))
const when = s => `${dayName(dayKey(s.start))}, ${time(s.start)}${s.end > s.start ? `–${time(s.end)}` : ""}`
const tokens = n => `${n} token${n === 1 ? "" : "s"}`
const now = () => Math.floor(Date.now() / 1000)
const hm = t => new Intl.DateTimeFormat("en-GB", { timeZone: TZ, hour: "2-digit", minute: "2-digit", hourCycle: "h23" }).format(new Date(t * 1000))
const naddr = s => nip19.naddrEncode({ kind: SESSION_KIND, pubkey: s.pubkey, identifier: s.d })

function failure(e) {
  if (e.status === 403) return '<p class="notice bad">The programme is for participants with an active pass.</p>'
  return `<p class="notice bad">Could not load the programme: ${esc(e.message)}</p>`
}

async function requireSignIn(body) {
  if (session.currentPubkey()) return true
  forgetProgramme()
  body.innerHTML = signInCard(WHY)
  bindCommon(route)
  return false
}

const CACHE_KEY = "belfry-programme-cache"

/** The programme as this device last loaded it, for the signed-in participant (shown at once, then
 * refreshed from the relay; also what is left when the venue's network is down). */
function cachedProgramme() {
  try {
    const c = JSON.parse(localStorage.getItem(CACHE_KEY) || "null")
    if (!c || c.pubkey !== session.currentPubkey()) return null
    return { ...c, active: new Set(c.active), hidden: new Set(c.hidden), cached: true }
  } catch {
    return null
  }
}

function keepProgramme(data) {
  try {
    if (!data.sessions.length) return
    const { active, hidden, sessions, rsvps, tracks } = data
    localStorage.setItem(CACHE_KEY, JSON.stringify({ pubkey: session.currentPubkey(), active: [...active], hidden: [...hidden], sessions, rsvps, tracks }))
  } catch {
    // storage full or blocked: the next visit loads from the relay
  }
}

function forgetProgramme() {
  try {
    localStorage.removeItem(CACHE_KEY)
  } catch {
    // nothing kept
  }
}

let loaded = null
/** Sessions of the programme key and of active participants, the participants' RSVPs, once per page
 * load (an RSVP or a comment reloads what it changes). */
function loadProgramme() {
  loaded ||= (async () => {
    const active = await participants()
    const authors = [...new Set([PROGRAMME_KEY, ...active].filter(Boolean))]
    const [events, deletions, rsvps, hidden] = await Promise.all([
      queryAuthors({ kinds: [SESSION_KIND] }, authors, { all: true }),
      queryAuthors({ kinds: [5], "#k": [String(SESSION_KIND)] }, authors, { all: true }),
      queryAuthors({ kinds: [RSVP_KIND] }, [...active], { all: true }),
      hiddenRefs(),
    ])
    const sessions = currentSessions(events, deletions, PROGRAMME_KEY).filter(
      s => s.official || !(hidden.has(s.id) || hidden.has(s.coordinate)) || isMe(s.pubkey),
    )
    // one colour per track: the programme, then the rooms by name, then participants' sessions
    const rooms = [...new Map(sessions.filter(s => s.track.id.startsWith("room-")).map(s => [s.track.id, s.track])).values()].sort((a, b) =>
      a.title.localeCompare(b.title),
    )
    const tracks = []
    if (sessions.some(s => s.track.id === "programme")) tracks.push({ id: "programme", title: "Village programme", color: PROGRAMME_COLOR })
    rooms.forEach((r, i) => tracks.push({ ...r, color: ROOM_COLORS[i % ROOM_COLORS.length] }))
    if (sessions.some(s => s.track.id === "community")) tracks.push({ id: "community", title: "From participants", color: COMMUNITY_COLOR })
    const data = { active, sessions, rsvps, tracks, hidden }
    keepProgramme(data)
    return data
  })()
  loaded.catch(() => (loaded = null))
  return loaded
}

const trackOf = (data, s) => data.tracks.find(t => t.id === s.track.id) || { title: s.track.title, color: COMMUNITY_COLOR }

function chosenTrack() {
  try {
    return localStorage.getItem(TRACK_KEY) || ""
  } catch {
    return ""
  }
}

function row(data, s, t) {
  const answers = rsvpsFor(data.rsvps, s.coordinate)
  const going = [...answers.values()].filter(v => v === "accepted").length
  const mine = answers.get(session.currentPubkey())
  const live = s.start <= t && t < s.end
  const track = trackOf(data, s)
  return `
    <a class="card session ${s.end <= t ? "past" : ""}" href="#/s/${naddr(s)}" style="--track:${esc(track.color)}">
      <span class="session-time">${esc(time(s.start))}${s.end > s.start ? `<br><span class="muted">${esc(time(s.end))}</span>` : ""}</span>
      <span class="session-text">
        <span class="session-title">${live ? '<span class="badge open">Now</span> ' : ""}${esc(s.title)}</span>
        <span class="small muted">${esc(track.title)}${s.location && !s.official ? ` · ${esc(s.location.split(",")[0])}` : ""}</span>
        ${going || mine ? `<span class="small">${going ? `${going} going` : ""}${mine ? `${going ? " · " : ""}<strong>you: ${esc(STATUS_LABEL[mine].toLowerCase())}</strong>` : ""}</span>` : ""}
      </span>
    </a>`
}

/** The day to open on: today when it has sessions, else the next day with sessions, else the last one. */
function defaultDay(days) {
  const today = dayKey(now())
  return days.find(d => d >= today) || days.at(-1)
}

/** The tab bar at the bottom of the screen, within thumb reach. */
function tabs(current) {
  const items = [
    ["days", "#/", "Programme", "M4 5h16M4 10h16M4 15h10"],
    ["search", "#/search", "Search", "M10.5 17a6.5 6.5 0 1 1 0-13 6.5 6.5 0 0 1 0 13Zm4.6-1.9L20 20"],
    ["mine", "#/mine", "Yours", "M12 20s-7-4.4-7-9.5A3.9 3.9 0 0 1 12 8a3.9 3.9 0 0 1 7 2.5C19 15.6 12 20 12 20Z"],
    ["new", "#/new", "Announce", "M12 5v14M5 12h14"],
  ]
  return `<nav class="tabs" aria-label="Programme">${items
    .map(
      ([id, href, label, d]) =>
        `<a href="${href}" ${id === current ? 'class="on" aria-current="page"' : ""}><svg viewBox="0 0 24 24" aria-hidden="true"><path d="${d}"/></svg><span>${label}</span></a>`,
    )
    .join("")}</nav>`
}

/** "Starting soon": the participant's sessions on now or within half an hour. */
function soonNote(data, t) {
  const me = session.currentPubkey()
  const soon = upcomingFor(data.sessions, data.rsvps, me, t)
  if (!soon.length) return ""
  return `<div class="soon stack">${soon
    .map(
      s =>
        `<a class="card soon-item" href="#/s/${naddr(s)}"><span class="label">${s.start <= t ? "On now" : `Starts in ${Math.max(1, Math.round((s.start - t) / 60))} min`}</span><strong>${esc(
          s.title,
        )}</strong>${s.location ? `<span class="small muted">${esc(s.location.split(",")[0])}</span>` : ""}</a>`,
    )
    .join("")}</div>`
}

/** Draws a page from the programme kept on this device at once, then again from the relay (unless
 * another page replaced it meanwhile). `draw(data)` may run twice. */
async function withProgramme(body, draw) {
  const cached = cachedProgramme()
  if (cached) draw(cached)
  try {
    const data = await loadProgramme()
    if (!body.isConnected) return
    // an empty answer is a relay that did not answer in time, not an empty programme
    if (cached && !data.sessions.length) return notice("Could not refresh the programme; this is what your phone kept.", "bad")
    draw(data)
  } catch (e) {
    if (!body.isConnected) return
    if (cached) notice("Could not refresh the programme; this is what your phone kept.", "bad")
    else body.innerHTML = failure(e)
  }
}

let dayTimer = null

async function dayPage(day = "") {
  render(`${header("Programme")}<div id="body"><p class="muted">Loading…</p></div>${tabs("days")}`)
  bindCommon(route)
  const body = document.getElementById("body")
  if (!(await requireSignIn(body))) return
  await withProgramme(body, data => drawDay(body, data, day))
}

function drawDay(body, data, day) {
  const track = data.tracks.some(t => t.id === chosenTrack()) ? chosenTrack() : ""
  const shown = data.sessions.filter(s => !track || s.track.id === track)
  const days = [...new Set(shown.map(s => dayKey(s.start)))].sort()
  const t = now()
  if (!days.length) {
    body.innerHTML = `${filters(data, track)}<p class="muted">Nothing in the programme yet. <a href="#/new">Announce a session</a>.</p>`
    bindFilters(day)
    return
  }
  if (!days.includes(day)) day = defaultDay(days)
  const today = dayKey(t) === day
  const list = shown.filter(s => dayKey(s.start) === day)
  const sections = today
    ? [
        ["On now", list.filter(s => s.start <= t && t < s.end)],
        ["Later today", list.filter(s => s.start > t)],
        ["Earlier today", list.filter(s => s.end <= t && !(s.start <= t && t < s.end))],
      ]
    : [["", list]]
  const at = days.indexOf(day)
  body.innerHTML = `
    ${soonNote(data, t)}
    <nav class="days" aria-label="Days">${days
      .map(d => `<a class="chip ${d === day ? "on" : ""}" href="#/d/${d}" ${d === day ? 'aria-current="date"' : ""}>${esc(d === dayKey(t) ? "Today" : dayName(d, "short"))}</a>`)
      .join("")}</nav>
    ${filters(data, track)}
    <div id="day">
      <h2>${esc(dayName(day))}</h2>
      ${sections
        .filter(([, items]) => items.length)
        .map(([title, items]) => `${title ? `<h3 class="day">${esc(title)}</h3>` : ""}<div class="stack">${items.map(s => row(data, s, t)).join("")}</div>`)
        .join("")}
      <p class="small muted center">${at > 0 || at < days.length - 1 ? "Swipe for the other days." : ""}</p>
    </div>`
  bindFilters(day)
  document.querySelector('.tabs a[href="#/new"]')?.setAttribute("href", `#/new?day=${day}`)
  document.querySelector(".days .on")?.scrollIntoView({ inline: "center", block: "nearest" })
  swipe(document.getElementById("day"), dir => {
    const next = days[at + dir]
    if (next) location.hash = `#/d/${next}`
  })
  // today: "On now" and "Starting soon" move with the clock
  clearInterval(dayTimer)
  if (today) {
    dayTimer = setInterval(() => {
      if (!body.isConnected) return clearInterval(dayTimer)
      if (document.visibilityState === "visible") drawDay(body, data, day)
    }, 60000)
  }
}

/** Horizontal swipes on `el`: `onSwipe(+1)` for the next (swipe left), -1 for the previous. */
function swipe(el, onSwipe) {
  if (!el) return
  let x0 = null
  let y0 = 0
  el.addEventListener(
    "touchstart",
    e => {
      x0 = e.touches[0].clientX
      y0 = e.touches[0].clientY
    },
    { passive: true },
  )
  el.addEventListener(
    "touchend",
    e => {
      if (x0 === null) return
      const dx = e.changedTouches[0].clientX - x0
      const dy = e.changedTouches[0].clientY - y0
      x0 = null
      if (Math.abs(dx) > 70 && Math.abs(dx) > 2 * Math.abs(dy)) onSwipe(dx < 0 ? 1 : -1)
    },
    { passive: true },
  )
}

function filters(data, track) {
  if (data.tracks.length < 2) return ""
  return `<div class="chips tracks">${[{ id: "", title: "All", color: "" }, ...data.tracks]
    .map(
      t =>
        `<button class="chip ${t.id === track ? "on" : ""}" data-track="${esc(t.id)}">${t.color ? `<span class="dot" style="background:${esc(t.color)}"></span>` : ""}${esc(t.title)}</button>`,
    )
    .join("")}</div>`
}

function bindFilters(day) {
  for (const b of document.querySelectorAll("[data-track]"))
    b.onclick = () => {
      try {
        localStorage.setItem(TRACK_KEY, b.dataset.track)
      } catch {
        // private mode: the filter lasts until the page is left
      }
      const body = document.getElementById("body")
      const data = cachedProgramme()
      if (body && data) drawDay(body, data, day)
      else dayPage(day)
    }
}

/** Sessions by day, with a heading per day. */
function byDay(data, list, t) {
  let day = ""
  return list
    .map(s => {
      const d = dayKey(s.start)
      const heading = d !== day ? `<h3 class="day">${esc(d === dayKey(t) ? "Today" : dayName(d))}</h3>` : ""
      day = d
      return heading + row(data, s, t)
    })
    .join("")
}

async function searchPage(q = "") {
  render(`${header("Search")}<div id="body"><p class="muted">Loading…</p></div>${tabs("search")}`)
  bindCommon(route)
  const body = document.getElementById("body")
  if (!(await requireSignIn(body))) return
  let data = null
  body.innerHTML = `
    <form id="search" role="search"><input id="q" type="search" enterkeyhint="search" autocomplete="off" placeholder="Talk, room, topic…" aria-label="Search the programme" value="${esc(q)}" /></form>
    <div id="results"></div>`
  const input = document.getElementById("q")
  const results = document.getElementById("results")
  const show = () => {
    if (!data) return
    const query = input.value.trim()
    const t = now()
    // upcoming first, then what has passed
    const hits = data.sessions.filter(s => sessionMatches(s, query))
    const ordered = [...hits.filter(s => s.end > t), ...hits.filter(s => s.end <= t)]
    results.innerHTML = !query
      ? '<p class="muted small">Search titles, descriptions, rooms and hosts\' places.</p>'
      : ordered.length
        ? `<p class="small muted">${ordered.length} session${ordered.length === 1 ? "" : "s"}</p>${byDay(data, ordered, t)}`
        : '<p class="muted">Nothing found.</p>'
    history.replaceState(null, "", query ? `#/search?q=${encodeURIComponent(query)}` : "#/search")
  }
  input.oninput = show
  document.getElementById("search").onsubmit = e => {
    e.preventDefault()
    input.blur() // closes the phone's keyboard
  }
  if (!q) input.focus()
  // the results follow the programme as it arrives; the field keeps what is typed
  await withProgramme(body, fresh => {
    data = fresh
    show()
  })
}

async function minePage() {
  render(`${header("Your sessions")}<div id="body"><p class="muted">Loading…</p></div>${tabs("mine")}`)
  bindCommon(route)
  const body = document.getElementById("body")
  if (!(await requireSignIn(body))) return
  await withProgramme(body, data => drawMine(body, data))
}

function drawMine(body, data) {
  const t = now()
  const me = session.currentPubkey()
  const mine = data.sessions.filter(s => {
    const a = rsvpsFor(data.rsvps, s.coordinate).get(me)
    return s.end > t && (a === "accepted" || a === "tentative" || s.pubkey === me)
  })
  if (!mine.length) {
    body.innerHTML =
      '<p class="muted">Sessions you are going to (or might go to) and your own show up here. <a href="#/">See the programme</a> or <a href="#/new">announce a session</a>.</p>'
    return
  }
  const ics = `data:text/calendar;charset=utf-8,${encodeURIComponent(sessionsIcs(mine, s => `${location.origin}/#/s/${naddr(s)}`))}`
  body.innerHTML = `
    ${soonNote(data, t)}
    ${byDay(data, mine, t)}
    <div class="stack top-gap">
      <a class="button" href="${ics}" download="open-source-village.ics">Add all ${mine.length} to my calendar</a>
      <p class="small muted">One file for your phone's calendar, with a reminder 15 minutes before each session. Add it again after you change your plans.</p>
    </div>`
}

async function sessionPage(code) {
  render(`${header("Session", "#/")}<div id="body"><p class="muted">Loading…</p></div>${tabs("")}`)
  bindCommon(route)
  const body = document.getElementById("body")
  if (!(await requireSignIn(body))) return
  let s, data, answers, comments, photos, zaps, profiles, polls
  try {
    const ref = nip19.decode(code)
    if (ref.type !== "naddr" || ref.data.kind !== SESSION_KIND) throw new Error("not a session link")
    data = await loadProgramme()
    const coordinate = `${SESSION_KIND}:${ref.data.pubkey}:${ref.data.identifier}`
    s = data.sessions.find(x => x.coordinate === coordinate)
    if (!s) throw Object.assign(new Error("This session is not in the programme (any more)."), { status: 404 })
    const [rsvpEvents, commentEvents, photoEvents, zapEvents, pollList] = await Promise.all([
      query({ kinds: [RSVP_KIND], "#a": [coordinate] }),
      query({ kinds: [1111], "#A": [coordinate] }),
      query({ kinds: [PHOTO_KIND], "#a": [coordinate] }),
      s.official ? Promise.resolve([]) : query({ kinds: [9321], "#a": [coordinate] }),
      sessionPolls(coordinate),
    ])
    polls = pollList
    answers = rsvpsFor(
      rsvpEvents.filter(e => data.active.has(e.pubkey)),
      coordinate,
    )
    comments = commentEvents
      .filter(c => data.active.has(c.pubkey) && c.content.trim() && !(data.hidden.has(c.id) && !isMe(c.pubkey)))
      .sort((a, b) => a.created_at - b.created_at)
    const photoById = new Map(photoEvents.filter(e => data.active.has(e.pubkey)).map(e => [e.id, e]))
    const posters = [...new Set([...photoById.values()].map(e => e.pubkey))]
    const gone = posters.length ? deletedIds(await queryAuthors({ kinds: [5], "#k": [String(PHOTO_KIND)] }, posters, { all: true }), photoById) : new Set()
    photos = [...photoById.values()]
      .filter(e => !gone.has(e.id) && !data.hidden.has(e.id))
      .map(photoFromEvent)
      .filter(Boolean)
      .sort((a, b) => b.createdAt - a.createdAt)
    zaps = zapEvents.filter(z => z.tags.some(t => t[0] === "p" && t[1] === s.pubkey))
    const going = [...answers].filter(([, v]) => v !== "declined").map(([pk]) => pk)
    profiles = await loadProfiles([s.pubkey, ...going, ...comments.map(c => c.pubkey)])
  } catch (e) {
    body.innerHTML = e.status === 403 ? failure(e) : `<p class="notice bad">${esc(e.message)}</p>`
    return
  }
  if (!body.isConnected) return // another page replaced this one meanwhile
  const me = session.currentPubkey()
  const mine = answers.get(me)
  const track = trackOf(data, s)
  const going = [...answers].filter(([, v]) => v === "accepted").map(([pk]) => pk)
  const maybe = [...answers].filter(([, v]) => v === "tentative").map(([pk]) => pk)
  const host = profiles.get(s.pubkey)
  const link = `${location.origin}/#/s/${naddr(s)}`
  const ics = `data:text/calendar;charset=utf-8,${encodeURIComponent(sessionIcs(s, link))}`
  const memo = `Session: ${s.title}`
  const tipLinks = s.official || isMe(s.pubkey) ? [] : TIPS.map(n => [n, walletPayLink(s.pubkey, n, memo, s.coordinate)]).filter(([, u]) => u)
  const tipped = zaps.length ? zaps.length : 0
  const people = list =>
    list
      .map(pk => {
        const p = profiles.get(pk)
        const inner = `${avatar(p, 28, pk)}<span>${esc(profileName(p, "A participant"))}</span>`
        return PEOPLE_URL ? `<a class="who" href="${esc(`${PEOPLE_URL}/#/p/${nip19.npubEncode(pk)}`)}">${inner}</a>` : `<span class="who">${inner}</span>`
      })
      .join("")
  body.innerHTML = `
    <article class="card stack session-page" style="--track:${esc(track.color)}">
      <span class="small"><span class="dot" style="background:${esc(track.color)}"></span>${esc(track.title)}</span>
      <h2>${esc(s.title)}</h2>
      <p class="when"><strong>${esc(when(s))}</strong>${s.location ? `<br><span class="muted">${esc(s.location)}</span>` : ""}</p>
      ${
        s.official
          ? ""
          : `<div class="person">${avatar(host, 32, s.pubkey)}<span class="person-text"><span class="small muted">Announced by</span><span class="person-name">${esc(
              profileName(host, "A participant"),
            )}${isMe(s.pubkey) ? ' <span class="badge">you</span>' : ""}</span></span></div>`
      }
      ${s.description ? `<p class="pre">${esc(s.description)}</p>` : ""}
      ${s.links.length ? `<p class="small">${s.links.map(u => `<a href="${esc(u)}" target="_blank" rel="noopener">${esc(u.replace(/^https?:\/\//, ""))}</a>`).join("<br>")}</p>` : ""}
      <div class="rsvp" role="group" aria-label="Are you going?">${Object.entries(STATUS_LABEL)
        .map(([k, l]) => `<button data-rsvp="${k}" class="${mine === k ? "primary" : ""}" aria-pressed="${mine === k}">${esc(l)}</button>`)
        .join("")}</div>
      <p class="small muted">${going.length} going${maybe.length ? `, ${maybe.length} maybe` : ""}. Other participants see your answer.</p>
      ${going.length ? `<div class="people">${people(going)}</div>` : ""}
      ${maybe.length ? `<span class="label">Maybe</span><div class="people">${people(maybe)}</div>` : ""}
      <div class="actions tight">
        <a class="button" href="${ics}" download="${esc(s.title.slice(0, 60).replace(/[^\p{L}\p{N} _-]/gu, ""))}.ics">Add to my calendar</a>
        <button id="share">Share</button>
        ${s.official ? "" : isMe(s.pubkey) ? "" : reportLink({ kind: "session", ref: s.coordinate, pubkey: s.pubkey, excerpt: s.title })}
      </div>
      ${
        !s.official && isMe(s.pubkey)
          ? `<div class="actions tight"><a class="button" href="#/edit/${naddr(s)}">Edit</a><button id="cancel" class="danger">Cancel the session</button></div>`
          : ""
      }
      ${
        tipLinks.length
          ? `<div class="actions tight"><span class="label">Thank the host</span>${tipLinks.map(([n, u]) => `<a class="button" href="${esc(u)}">${tokens(n)}</a>`).join("")}</div>
             ${tipped ? `<p class="small muted">${tipped} thank-you${tipped === 1 ? "" : "s"} in tokens so far.</p>` : ""}`
          : ""
      }
      ${
        VOTE_URL
          ? `<h3>Polls</h3>
             ${
               polls === null
                 ? '<p class="muted small">The polls could not be loaded.</p>'
                 : polls.length
                   ? `<div class="stack">${polls
                       .map(
                         p =>
                           `<a class="card poll" href="${esc(`${VOTE_URL}/#/poll/${p.id}`)}"><span class="session-title">${esc(p.title)}</span><span class="small muted"><span class="badge ${p.open ? "open" : ""}">${
                             p.open ? "Open" : "Results"
                           }</span> ${p.votes} vote${p.votes === 1 ? "" : "s"}</span></a>`,
                       )
                       .join("")}</div>`
                   : '<p class="muted small">No polls about this session. Ask the room something, anonymously.</p>'
             }
             <a class="button" href="${esc(`${VOTE_URL}/#/new?session=${encodeURIComponent(s.coordinate)}&title=${encodeURIComponent(s.title)}`)}">Start a poll</a>`
          : ""
      }
      <h3>Photos</h3>
      ${
        photos.length
          ? `<div class="photo-grid">${photos
              .map(p => `<a class="photo-tile" href="${esc(`${PHOTOS_URL}/#/p/${p.id}`)}"><img src="${esc(p.images[0].url)}" alt="${esc(p.images[0].alt || p.title)}" loading="lazy" referrerpolicy="no-referrer" /></a>`)
              .join("")}</div>`
          : '<p class="muted small">No photos from this session yet.</p>'
      }
      ${PHOTOS_URL ? `<a class="button" href="${esc(`${PHOTOS_URL}/#/new?session=${encodeURIComponent(s.coordinate)}`)}">Share a photo from this session</a>` : ""}
      <h3>Comments</h3>
      <div class="stack">${
        comments.length
          ? comments
              .map(
                c => `<div class="comment">${avatar(profiles.get(c.pubkey), 28, c.pubkey)}<div><span class="small">${esc(profileName(profiles.get(c.pubkey), "A participant"))} <span class="muted">${esc(
                  `${dayName(dayKey(c.created_at), "short")} ${time(c.created_at)}`,
                )}</span>${isMe(c.pubkey) ? "" : ` ${reportLink({ kind: "comment", ref: c.id, pubkey: c.pubkey, excerpt: c.content })}`}</span><p class="pre">${esc(c.content)}</p></div></div>`,
              )
              .join("")
          : '<p class="muted small">No comments yet. Questions, notes, slides: add them here.</p>'
      }</div>
      <form id="comment" class="stack">
        <label>Your comment<textarea id="text" rows="2" maxlength="2000" required></textarea></label>
        <button type="submit">Comment</button>
      </form>
    </article>`
  bindReports(body)
  for (const b of body.querySelectorAll("[data-rsvp]"))
    b.onclick = async () => {
      for (const x of body.querySelectorAll("[data-rsvp]")) x.disabled = true
      try {
        const event = await publish(rsvpTemplate(s, b.dataset.rsvp, me))
        data.rsvps.push(event)
        keepProgramme(data)
        sessionPage(code)
      } catch (err) {
        for (const x of body.querySelectorAll("[data-rsvp]")) x.disabled = false
        notice(`Could not save your answer: ${err.message}`, "bad")
      }
    }
  const cancel = document.getElementById("cancel")
  if (cancel)
    cancel.onclick = async () => {
      if (!confirm(`Cancel "${s.title}"? It disappears from the programme.`)) return
      cancel.disabled = true
      try {
        await publish(sessionDeletionTemplate(s))
        loaded = null
        location.hash = `#/d/${dayKey(s.start)}`
        notice("Session cancelled.", "good")
      } catch (err) {
        cancel.disabled = false
        notice(`Could not cancel: ${err.message}`, "bad")
      }
    }
  document.getElementById("share").onclick = async () => {
    try {
      if (navigator.share) await navigator.share({ title: s.title, text: when(s), url: link })
      else {
        await navigator.clipboard.writeText(link)
        notice("Link copied. Participants open it in the programme.", "good")
      }
    } catch {
      // the share sheet was closed
    }
  }
  document.getElementById("comment").onsubmit = async ev => {
    ev.preventDefault()
    const btn = ev.target.querySelector("button")
    const text = document.getElementById("text").value.trim()
    if (!text) return
    btn.disabled = true
    try {
      await publish(sessionCommentTemplate(s, text))
      sessionPage(code)
    } catch (err) {
      btn.disabled = false
      notice(`Could not comment: ${err.message}`, "bad")
    }
  }
}

/** Announce a session (a public kind 31923 in the participant's name), or edit one's own. Times are
 * the village's (TZ), whatever the phone's zone. */
async function editPage(code = "", day = "") {
  render(`${header(code ? "Edit your session" : "Announce a session", code ? `#/s/${code}` : "#/")}<div id="body"><p class="muted">Loading…</p></div>${tabs(code ? "" : "new")}`)
  bindCommon(route)
  const body = document.getElementById("body")
  if (!(await requireSignIn(body))) return
  let data, s = null
  try {
    data = await loadProgramme()
    if (code) {
      const ref = nip19.decode(code)
      s = data.sessions.find(x => x.coordinate === `${SESSION_KIND}:${ref.data.pubkey}:${ref.data.identifier}`)
      if (!s || !isMe(s.pubkey) || s.official) throw new Error("You can only edit sessions you announced.")
    }
  } catch (e) {
    body.innerHTML = e.status === 403 ? failure(e) : `<p class="notice bad">${esc(e.message)}</p>`
    return
  }
  if (!body.isConnected) return // another page replaced this one meanwhile
  const days = [...new Set(data.sessions.filter(x => x.official).map(x => dayKey(x.start)))].sort()
  const date = s ? dayKey(s.start) : /^\d{4}-\d{2}-\d{2}$/.test(day) ? day : days.find(d => d >= dayKey(now())) || dayKey(now())
  const places = [...new Set(data.sessions.filter(x => x.track.id.startsWith("room-") && x.location).map(x => x.location))].sort()
  body.innerHTML = `
    <form class="card stack" id="form">
      <p class="small muted">Your session appears in the programme under "From participants". Everyone with a pass sees it, with your name.</p>
      <label>Title<input id="title" required maxlength="200" value="${esc(s?.title || "")}" placeholder="Rust meetup, a walk to the canal, a workshop…" /></label>
      <label>Day<input id="date" type="date" required value="${esc(date)}" /></label>
      <div class="two">
        <label>From<input id="start" type="time" required value="${esc(s ? hm(s.start) : "")}" /></label>
        <label>Until<input id="end" type="time" required value="${esc(s ? hm(s.end) : "")}" /></label>
      </div>
      <p class="small muted">Times in the village's time (${esc(TZ.replace(/_/g, " "))}).</p>
      <label>Where<input id="location" maxlength="200" list="places" value="${esc(s?.location || "")}" placeholder="A room, the garden, the café…" /></label>
      <datalist id="places">${places.map(p => `<option value="${esc(p)}"></option>`).join("")}</datalist>
      <label>What it is about<textarea id="description" rows="5" maxlength="4000">${esc(s?.description || "")}</textarea></label>
      <label>A link (optional)<input id="link" type="url" maxlength="500" value="${esc(s?.links[0] || "")}" placeholder="https://" /></label>
      <button type="submit" class="primary">${s ? "Save" : "Announce"}</button>
    </form>`
  document.getElementById("form").onsubmit = async ev => {
    ev.preventDefault()
    const v = id => document.getElementById(id).value
    const btn = ev.target.querySelector("button[type=submit]")
    const date = v("date")
    const start = zonedTime(date, v("start"), TZ)
    let end = zonedTime(date, v("end"), TZ)
    if (end <= start) end = zonedTime(dayKey(start + 86400), v("end"), TZ) // past midnight
    btn.disabled = true
    try {
      const template = sessionTemplate({ d: s?.d, title: v("title"), start, end, tzid: TZ, location: v("location"), description: v("description"), link: v("link"), keep: s?.tags || [] })
      const event = await publish(template)
      loaded = null
      location.hash = `#/s/${nip19.naddrEncode({ kind: SESSION_KIND, pubkey: event.pubkey, identifier: template.tags[0][1] })}`
      notice(s ? "Saved." : "Your session is in the programme.", "good")
    } catch (err) {
      btn.disabled = false
      notice(`Could not save: ${err.message}`, "bad")
    }
  }
}

/** The polls about a session (the polls app's public list); null when it cannot be reached. */
async function sessionPolls(coordinate) {
  if (!VOTE_URL) return []
  try {
    const res = await fetch(`${VOTE_URL}/api/polls?session=${encodeURIComponent(coordinate)}`)
    if (!res.ok) return null
    return (await res.json()).polls
  } catch {
    return null
  }
}

function route() {
  const hash = location.hash || "#/"
  let m
  if ((m = hash.match(/^#\/s\/(naddr1\w+)$/))) return sessionPage(m[1])
  if ((m = hash.match(/^#\/d\/(\d{4}-\d{2}-\d{2})$/))) return dayPage(m[1])
  if (hash === "#/mine") return minePage()
  if ((m = hash.match(/^#\/c\/(.+)$/))) {
    // a session by its coordinate (`31923:<pubkey>:<d>`, URL-encoded): on to its page
    try {
      const [kind, pubkey, ...d] = decodeURIComponent(m[1]).split(":")
      if (Number(kind) === SESSION_KIND && /^[0-9a-f]{64}$/.test(pubkey) && d.length) {
        location.replace(`#/s/${nip19.naddrEncode({ kind: SESSION_KIND, pubkey, identifier: d.join(":") })}`)
        return
      }
    } catch {
      // not a coordinate: the programme
    }
  }
  if ((m = hash.match(/^#\/search(?:\?q=(.*))?$/))) {
    let q = ""
    try {
      q = decodeURIComponent(m[1] || "")
    } catch {
      // a broken link: start empty
    }
    return searchPage(q)
  }
  if ((m = hash.match(/^#\/new(?:\?day=(\d{4}-\d{2}-\d{2}))?$/))) return editPage("", m[1])
  if ((m = hash.match(/^#\/edit\/(naddr1\w+)$/))) return editPage(m[1])
  if (hash === "#/signer") return signerPage("Your own signer", () => (location.hash = "#/"))
  if (hash === "#/self-custody") return signerPage("Your own signer", () => (location.hash = "#/"), true)
  return dayPage()
}

boot(route)

// the page opens without a network too (public/sw.js); the programme then comes from the phone's copy
if ("serviceWorker" in navigator) navigator.serviceWorker.register("/sw.js").catch(() => {})
