// Open Source Village photos: pictures shared between participants (NIP-68, kind 20), stored on
// Belfry's Blossom server. Likes, comments and tips in village tokens through the wallet. Hash
// routes: #/ (everyone's photos), #/t/<hashtag>, #/u/<npub> (one person's), #/p/<event id> (one
// post), #/new, #/signer.
import "../shared/style.css"
import { nip19 } from "nostr-tools"
import * as session from "../shared/session.js"
import {
  MAX_PHOTOS,
  PHOTO_KIND,
  commentTemplate,
  deletedIds,
  likeTemplate,
  nutzapAmount,
  parseHashtags,
  photoFromEvent,
  photoTemplate,
  profileName,
} from "../shared/model.js"
import { isMe, loadProfiles, participants, publish, query, queryAuthors, shrinkImageWithSize, uploadDescriptor } from "../shared/nostr.js"
import { avatar, bindCommon, boot, esc, header, notice, render, signInCard, signerPage, walletPayLink } from "../shared/ui.js"
import { bindReports, hiddenRefs, reportLink } from "../shared/report.js"

const WHY = "Participants share their pictures of the village here. Sign in to see them and to add your own."
const PEOPLE_URL = (import.meta.env.VITE_PEOPLE_URL || "").replace(/\/$/, "")
const FEED_LIMIT = 500
const TIPS = [1, 5, 10]
const when = t => new Date(t * 1000).toLocaleString([], { dateStyle: "medium", timeStyle: "short" })
const tokens = n => `${n} token${n === 1 ? "" : "s"}`

function failure(e) {
  if (e.status === 403) return '<p class="notice bad">The photos are for participants with an active pass.</p>'
  return `<p class="notice bad">Could not load the photos: ${esc(e.message)}</p>`
}

async function requireSignIn(body) {
  if (session.currentPubkey()) return true
  body.innerHTML = signInCard(WHY)
  bindCommon(route)
  return false
}

/** Photo posts of active participants (optionally one author), newest first, without deleted ones. */
async function activePhotos(author = null) {
  const active = await participants()
  if (author && !active.has(author)) return []
  const authors = author ? [author] : [...active]
  const [events, hidden] = await Promise.all([queryAuthors({ kinds: [PHOTO_KIND], limit: FEED_LIMIT }, authors), hiddenRefs()])
  const byId = new Map(events.map(e => [e.id, e]))
  const posters = [...new Set(events.map(e => e.pubkey))]
  const gone = posters.length ? deletedIds(await queryAuthors({ kinds: [5], "#k": [String(PHOTO_KIND)] }, posters), byId) : new Set()
  return [...byId.values()]
    .filter(e => !gone.has(e.id) && !(hidden.has(e.id) && !isMe(e.pubkey)))
    .map(photoFromEvent)
    .filter(Boolean)
    .sort((a, b) => b.createdAt - a.createdAt)
}

function tile(p) {
  const img = p.images[0]
  return `
    <a class="photo-tile" href="#/p/${p.id}" title="${esc(p.title || p.caption.slice(0, 80))}">
      <img src="${esc(img.url)}" alt="${esc(img.alt || p.title)}" loading="lazy" referrerpolicy="no-referrer" />
      ${p.images.length > 1 ? `<span class="photo-count">${p.images.length}</span>` : ""}
    </a>`
}

