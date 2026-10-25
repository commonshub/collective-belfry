// Open Source Village marketplace: things, food, help and skills offered between participants,
// paid in village tokens through the wallet (a nutzap to the seller). Hash routes: #/ (listings),
// #/l/<naddr> (one listing), #/new, #/edit/<d>, #/mine, #/signer.
import "../shared/style.css"
import { nip19 } from "nostr-tools"
import * as session from "../shared/session.js"
import { CATEGORIES, LISTING_KIND, listingFromEvent, listingTemplate, profileName } from "../shared/model.js"
import { isMe, loadProfiles, participants, publish, query, queryAuthors, shrinkImage, uploadBlob } from "../shared/nostr.js"
import { avatar, bindCommon, boot, chatLink, esc, header, notice, render, signInCard, signerPage, walletPayLink } from "../shared/ui.js"
import { bindReports, hiddenRefs, reportLink } from "../shared/report.js"

const WHY = "Participants offer things, food, help and skills here, paid in village tokens. Sign in to see the offers and to add your own."
const PEOPLE_URL = (import.meta.env.VITE_PEOPLE_URL || "").replace(/\/$/, "")
const MAX_IMAGES = 4
const categoryName = key => CATEGORIES.find(([k]) => k === key)?.[1] || "Other"
const priceText = l => (l.price === null ? "Price on request" : l.price === 0 ? "Free" : `${l.price} token${l.price === 1 ? "" : "s"}`)
const naddr = l => nip19.naddrEncode({ kind: LISTING_KIND, pubkey: l.pubkey, identifier: l.d })

/** Newest version of every listing (addressable events: one per author and d tag). */
function newest(events) {
  const out = new Map()
  for (const e of events) {
    const l = listingFromEvent(e)
    if (!l) continue
    const prev = out.get(l.coordinate)
    if (!prev || l.createdAt > prev.createdAt) out.set(l.coordinate, l)
  }
  return [...out.values()]
}

/** Coordinates the author deleted (NIP-09 kind 5 with an "a" tag). */
async function deletedCoordinates(authors) {
  const dels = await queryAuthors({ kinds: [5], "#k": [String(LISTING_KIND)] }, authors)
  return new Set(dels.flatMap(e => e.tags.filter(t => t[0] === "a" && t[1].split(":")[1] === e.pubkey).map(t => t[1])))
}

async function activeListings() {
  const active = [...(await participants())]
  const [listings, hidden] = await Promise.all([queryAuthors({ kinds: [LISTING_KIND] }, active).then(newest), hiddenRefs()])
  const gone = await deletedCoordinates([...new Set(listings.map(l => l.pubkey))])
  return listings
    .filter(l => !gone.has(l.coordinate) && !(hidden.has(l.coordinate) && !isMe(l.pubkey)))
    .sort((a, b) => b.publishedAt - a.publishedAt)
}

function failure(e) {
  if (e.status === 403) return '<p class="notice bad">The marketplace is for participants with an active pass.</p>'
  return `<p class="notice bad">Could not load the marketplace: ${esc(e.message)}</p>`
}

function listingCard(l, seller) {
  return `
    <a class="card listing" href="#/l/${naddr(l)}">
      ${l.images[0] ? `<img class="thumb" src="${esc(l.images[0])}" alt="" loading="lazy" referrerpolicy="no-referrer" />` : '<span class="thumb blank"></span>'}
      <span class="listing-text">
        <span class="listing-title">${esc(l.title)}${l.status === "sold" ? ' <span class="badge">gone</span>' : ""}</span>
        <span class="price">${esc(priceText(l))}</span>
        <span class="small muted">${esc(categoryName(l.category))} · ${esc(profileName(seller, "a participant"))}${isMe(l.pubkey) ? " (you)" : ""}</span>
        ${l.summary ? `<span class="about">${esc(l.summary)}</span>` : ""}
      </span>
    </a>`
}

async function requireSignIn(body) {
  if (session.currentPubkey()) return true
  body.innerHTML = signInCard(WHY)
  bindCommon(route)
  return false
}

