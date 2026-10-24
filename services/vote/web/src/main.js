// Open Source Village polls: list, create, vote anonymously, results. Hash routes:
// #/ (polls), #/new, #/poll/<id>, #/device (the voting key on this device).
import "./style.css"
import { Group, generateProof } from "@semaphore-protocol/core"
import { api } from "./api.js"
import * as session from "./session.js"
import { SSO_CALLBACK_PATH, endSsoSession, finishSso, shouldTrySilentSso, ssoEnabled, startSso } from "./sso.js"

const TREE_DEPTH = 10
const ARTIFACTS = { wasm: "/artifacts/semaphore-10.wasm", zkey: "/artifacts/semaphore-10.zkey" }
const app = document.getElementById("app")

const esc = s =>
  String(s ?? "").replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c])

function when(ts) {
  const d = new Date(ts * 1000)
  return d.toLocaleString(undefined, { weekday: "short", day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" })
}

function closesText(poll) {
  if (!poll.open) return `Closed ${when(poll.closes_at)}`
  const mins = Math.round((poll.closes_at - Date.now() / 1000) / 60)
  if (mins < 60) return `Closes in ${mins} min`
  if (mins < 48 * 60) return `Closes in ${Math.round(mins / 60)} h`
  return `Closes ${when(poll.closes_at)}`
}

function render(html) {
  app.innerHTML = html
}

function notice(text, kind = "info") {
  const el = document.getElementById("notice")
  if (el) {
    el.className = `notice ${kind}`
    el.textContent = text
    el.hidden = !text
  }
}

function header(title, back = false) {
  return `
    <header class="top">
      ${back ? '<a class="back" href="#/" aria-label="All polls">&larr;</a>' : ""}
      <h1>${esc(title)}</h1>
      ${session.currentPubkey() ? '<button class="link" id="signout">Log out</button>' : ""}
    </header>
    <p id="notice" class="notice" hidden></p>`
}

function signInCard() {
  if (!ssoEnabled) return '<div class="card"><p>Sign-in is not configured.</p></div>'
  return `
    <div class="card signin">
      <p>Sign in to open polls and to vote. Your vote stays anonymous: it is a zero-knowledge proof
      that it comes from one of the village's passes, not from whom.</p>
      <button class="primary" id="signin">Sign in with Open Source Village</button>
    </div>`
}

// -- pages ----------------------------------------------------------------------------------

async function listPage() {
  render(`${header("Polls")}<div id="setup"></div><div id="polls" class="stack"><p class="muted">Loading…</p></div>`)
  bindCommon()
  const setup = document.getElementById("setup")
  if (!session.currentPubkey()) setup.innerHTML = signInCard()
  else {
    setup.innerHTML = `<div class="row-end"><a class="primary button" href="#/new">New poll</a></div>`
    await ensureRegistered(setup)
  }
  bindCommon()
  try {
    const { polls } = await api.polls()
    const box = document.getElementById("polls")
    if (!polls.length) {
      box.innerHTML = '<p class="muted">No polls yet. Any participant can open one.</p>'
      return
    }
    box.innerHTML = polls
      .map(
        p => `
        <a class="card poll-link" href="#/poll/${esc(p.id)}">
          <span class="poll-title">${esc(p.title)}</span>
          <span class="meta"><span class="badge ${p.open ? "open" : ""}">${p.open ? "Open" : "Closed"}</span>
          ${esc(closesText(p))} · ${p.votes} of ${p.members} voted</span>
        </a>`,
      )
      .join("")
  } catch (e) {
    notice(`Could not load the polls: ${e.message}`, "bad")
  }
}

/** Registers this device's identity for the signed-in pass when it is not registered yet. */
async function ensureRegistered(box) {
  const local = session.identity()
  let me
  try {
    me = await api.me()
  } catch (e) {
    box.insertAdjacentHTML("beforeend", `<p class="notice bad">${esc(e.status === 403 ? "Voting needs an active pass." : e.message)}</p>`)
    return null
  }
  const registered = me.registration?.commitment
  if (local && registered === local.commitment.toString()) return me
  const explain = registered
    ? `<p>Your pass votes with a key from another device. Bring that key here with its export code
       (<a href="#/device">Voting key</a>), or start using a new key on this device: it counts from
       the next poll on, polls that are already open keep counting the other one.</p>`
    : `<p>To vote, this device creates a voting key. It stays on this device and is never sent
       anywhere; the village only learns its public part, which cannot be linked to your votes.</p>`
  box.insertAdjacentHTML(
    "beforeend",
    `<div class="card" id="register-card">${explain}
       <button class="primary" id="register">${registered ? "Use a new key on this device" : "Set up voting on this device"}</button>
     </div>`,
  )
  document.getElementById("register").onclick = async () => {
    const btn = document.getElementById("register")
    btn.disabled = true
    btn.textContent = "Setting up…"
    try {
      const id = session.identity() && !registered ? session.identity() : session.createIdentity()
      const r = await api.register(id.commitment.toString())
      document.getElementById("register-card").remove()
      notice(r.joined.length ? `Ready. You can vote in ${r.joined.length} open poll(s).` : "Ready to vote.", "good")
    } catch (e) {
      btn.disabled = false
      btn.textContent = "Try again"
      notice(e.message, "bad")
    }
  }
  return me
}

async function pollPage(id) {
  render(`${header("Poll", true)}<div id="poll" class="stack"><p class="muted">Loading…</p></div>`)
  bindCommon()
  let poll
  try {
    poll = await api.poll(id)
  } catch (e) {
    notice(e.status === 404 ? "This poll does not exist." : e.message, "bad")
    document.getElementById("poll").innerHTML = ""
    return
  }
  const box = document.getElementById("poll")
  const voted = session.currentPubkey() ? session.rememberedVote(id) : null
  let body = `
    <div class="card">
      <h2>${esc(poll.title)}</h2>
      ${poll.description ? `<p class="desc">${esc(poll.description)}</p>` : ""}
      <p class="meta"><span class="badge ${poll.open ? "open" : ""}">${poll.open ? "Open" : "Closed"}</span>
        ${esc(closesText(poll))} · ${poll.votes} of ${poll.members} voted</p>`
  if (!poll.open) {
    const total = poll.results.reduce((a, b) => a + b, 0)
    body += poll.options
      .map((o, i) => {
        const n = poll.results[i]
        const pct = total ? Math.round((n * 100) / total) : 0
        return `<div class="result"><div class="result-head"><span>${esc(o)}${voted === i ? " · your vote" : ""}</span><span>${n} (${pct}%)</span></div>
          <div class="bar"><span style="width:${pct}%"></span></div></div>`
      })
      .join("")
    body += `<p class="muted small">Every vote is a published zero-knowledge proof:
      <a href="/api/polls/${esc(id)}/proofs" target="_blank" rel="noopener">check the tally yourself</a>.</p></div>`
    box.innerHTML = body
    return
  }
  if (voted !== null) {
    body += `<p class="notice good">You voted “${esc(poll.options[voted])}” from this device. Results appear when the poll closes.</p></div>`
    box.innerHTML = body
    return
  }
  if (!session.currentPubkey()) {
    box.innerHTML = body + "</div>" + signInCard()
    bindCommon()
    return
  }
  body += `<form id="vote-form" class="options">
      ${poll.options
        .map((o, i) => `<label class="option"><input type="radio" name="option" value="${i}" required><span>${esc(o)}</span></label>`)
        .join("")}
      <button class="primary" type="submit">Vote anonymously</button>
    </form>
    <p class="muted small">Results appear when the poll closes. Your vote cannot be changed.</p>
    ${poll.members < 5 ? `<p class="notice bad small">Only ${poll.members} participant(s) can vote in this poll so far: with so few, the result may show who voted how.</p>` : ""}</div>`
  box.innerHTML = body
  document.getElementById("vote-form").onsubmit = async ev => {
    ev.preventDefault()
    const option = Number(new FormData(ev.target).get("option"))
    const btn = ev.target.querySelector("button")
    btn.disabled = true
    try {
      const identity = session.identity()
      if (!identity) throw new Error("Set up voting on this device first (on the polls page).")
      btn.textContent = "Checking…"
      const { commitment } = await api.myMembership(id)
      if (!commitment) throw new Error("Your pass is not in this poll: set up voting first, then it joins open polls.")
      if (commitment !== identity.commitment.toString()) {
        throw new Error("This poll counts the voting key of your other device. Vote there, or import its key.")
      }
      btn.textContent = "Making the proof…"
      const { commitments } = await api.group(id)
      const group = new Group(commitments.map(BigInt))
      const proof = await generateProof(identity, group, option, poll.scope, TREE_DEPTH, ARTIFACTS)
      btn.textContent = "Sending…"
      await api.vote(id, proof)
      session.rememberVote(id, option)
      pollPage(id)
    } catch (e) {
      btn.disabled = false
      btn.textContent = "Vote anonymously"
      notice(e.status === 409 ? "This pass has already voted in this poll." : e.message, "bad")
    }
  }
}

function newPage() {
  if (!session.currentPubkey()) {
    render(`${header("New poll", true)}${signInCard()}`)
    bindCommon()
    return
  }
  render(`${header("New poll", true)}
    <form id="new-form" class="card stack">
      <label>Question<input name="title" maxlength="200" required placeholder="What should we…?"></label>
      <label>Details (optional)<textarea name="description" maxlength="2000" rows="3"></textarea></label>
      <fieldset><legend>Options</legend><div id="opts"></div>
        <button type="button" class="link" id="add-opt">+ Add an option</button></fieldset>
      <label>Open for<select name="duration">
        <option value="3600">1 hour</option><option value="86400" selected>1 day</option>
        <option value="259200">3 days</option><option value="604800">1 week</option>
        <option value="1209600">2 weeks</option></select></label>
      <p class="muted small">Every participant with an active pass can vote once, anonymously.
        Results are shown when the poll closes.</p>
      <button class="primary" type="submit">Open the poll</button>
    </form>`)
  bindCommon()
  const opts = document.getElementById("opts")
  const addOpt = (value = "") => {
    if (opts.children.length >= 10) return
    opts.insertAdjacentHTML("beforeend", `<input name="option" maxlength="200" placeholder="Option ${opts.children.length + 1}" value="${esc(value)}">`)
  }
  addOpt()
  addOpt()
  document.getElementById("add-opt").onclick = () => addOpt()
  document.getElementById("new-form").onsubmit = async ev => {
    ev.preventDefault()
    const f = new FormData(ev.target)
    const btn = ev.target.querySelector("button[type=submit]")
    btn.disabled = true
    try {
      const poll = await api.create({
        title: f.get("title"),
        description: f.get("description"),
        options: f.getAll("option").map(String).filter(o => o.trim()),
        closes_at: Math.floor(Date.now() / 1000) + Number(f.get("duration")),
      })
      location.hash = `#/poll/${poll.id}`
    } catch (e) {
      btn.disabled = false
      notice(e.message, "bad")
    }
  }
}

function devicePage() {
  render(`${header("Voting key", true)}<div class="card stack" id="device"></div>`)
  bindCommon()
  const box = document.getElementById("device")
  if (!session.currentPubkey()) {
    box.outerHTML = signInCard()
    bindCommon()
    return
  }
  const code = session.exportCode()
  box.innerHTML = `
    <p>Your voting key lives only on this device. To vote from another device, copy this export code
    there (Voting key → Import). Anyone with the code can vote in your name, so keep it private.</p>
    ${code ? `<textarea readonly rows="3" id="code">${esc(code)}</textarea><button class="link" id="copy">Copy the code</button>` : '<p class="muted">No voting key on this device yet.</p>'}
    <label>Import a code from another device<textarea id="import" rows="3" placeholder="Paste the export code"></textarea></label>
    <button class="primary" id="do-import">Import and use it here</button>`
  document.getElementById("copy")?.addEventListener("click", () => navigator.clipboard.writeText(code).then(() => notice("Copied.", "good")))
  document.getElementById("do-import").onclick = async () => {
    try {
      const id = session.importIdentity(document.getElementById("import").value)
      const me = await api.me()
      if (me.registration?.commitment !== id.commitment.toString()) await api.register(id.commitment.toString())
      notice("This device now votes with the imported key.", "good")
      devicePage()
    } catch (e) {
      notice(`Could not import: ${e.message}`, "bad")
    }
  }
}

// -- shell ----------------------------------------------------------------------------------

function bindCommon() {
  const si = document.getElementById("signin")
  if (si) si.onclick = () => startSso(false)
  const so = document.getElementById("signout")
  if (so)
    so.onclick = async () => {
      await session.signOut()
      await endSsoSession()
      route()
    }
}

function route() {
  const hash = location.hash || "#/"
  let m
  if ((m = hash.match(/^#\/poll\/([\w-]+)$/))) return pollPage(m[1])
  if (hash === "#/new") return newPage()
  if (hash === "#/device") return devicePage()
  return listPage()
}

async function boot() {
  if (location.pathname === SSO_CALLBACK_PATH) {
    render('<p class="muted center">Signing in…</p>')
    const result = await finishSso(location.search)
    history.replaceState(null, "", `/${result.returnTo}`)
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
  foot.innerHTML = '<a href="#/device">Voting key</a> · Open Source Village, run by Belfry'
  document.body.appendChild(foot)
}

boot()