/** The feed: everyone's photos, one hashtag's, or one person's. */
async function feedPage({ hashtag = "", author = null } = {}) {
  const title = author ? "Photos" : hashtag ? `#${hashtag}` : "Photos"
  render(`${header(title, author || hashtag ? "#/" : null)}<div id="body"></div>`)
  bindCommon(route)
  const body = document.getElementById("body")
  if (!(await requireSignIn(body))) return
  body.innerHTML = `
    <div class="row-end">
      <a class="button" href="#/u/${nip19.npubEncode(session.currentPubkey())}">Yours</a>
      <a class="primary button" href="#/new">Share photos</a>
    </div>
    <div id="who"></div>
    <div class="chips" id="tags"></div>
    <div id="grid" class="photo-grid"><p class="muted">Loading…</p></div>`
  let photos
  try {
    photos = await activePhotos(author)
  } catch (e) {
    document.getElementById("grid").outerHTML = failure(e)
    return
  }
  if (author) {
    const profile = (await loadProfiles([author])).get(author)
    const npub = nip19.npubEncode(author)
    document.getElementById("who").innerHTML = `
      <div class="person card">${avatar(profile, 40, author)}<span class="person-text"><span class="person-name">${esc(profileName(profile, "A participant"))}${
        isMe(author) ? ' <span class="badge">you</span>' : ""
      }</span>${PEOPLE_URL ? `<a class="small" href="${esc(`${PEOPLE_URL}/#/p/${npub}`)}">In the directory</a>` : ""}</span></div>`
  }
  // the most used hashtags of what is loaded, as filters
  const counts = new Map()
  for (const p of photos) for (const h of p.hashtags) counts.set(h, (counts.get(h) || 0) + 1)
  const top = [...counts].sort((a, b) => b[1] - a[1] || a[0].localeCompare(b[0])).slice(0, 12)
  if (!author && top.length) {
    document.getElementById("tags").innerHTML =
      `<a class="chip ${hashtag ? "" : "on"}" href="#/">All</a>` +
      top.map(([h]) => `<a class="chip ${h === hashtag ? "on" : ""}" href="#/t/${encodeURIComponent(h)}">#${esc(h)}</a>`).join("")
  }
  const shown = hashtag ? photos.filter(p => p.hashtags.includes(hashtag)) : photos
  document.getElementById("grid").innerHTML = shown.length
    ? shown.map(tile).join("")
    : `<p class="muted">${author ? (isMe(author) ? 'You have not shared photos yet. <a href="#/new">Share photos</a>.' : "No photos yet.") : 'No photos yet. <a href="#/new">Share the first ones</a>.'}</p>`
}

/** Likes (unique authors, the newest reaction each), comments and tips of one post. */
async function reactionsFor(p, active, hidden) {
  const [reactions, comments, zaps] = await Promise.all([
    query({ kinds: [7], "#e": [p.id] }),
    query({ kinds: [1111], "#E": [p.id] }),
    query({ kinds: [9321], "#e": [p.id] }),
  ])
  const newestReaction = new Map()
  for (const r of reactions) {
    if (!active.has(r.pubkey)) continue
    const prev = newestReaction.get(r.pubkey)
    if (!prev || r.created_at > prev.created_at) newestReaction.set(r.pubkey, r)
  }
  const likers = [...newestReaction].filter(([, r]) => r.content !== "-").map(([pk]) => pk)
  return {
    likers,
    comments: comments
      .filter(c => active.has(c.pubkey) && c.content.trim() && !(hidden.has(c.id) && !isMe(c.pubkey)))
      .sort((a, b) => a.created_at - b.created_at),
    // a tip counts only when it went to the photographer
    tips: zaps
      .filter(z => z.tags.some(t => t[0] === "p" && t[1] === p.pubkey))
      .map(z => ({ from: z.pubkey, at: z.created_at, amount: nutzapAmount(z) })),
  }
}