async function listPage() {
  render(`${header("Marketplace")}<div id="body"></div>`)
  bindCommon(route)
  const body = document.getElementById("body")
  if (!(await requireSignIn(body))) return
  body.innerHTML = `
    <div class="row-end"><a class="button" href="#/mine">Your offers</a><a class="primary button" href="#/new">Offer something</a></div>
    <input id="search" type="search" placeholder="Search offers" aria-label="Search" />
    <div class="chips" id="cats"><button class="chip on" data-cat="">All</button>${CATEGORIES.map(([k, n]) => `<button class="chip" data-cat="${k}">${esc(n)}</button>`).join("")}</div>
    <div id="list" class="stack"><p class="muted">Loading…</p></div>`
  let listings, sellers
  try {
    listings = (await activeListings()).filter(l => l.status === "active" || isMe(l.pubkey))
    sellers = await loadProfiles(listings.map(l => l.pubkey))
  } catch (e) {
    document.getElementById("list").innerHTML = failure(e)
    return
  }
  let cat = ""
  let term = ""
  const box = document.getElementById("list")
  const show = () => {
    const t = term.trim().toLowerCase()
    const hits = listings.filter(l => (!cat || l.category === cat) && (!t || [l.title, l.summary, l.description, l.location].some(v => v.toLowerCase().includes(t))))
    box.innerHTML = hits.length
      ? hits.map(l => listingCard(l, sellers.get(l.pubkey))).join("")
      : listings.length
        ? '<p class="muted">Nothing matches.</p>'
        : '<p class="muted">No offers yet. <a href="#/new">Offer something</a>.</p>'
  }
  show()
  document.getElementById("search").oninput = e => {
    term = e.target.value
    show()
  }
  for (const b of document.querySelectorAll("#cats .chip"))
    b.onclick = () => {
      cat = b.dataset.cat
      for (const x of document.querySelectorAll("#cats .chip")) x.classList.toggle("on", x === b)
      show()
    }
}

/** Nutzaps (kind 9321) that name this listing in an "a" tag: what was paid for it. */
async function paymentsFor(l) {
  const zaps = await query({ kinds: [9321], "#a": [l.coordinate] })
  return zaps.map(z => ({
    from: z.pubkey,
    at: z.created_at,
    amount: z.tags
      .filter(t => t[0] === "proof")
      .reduce((sum, t) => {
        try {
          return sum + (JSON.parse(t[1]).amount || 0)
        } catch {
          return sum
        }
      }, 0),
  }))
}

async function listingPage(code) {
  render(`${header("Offer", "#/")}<div id="body"><p class="muted">Loading…</p></div>`)
  bindCommon(route)
  const body = document.getElementById("body")
  if (!(await requireSignIn(body))) return
  let l, seller, payments
  try {
    const a = nip19.decode(code).data
    if (!(await participants()).has(a.pubkey)) throw Object.assign(new Error("The seller has no active pass any more."), { status: 410 })
    l = newest(await query({ kinds: [LISTING_KIND], authors: [a.pubkey], "#d": [a.identifier] }))[0]
    if (!l || (await deletedCoordinates([a.pubkey])).has(l.coordinate)) throw Object.assign(new Error("This offer is gone."), { status: 404 })
    l.hidden = (await hiddenRefs()).has(l.coordinate)
    if (l.hidden && !isMe(l.pubkey)) throw Object.assign(new Error("The stewards hid this offer."), { status: 404 })
    seller = (await loadProfiles([l.pubkey])).get(l.pubkey)
    payments = await paymentsFor(l)
  } catch (e) {
    body.innerHTML = e.status === 403 ? failure(e) : `<p class="notice bad">${esc(e.message)}</p>`
    return
  }
  const mine = isMe(l.pubkey)
  const paidByMe = payments.filter(p => isMe(p.from))
  const pay = l.price > 0 && l.status === "active" && !mine ? walletPayLink(l.pubkey, l.price, `Marketplace: ${l.title}`, l.coordinate) : ""
  const chat = mine ? "" : chatLink(l.pubkey)
  const profileUrl = PEOPLE_URL ? `${PEOPLE_URL}/#/p/${nip19.npubEncode(l.pubkey)}` : ""
  const payersProfiles = mine ? await loadProfiles(payments.map(p => p.from)) : new Map()
  body.innerHTML = `
    <article class="card stack">
      ${l.images.length ? `<div class="gallery">${l.images.map(u => `<img src="${esc(u)}" alt="" referrerpolicy="no-referrer" />`).join("")}</div>` : ""}
      <h2>${esc(l.title)}${l.status === "sold" ? ' <span class="badge">gone</span>' : ""}</h2>
      <p class="price big">${esc(priceText(l))}</p>
      ${l.summary ? `<p><strong>${esc(l.summary)}</strong></p>` : ""}
      ${l.description ? `<p class="pre">${esc(l.description)}</p>` : ""}
      <p class="small muted">${esc(categoryName(l.category))}${l.location ? ` · ${esc(l.location)}` : ""} · since ${new Date(l.publishedAt * 1000).toLocaleDateString()}</p>
      <div class="person">${avatar(seller, 40, l.pubkey)}<span class="person-text"><span class="person-name">${
        profileUrl ? `<a href="${esc(profileUrl)}">${esc(profileName(seller, "A participant"))}</a>` : esc(profileName(seller, "A participant"))
      }</span>${seller?.nip05 ? `<span class="small muted">${esc(seller.nip05)}</span>` : ""}</span></div>
      ${paidByMe.length ? `<p class="notice good">You paid ${paidByMe.reduce((s, p) => s + p.amount, 0)} tokens for this. Agree with the seller how you get it.</p>` : ""}
      <div class="actions">
        ${pay ? `<a class="primary button" id="pay" href="${esc(pay)}">Pay ${l.price} tokens</a>` : ""}
        ${chat ? `<a class="button" href="${esc(chat)}">Message the seller</a>` : ""}
        ${mine ? `<a class="button" href="#/edit/${encodeURIComponent(l.d)}">Edit</a>` : ""}
      </div>
      ${pay ? '<p class="small muted">Paying opens your wallet with the amount filled in and comes back here.</p>' : ""}
      ${mine && l.hidden ? '<p class="notice bad">The stewards hid this offer after a report: other participants do not see it. Ask a steward if you think this is a mistake.</p>' : ""}
      ${mine ? "" : `<p class="small">${reportLink({ kind: "listing", ref: l.coordinate, pubkey: l.pubkey, excerpt: [l.title, l.summary].filter(Boolean).join(" — ") })}</p>`}
      ${
        mine
          ? `<h3>Payments</h3>${
              payments.length
                ? `<ul>${payments.map(p => `<li>${p.amount} tokens from ${esc(profileName(payersProfiles.get(p.from), "a participant"))}, ${new Date(p.at * 1000).toLocaleString()}</li>`).join("")}</ul>`
                : '<p class="muted small">None yet. Payments arrive in your wallet.</p>'
            }`
          : ""
      }
    </article>`
  bindReports(body)
}

