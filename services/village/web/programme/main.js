// Open Source Village programme: what is on, as a list for the phone. The village programme and
// the room bookings (public NIP-52 events of Belfry's programme key, published by the onboarding
// service's sync-programme job) and participants' own public events (kind 31923). Each session
// links what participants do with it: RSVPs (kind 31925), comments (NIP-22), photos taken there
// (kind 20 with the session's `a` tag) and, for participants' sessions, tips to the host.
// Hash routes: #/ (the day to show first), #/d/<YYYY-MM-DD>, #/s/<naddr> (one session), #/mine,
// #/signer.
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
  sessionIcs,
} from "../shared/model.js"
import { isMe, loadProfiles, participants, publish, query, queryAuthors } from "../shared/nostr.js"
import { avatar, bindCommon, boot, esc, header, notice, render, signInCard, signerPage, walletPayLink } from "../shared/ui.js"
import { bindReports, hiddenRefs, reportLink } from "../shared/report.js"

const WHY = "The village programme, the rooms and the sessions participants announce. Sign in to see what is on."
const PROGRAMME_KEY = (import.meta.env.VITE_PROGRAMME_PUBKEY || "").trim()
const TZ = import.meta.env.VITE_EVENT_TZ || "Europe/Brussels"
const PHOTOS_URL = (import.meta.env.VITE_PHOTOS_URL || "").replace(/\/$/, "")
const PEOPLE_URL = (import.meta.env.VITE_PEOPLE_URL || "").replace(/\/$/, "")
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
const naddr = s => nip19.naddrEncode({ kind: SESSION_KIND, pubkey: s.pubkey, identifier: s.d })

function failure(e) {
  if (e.status === 403) return '<p class="notice bad">The programme is for participants with an active pass.</p>'
  return `<p class="notice bad">Could not load the programme: ${esc(e.message)}</p>`
}

async function requireSignIn(body) {
  if (session.currentPubkey()) return true
  body.innerHTML = signInCard(WHY)
  bindCommon(route)
  return false
}

let loaded = null
/** Sessions of the programme key and of active participants, the participants' RSVPs, once per page
 * load (an RSVP or a comment reloads what it changes). */