async function photoPage(id) {
  render(`${header("Photo", "#/")}<div id="body"><p class="muted">Loading…</p></div>`)
  bindCommon(route)
  const body = document.getElementById("body")
  if (!(await requireSignIn(body))) return
  let p, active, r, profiles, hidden
  try {
    active = await participants()
    const event = (await query({ ids: [id], kinds: [PHOTO_KIND] }))[0]
    p = event && photoFromEvent(event)
    if (!p) throw Object.assign(new Error("This photo is gone."), { status: 404 })
    if (!active.has(p.pubkey)) throw Object.assign(new Error("The photographer has no active pass any more."), { status: 410 })
    const dels = await query({ kinds: [5], authors: [p.pubkey], "#e": [p.id] })
    if (deletedIds(dels, new Map([[p.id, event]])).has(p.id)) throw Object.assign(new Error("This photo is gone."), { status: 404 })
    hidden = await hiddenRefs()
    if (hidden.has(p.id) && !isMe(p.pubkey)) throw Object.assign(new Error("The stewards hid this photo."), { status: 404 })
    r = await reactionsFor(p, active, hidden)
    profiles = await loadProfiles([p.pubkey, ...r.comments.map(c => c.pubkey), ...r.tips.map(t => t.from)])
  } catch (e) {
    body.innerHTML = e.status === 403 ? failure(e) : `<p class="notice bad">${esc(e.message)}</p>`
    return
  }
  const mine = isMe(p.pubkey)
  const author = profiles.get(p.pubkey)
  const liked = r.likers.some(isMe)
  const myTips = r.tips.filter(t => isMe(t.from)).reduce((s, t) => s + t.amount, 0)
  const allTips = r.tips.reduce((s, t) => s + t.amount, 0)
  const memo = `Photo: ${p.title || "a picture"}`
  const tipLinks = mine ? [] : TIPS.map(n => [n, walletPayLink(p.pubkey, n, memo, p.id)]).filter(([, u]) => u)
  body.innerHTML = `
    <article class="card stack">
      <div class="person">${avatar(author, 40, p.pubkey)}<span class="person-text"><span class="person-name"><a href="#/u/${nip19.npubEncode(p.pubkey)}">${esc(
        profileName(author, "A participant"),
      )}</a></span><span class="small muted">${esc(when(p.createdAt))}${p.location ? ` · ${esc(p.location)}` : ""}</span></span></div>
      <div class="photo-full">${p.images
        .map(
          img => `<a href="${esc(img.url)}" target="_blank" rel="noopener"><img src="${esc(img.url)}" alt="${esc(img.alt)}" ${
            img.width ? `width="${img.width}" height="${img.height}"` : ""
          } referrerpolicy="no-referrer" /></a>`,
        )
        .join("")}</div>
      ${p.title ? `<h2>${esc(p.title)}</h2>` : ""}
      ${p.caption ? `<p class="pre">${esc(p.caption)}</p>` : ""}
      ${p.hashtags.length ? `<p class="chips">${p.hashtags.map(h => `<a class="chip" href="#/t/${encodeURIComponent(h)}">#${esc(h)}</a>`).join("")}</p>` : ""}
      <div class="actions">
        <button id="like" ${liked ? "disabled" : ""}>${liked ? "Liked" : "Like"}</button>
        <span class="muted small">${r.likers.length} like${r.likers.length === 1 ? "" : "s"}${allTips ? ` · ${tokens(allTips)} in tips` : ""}</span>
        ${mine ? '<button class="danger" id="delete">Delete</button>' : reportLink({ kind: "photo", ref: p.id, pubkey: p.pubkey, excerpt: [p.title, p.caption].filter(Boolean).join(" — ") })}
      </div>
      ${mine && hidden.has(p.id) ? '<p class="notice bad">The stewards hid this photo after a report: other participants do not see it. Ask a steward if you think this is a mistake.</p>' : ""}
      ${
        tipLinks.length
          ? `<div class="actions tight"><span class="label">Tip the photographer</span>${tipLinks
              .map(([n, u]) => `<a class="button" href="${esc(u)}">${tokens(n)}</a>`)
              .join("")}</div>
             ${myTips ? `<p class="notice good">You tipped ${tokens(myTips)}. Thank you!</p>` : ""}
             <p class="small muted">Tipping opens your wallet with the amount filled in and comes back here.</p>`
          : ""
      }
      ${
        mine && r.tips.length
          ? `<h3>Tips</h3><ul>${r.tips
              .map(t => `<li>${tokens(t.amount)} from ${esc(profileName(profiles.get(t.from), "a participant"))}, ${esc(when(t.at))}</li>`)
              .join("")}</ul>`
          : ""
      }
      <h3>Comments</h3>
      <div class="stack" id="comments">${
        r.comments.length
          ? r.comments
              .map(
                c => `<div class="comment">${avatar(profiles.get(c.pubkey), 28, c.pubkey)}<div><span class="small"><a href="#/u/${nip19.npubEncode(c.pubkey)}">${esc(
                  profileName(profiles.get(c.pubkey), "A participant"),
                )}</a> <span class="muted">${esc(when(c.created_at))}</span>${
                  isMe(c.pubkey) ? "" : ` ${reportLink({ kind: "comment", ref: c.id, pubkey: c.pubkey, excerpt: c.content }, "Report")}`
                }</span><p class="pre">${esc(c.content)}</p></div></div>`,
              )
              .join("")
          : '<p class="muted small">No comments yet.</p>'
      }</div>
      <form id="comment" class="stack">
        <label>Your comment<textarea id="text" rows="2" maxlength="2000" required></textarea></label>
        <button type="submit">Comment</button>
      </form>
    </article>`
  bindReports(body)
  document.getElementById("like").onclick = async e => {
    e.target.disabled = true
    try {
      await publish(likeTemplate(p))
      photoPage(id)
    } catch (err) {
      e.target.disabled = false
      notice(`Could not like it: ${err.message}`, "bad")
    }
  }
  const del = document.getElementById("delete")
  if (del)
    del.onclick = async () => {
      if (!confirm("Delete this post? The pictures disappear from the app.")) return
      del.disabled = true
      try {
        await publish({
          kind: 5,
          created_at: Math.floor(Date.now() / 1000),
          tags: [
            ["e", p.id],
            ["k", String(PHOTO_KIND)],
          ],
          content: "deleted by the photographer",
        })
        location.hash = "#/"
      } catch (err) {
        del.disabled = false
        notice(`Could not delete it: ${err.message}`, "bad")
      }
    }
  document.getElementById("comment").onsubmit = async ev => {
    ev.preventDefault()
    const btn = ev.target.querySelector("button")
    const text = document.getElementById("text").value.trim()
    if (!text) return
    btn.disabled = true
    try {
      await publish(commentTemplate(p, text))
      photoPage(id)
    } catch (err) {
      btn.disabled = false
      notice(`Could not comment: ${err.message}`, "bad")
    }
  }
}