async function myListingsPage() {
  render(`${header("Your offers", "#/")}<div id="body"><p class="muted">Loading…</p></div>`)
  bindCommon(route)
  const body = document.getElementById("body")
  if (!(await requireSignIn(body))) return
  const me = session.currentPubkey()
  let mine
  try {
    await participants()
    const gone = await deletedCoordinates([me])
    mine = newest(await query({ kinds: [LISTING_KIND], authors: [me] })).filter(l => !gone.has(l.coordinate))
  } catch (e) {
    body.innerHTML = failure(e)
    return
  }
  const profile = (await loadProfiles([me])).get(me)
  body.innerHTML = `<div class="row-end"><a class="primary button" href="#/new">Offer something</a></div>${
    mine.length
      ? mine
          .sort((a, b) => b.publishedAt - a.publishedAt)
          .map(
            l => `${listingCard(l, profile)}
            <div class="actions tight">
              <button data-act="toggle" data-d="${esc(l.d)}">${l.status === "sold" ? "Offer it again" : "Mark as gone"}</button>
              <a class="button" href="#/edit/${encodeURIComponent(l.d)}">Edit</a>
              <button class="danger" data-act="delete" data-d="${esc(l.d)}">Delete</button>
            </div>`,
          )
          .join("")
      : '<p class="muted">You offer nothing yet.</p>'
  }`
  for (const b of body.querySelectorAll("button[data-act]")) {
    b.onclick = async () => {
      const l = mine.find(x => x.d === b.dataset.d)
      b.disabled = true
      try {
        if (b.dataset.act === "toggle") {
          await publish(listingTemplate({ ...l, status: l.status === "sold" ? "active" : "sold" }))
        } else {
          if (!confirm(`Delete "${l.title}"?`)) return
          await publish({
            kind: 5,
            created_at: Math.floor(Date.now() / 1000),
            tags: [
              ["a", l.coordinate],
              ["e", l.id],
              ["k", String(LISTING_KIND)],
            ],
            content: "deleted by the seller",
          })
        }
        myListingsPage()
      } catch (e) {
        notice(`Could not change it: ${e.message}`, "bad")
      } finally {
        b.disabled = false
      }
    }
  }
}