function loadProgramme() {
  loaded ||= (async () => {
    const active = await participants()
    const authors = [...new Set([PROGRAMME_KEY, ...active].filter(Boolean))]
    const [events, deletions, rsvps, hidden] = await Promise.all([
      queryAuthors({ kinds: [SESSION_KIND], limit: 5000 }, authors),
      queryAuthors({ kinds: [5], limit: 5000 }, authors),
      queryAuthors({ kinds: [RSVP_KIND], limit: 5000 }, [...active]),
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
    return { active, sessions, rsvps, tracks, hidden }
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

async function dayPage(day = "") {
  render(`${header("Programme")}<div id="body"><p class="muted">Loading…</p></div>`)
  bindCommon(route)
  const body = document.getElementById("body")
  if (!(await requireSignIn(body))) return
  let data
  try {
    data = await loadProgramme()
  } catch (e) {
    body.innerHTML = failure(e)
    return
  }
  const track = data.tracks.some(t => t.id === chosenTrack()) ? chosenTrack() : ""
  const shown = data.sessions.filter(s => !track || s.track.id === track)
  const days = [...new Set(shown.map(s => dayKey(s.start)))].sort()
  if (!days.length) {
    body.innerHTML = `${filters(data, track)}<p class="muted">Nothing in the programme yet.</p>`
    bindFilters(day)
    return
  }
  if (!days.includes(day)) day = defaultDay(days)
  const t = now()
  const today = dayKey(t) === day
  const list = shown.filter(s => dayKey(s.start) === day)
  const sections = today
    ? [
        ["On now", list.filter(s => s.start <= t && t < s.end)],
        ["Later today", list.filter(s => s.start > t)],
        ["Earlier today", list.filter(s => s.end <= t && !(s.start <= t && t < s.end))],
      ]
    : [["", list]]
  body.innerHTML = `
    <div class="row-end"><a class="button" href="#/mine">Your sessions</a></div>
    ${filters(data, track)}
    <nav class="days" aria-label="Days">${days
      .map(d => `<a class="chip ${d === day ? "on" : ""}" href="#/d/${d}" ${d === day ? 'aria-current="date"' : ""}>${esc(d === dayKey(t) ? "Today" : dayName(d, "short"))}</a>`)
      .join("")}</nav>
    <h2>${esc(dayName(day))}</h2>
    ${sections
      .filter(([, items]) => items.length)
      .map(([title, items]) => `${title ? `<h3 class="day">${esc(title)}</h3>` : ""}<div class="stack">${items.map(s => row(data, s, t)).join("")}</div>`)
      .join("")}`
  bindFilters(day)
  document.querySelector(".days .on")?.scrollIntoView({ inline: "center", block: "nearest" })
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
      dayPage(day)
    }
}

async function minePage() {
  render(`${header("Your sessions", "#/")}<div id="body"><p class="muted">Loading…</p></div>`)
  bindCommon(route)
  const body = document.getElementById("body")
  if (!(await requireSignIn(body))) return
  let data
  try {
    data = await loadProgramme()
  } catch (e) {
    body.innerHTML = failure(e)
    return
  }
  const t = now()
  const me = session.currentPubkey()
  const mine = data.sessions.filter(s => {
    const a = rsvpsFor(data.rsvps, s.coordinate).get(me)
    return s.end > t && (a === "accepted" || a === "tentative" || s.pubkey === me)
  })
  let day = ""
  body.innerHTML = mine.length
    ? mine
        .map(s => {
          const d = dayKey(s.start)
          const heading = d !== day ? `<h3 class="day">${esc(dayName(d))}</h3>` : ""
          day = d
          return heading + row(data, s, t)
        })
        .join("")
    : '<p class="muted">Sessions you are going to (or might go to) and your own show up here. <a href="#/">See the programme</a>.</p>'
}

async function sessionPage(code) {
  render(`${header("Session", "#/")}<div id="body"><p class="muted">Loading…</p></div>`)
  bindCommon(route)
  const body = document.getElementById("body")
  if (!(await requireSignIn(body))) return
  let s, data, answers, comments, photos, zaps, profiles
  try {
    const ref = nip19.decode(code)
    if (ref.type !== "naddr" || ref.data.kind !== SESSION_KIND) throw new Error("not a session link")
    data = await loadProgramme()
    const coordinate = `${SESSION_KIND}:${ref.data.pubkey}:${ref.data.identifier}`
    s = data.sessions.find(x => x.coordinate === coordinate)
    if (!s) throw Object.assign(new Error("This session is not in the programme (any more)."), { status: 404 })
    const [rsvpEvents, commentEvents, photoEvents, zapEvents] = await Promise.all([
      query({ kinds: [RSVP_KIND], "#a": [coordinate] }),
      query({ kinds: [1111], "#A": [coordinate] }),
      query({ kinds: [PHOTO_KIND], "#a": [coordinate] }),
      s.official ? Promise.resolve([]) : query({ kinds: [9321], "#a": [coordinate] }),
    ])
    answers = rsvpsFor(
      rsvpEvents.filter(e => data.active.has(e.pubkey)),
      coordinate,
    )
    comments = commentEvents
      .filter(c => data.active.has(c.pubkey) && c.content.trim() && !(data.hidden.has(c.id) && !isMe(c.pubkey)))
      .sort((a, b) => a.created_at - b.created_at)
    const photoById = new Map(photoEvents.filter(e => data.active.has(e.pubkey)).map(e => [e.id, e]))
    const posters = [...new Set([...photoById.values()].map(e => e.pubkey))]
    const gone = posters.length ? deletedIds(await queryAuthors({ kinds: [5], "#k": [String(PHOTO_KIND)] }, posters), photoById) : new Set()
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
        tipLinks.length
          ? `<div class="actions tight"><span class="label">Thank the host</span>${tipLinks.map(([n, u]) => `<a class="button" href="${esc(u)}">${tokens(n)}</a>`).join("")}</div>
             ${tipped ? `<p class="small muted">${tipped} thank-you${tipped === 1 ? "" : "s"} in tokens so far.</p>` : ""}`
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
        const event = await publish(rsvpTemplate(s, b.dataset.rsvp))
        data.rsvps.push(event)
        sessionPage(code)
      } catch (err) {
        for (const x of body.querySelectorAll("[data-rsvp]")) x.disabled = false
        notice(`Could not save your answer: ${err.message}`, "bad")
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

function route() {
  const hash = location.hash || "#/"
  let m
  if ((m = hash.match(/^#\/s\/(naddr1\w+)$/))) return sessionPage(m[1])
  if ((m = hash.match(/^#\/d\/(\d{4}-\d{2}-\d{2})$/))) return dayPage(m[1])
  if (hash === "#/mine") return minePage()
  if (hash === "#/signer") return signerPage("Your own signer", () => (location.hash = "#/"))
  if (hash === "#/self-custody") return signerPage("Your own signer", () => (location.hash = "#/"), true)
  return dayPage()
}

boot(route)