async function newPage() {
  render(`${header("Share photos", "#/")}<div id="body"><p class="muted">Loading…</p></div>`)
  bindCommon(route)
  const body = document.getElementById("body")
  if (!(await requireSignIn(body))) return
  try {
    await participants()
  } catch (e) {
    body.innerHTML = failure(e)
    return
  }
  const images = []
  body.innerHTML = `
    <form class="card stack" id="form">
      <div><span class="label">Pictures (up to ${MAX_PHOTOS})</span>
        <div class="stack" id="images"></div>
        <input id="file" type="file" accept="image/*" multiple /></div>
      <p class="small muted">Pictures are shrunk to 2048 pixels and their location data is removed before they leave your device. Everyone with a pass can see them.</p>
      <label>Title<input id="title" maxlength="100" placeholder="Sunrise over the village" /></label>
      <label>Caption<textarea id="caption" rows="3" maxlength="2000"></textarea></label>
      <label>Hashtags<input id="hashtags" maxlength="200" placeholder="#workshop #food" /></label>
      <label>Where<input id="location" maxlength="100" placeholder="Workshop tent, the lake, …" /></label>
      <button class="primary" type="submit" disabled>Share</button>
    </form>`
  const submit = body.querySelector("button[type=submit]")
  const show = () => {
    const box = document.getElementById("images")
    box.innerHTML = images
      .map(
        (img, i) => `<div class="upload-row"><img src="${esc(img.url)}" alt="" referrerpolicy="no-referrer" />
          <span class="stack"><input data-alt="${i}" maxlength="300" placeholder="Describe it for people who can't see it" value="${esc(img.alt)}" />
          <button type="button" class="link" data-rm="${i}">Remove</button></span></div>`,
      )
      .join("")
    for (const el of box.querySelectorAll("[data-alt]")) el.oninput = () => (images[Number(el.dataset.alt)].alt = el.value.trim())
    for (const b of box.querySelectorAll("[data-rm]"))
      b.onclick = () => {
        images.splice(Number(b.dataset.rm), 1)
        show()
      }
    document.getElementById("file").disabled = images.length >= MAX_PHOTOS
    submit.disabled = !images.length
  }
  show()
  document.getElementById("file").onchange = async e => {
    const files = [...e.target.files].slice(0, MAX_PHOTOS - images.length)
    e.target.value = ""
    for (const [i, file] of files.entries()) {
      notice(`Uploading picture ${i + 1} of ${files.length}…`)
      try {
        const { blob, width, height } = await shrinkImageWithSize(file, 2048)
        const d = await uploadDescriptor(blob, "Upload a photo")
        images.push({ url: d.url, m: d.type || "image/jpeg", x: d.sha256, width, height, alt: "" })
        show()
      } catch (err) {
        notice(`Could not upload ${file.name}: ${err.message}`, "bad")
        return
      }
    }
    notice("")
  }
  document.getElementById("form").onsubmit = async ev => {
    ev.preventDefault()
    submit.disabled = true
    const val = id => document.getElementById(id).value.trim()
    try {
      const event = await publish(
        photoTemplate({ title: val("title"), caption: val("caption"), hashtags: parseHashtags(val("hashtags")), location: val("location"), images }),
      )
      location.hash = `#/p/${event.id}`
    } catch (e) {
      notice(`Could not share: ${e.message}`, "bad")
      submit.disabled = false
    }
  }
}

function route() {
  const hash = location.hash || "#/"
  let m
  if ((m = hash.match(/^#\/p\/([0-9a-f]{64})$/))) return photoPage(m[1])
  if ((m = hash.match(/^#\/u\/(npub1\w+)$/))) {
    try {
      return feedPage({ author: nip19.decode(m[1]).data })
    } catch {
      return feedPage()
    }
  }
  if ((m = hash.match(/^#\/t\/(.+)$/))) return feedPage({ hashtag: decodeURIComponent(m[1]).toLowerCase() })
  if (hash === "#/new") return newPage()
  if (hash === "#/signer") return signerPage("Your own signer", () => (location.hash = "#/"))
  if (hash === "#/self-custody") return signerPage("Your own signer", () => (location.hash = "#/"), true)
  return feedPage()
}

boot(route)