async function editPage(d) {
  render(`${header(d ? "Edit your offer" : "Offer something", d ? "#/mine" : "#/")}<div id="body"><p class="muted">Loading…</p></div>`)
  bindCommon(route)
  const body = document.getElementById("body")
  if (!(await requireSignIn(body))) return
  const me = session.currentPubkey()
  let l = { d: "", title: "", summary: "", description: "", price: 10, category: "stuff", location: "", images: [], status: "active" }
  try {
    await participants()
    if (d) {
      const found = newest(await query({ kinds: [LISTING_KIND], authors: [me], "#d": [d] }))[0]
      if (!found) throw new Error("This offer is not yours or is gone.")
      l = found
    }
  } catch (e) {
    body.innerHTML = e.status === 403 ? failure(e) : `<p class="notice bad">${esc(e.message)}</p>`
    return
  }
  let images = [...l.images]
  body.innerHTML = `
    <form class="card stack" id="form">
      <label>What you offer<input id="title" required maxlength="100" value="${esc(l.title)}" placeholder="Hand-made mug, a ride to the station, a Rust workshop…" /></label>
      <label>One line about it<input id="summary" maxlength="160" value="${esc(l.summary)}" /></label>
      <label>Details<textarea id="description" rows="5" maxlength="4000" placeholder="Condition, size, when and where to pick it up">${esc(l.description)}</textarea></label>
      <div class="two">
        <label>Price in tokens<input id="price" type="number" min="0" max="100000" step="1" required value="${l.price ?? 0}" /></label>
        <label>Kind<select id="category">${CATEGORIES.map(([k, n]) => `<option value="${k}" ${k === l.category ? "selected" : ""}>${esc(n)}</option>`).join("")}</select></label>
      </div>
      <p class="small muted">0 tokens means free. Village tokens have no money value.</p>
      <label>Where<input id="location" maxlength="100" value="${esc(l.location)}" placeholder="Workshop tent, room 2, …" /></label>
      <div><span class="label">Photos (up to ${MAX_IMAGES})</span><div class="gallery small" id="images"></div>
        <input id="file" type="file" accept="image/*" /></div>
      <button class="primary" type="submit">${d ? "Save" : "Publish the offer"}</button>
    </form>`
  const showImages = () => {
    document.getElementById("images").innerHTML = images
      .map((u, i) => `<span class="img-wrap"><img src="${esc(u)}" alt="" referrerpolicy="no-referrer" /><button type="button" class="link" data-rm="${i}">Remove</button></span>`)
      .join("")
    for (const b of document.querySelectorAll("[data-rm]"))
      b.onclick = () => {
        images.splice(Number(b.dataset.rm), 1)
        showImages()
      }
    document.getElementById("file").disabled = images.length >= MAX_IMAGES
  }
  showImages()
  document.getElementById("file").onchange = async e => {
    const file = e.target.files[0]
    e.target.value = ""
    if (!file) return
    notice("Uploading the photo…")
    try {
      images.push(await uploadBlob(await shrinkImage(file)))
      notice("")
      showImages()
    } catch (err) {
      notice(`Could not upload the photo: ${err.message}`, "bad")
    }
  }
  document.getElementById("form").onsubmit = async ev => {
    ev.preventDefault()
    const btn = ev.target.querySelector("button[type=submit]")
    btn.disabled = true
    const val = id => document.getElementById(id).value.trim()
    try {
      const next = {
        ...l,
        d: l.d || crypto.randomUUID(),
        title: val("title"),
        summary: val("summary"),
        description: val("description"),
        price: Math.max(0, Math.floor(Number(val("price")) || 0)),
        category: val("category"),
        location: val("location"),
        images,
      }
      await publish(listingTemplate(next))
      location.hash = `#/l/${naddr({ ...next, pubkey: me })}`
    } catch (e) {
      notice(`Could not publish: ${e.message}`, "bad")
      btn.disabled = false
    }
  }
}

function route() {
  const hash = location.hash || "#/"
  let m
  if ((m = hash.match(/^#\/l\/(naddr1\w+)$/))) return listingPage(m[1])
  if ((m = hash.match(/^#\/edit\/(.+)$/))) return editPage(decodeURIComponent(m[1]))
  if (hash === "#/new") return editPage("")
  if (hash === "#/mine") return myListingsPage()
  if (hash === "#/signer") return signerPage("Your own signer", () => (location.hash = "#/"))
  if (hash === "#/self-custody") return signerPage("Your own signer", () => (location.hash = "#/"), true)
  return listPage()
}

boot(route)
