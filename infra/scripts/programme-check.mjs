// Drives the programme app (services/village, programme.<domain>) in headless Chromium at phone
// width, as a participant with an active pass:
//
//   1. A (signed in silently) sees the day list: sessions of the programme key (sync-programme
//      must have published some) and participants' public events; the page never scrolls sideways.
//   2. A opens a session, answers "Going" (kind 31925) and comments (NIP-22 on the session's
//      coordinate); both show; "Your sessions" lists it.
//   3. "Share a photo from this session" opens the photos app with the session named; the shared
//      picture carries the session's `a` tag, says "At <session>" and appears on the session page.
//   4. With JAR_B: B sees A among the people going and A's comment.
//   5. A announces a session (a public kind 31923 in A's name, 19:00–20:30 village time), it shows
//      under "From participants" (with JAR_B: B sees it too, in the programme and in the calendar
//      app's "From participants" calendar), A edits its title, then cancels it (kind 5) and it is
//      gone.
//   6. Search finds the session by a word of its title; "Yours" offers all of A's sessions as one
//      calendar file; from the session page A starts a poll in the polls app, which says what it
//      is about, and the session page lists it.
//
// The tab bar sits at the bottom of the screen and the day chips stay in view when scrolling; a
// reload draws the programme kept on the device, and offline the page still opens with it.
// VOTE_URL (default http://localhost:3500) and CALENDAR_URL (default http://localhost:3200).
//
//   CHROME_BIN=~/.cache/ms-playwright/chromium-1223/chrome-linux64/chrome \
//   JAR_A=a.jar [JAR_B=b.jar] PROGRAMME_URL=http://localhost:3900 PHOTOS_URL=http://localhost:3800 \
//   node infra/scripts/programme-check.mjs [shot-prefix]
//
// Jars as for infra/scripts/village-check.mjs (SAVE_AK_JAR=<file> make smoke). KEEP=1 leaves the
// photo; the RSVP and the comment stay (they are harmless and replaceable).
import {spawn} from "node:child_process"
import {readFileSync, writeFileSync, mkdtempSync} from "node:fs"
import {tmpdir} from "node:os"
import {join} from "node:path"
import {deflateSync} from "node:zlib"

const [shots] = process.argv.slice(2)
const env = process.env
if (!env.JAR_A) (console.error("JAR_A is not set (see the header)"), process.exit(2))
const PROGRAMME = (env.PROGRAMME_URL || "http://localhost:3900").replace(/\/$/, "")
const PHOTOS = (env.PHOTOS_URL || "http://localhost:3800").replace(/\/$/, "")
const VOTE = (env.VOTE_URL || "http://localhost:3500").replace(/\/$/, "")
const CALENDAR = (env.CALENDAR_URL || "http://localhost:3200").replace(/\/$/, "")
const RUN = Math.random().toString(36).slice(2, 7)
const COMMENT = `Slides please ${RUN}`
const TITLE = `From the session ${RUN}`
const sleep = ms => new Promise(r => setTimeout(r, ms))
const procs = []

async function browser(name) {
  const port = 9300 + Math.floor(Math.random() * 600)
  const proc = spawn(
    env.CHROME_BIN || "chromium",
    ["--headless=new", "--no-sandbox", "--hide-scrollbars", "--window-size=390,844", `--remote-debugging-port=${port}`, "about:blank"],
    {stdio: "ignore"},
  )
  procs.push(proc)
  let targets
  for (let i = 0; i < 50 && !targets; i++) {
    try {
      targets = await (await fetch(`http://127.0.0.1:${port}/json`)).json()
    } catch {
      await sleep(200)
    }
  }
  const ws = new WebSocket(targets.find(t => t.type === "page").webSocketDebuggerUrl)
  await new Promise(r => (ws.onopen = r))
  let id = 0
  const pending = {}
  const errors = []
  ws.onmessage = e => {
    const m = JSON.parse(e.data)
    if (m.id && pending[m.id]) {
      pending[m.id](m)
      delete pending[m.id]
    }
    if (m.method === "Runtime.exceptionThrown") errors.push((m.params.exceptionDetails.exception?.description || "").slice(0, 200))
  }
  const send = (method, params = {}) =>
    new Promise(r => {
      const i = ++id
      pending[i] = r
      ws.send(JSON.stringify({id: i, method, params}))
    })
  const evaluate = async expression => (await send("Runtime.evaluate", {expression, returnByValue: true, awaitPromise: true})).result?.result?.value
  const text = () => evaluate(`document.body.innerText.replace(/\\s+/g, " ")`)
  const waitFor = async (expression, ms, what) => {
    const deadline = Date.now() + ms
    while (Date.now() < deadline) {
      if (await evaluate(expression)) return
      await sleep(500)
    }
    throw new Error(`${name}: timeout waiting for ${what}; at ${await evaluate("location.href")}: ${(await text())?.slice(0, 300)}; errors: ${errors.slice(-2).join(" | ")}`)
  }
  const go = async url => {
    await send("Page.navigate", {url})
    await sleep(800)
  }
  const shot = async label => {
    if (!shots) return
    const s = await send("Page.captureScreenshot", {format: "png"})
    if (s.result) writeFileSync(`${shots}-${name}-${label}.png`, Buffer.from(s.result.data, "base64"))
  }
  const fill = (sel, value) =>
    evaluate(`(() => { const i = document.querySelector(${JSON.stringify(sel)});
      const proto = i.tagName === "TEXTAREA" ? HTMLTextAreaElement.prototype : i.tagName === "SELECT" ? HTMLSelectElement.prototype : HTMLInputElement.prototype;
      Object.getOwnPropertyDescriptor(proto, "value").set.call(i, ${JSON.stringify(value)});
      i.dispatchEvent(new Event("input", {bubbles: true})); i.dispatchEvent(new Event("change", {bubbles: true})); return true })()`)
  const click = sel => evaluate(`document.querySelector(${JSON.stringify(sel)}).click()`)
  await send("Page.enable")
  await send("Runtime.enable")
  await send("Network.enable")
  await send("DOM.enable")
  await send("Emulation.setDeviceMetricsOverride", {width: 390, height: 844, deviceScaleFactor: 2, mobile: true})
  return {send, evaluate, text, waitFor, go, shot, fill, click, errors}
}

async function loadJar(b, jar) {
  for (const line of readFileSync(jar, "utf8").split("\n")) {
    if (!line || line.startsWith("# ")) continue
    const f = line.replace(/^#HttpOnly_/, "").split("\t")
    if (f.length < 7) continue
    await b.send("Network.setCookie", {name: f[5], value: f[6], domain: f[0].replace(/^\./, ""), path: f[2], httpOnly: line.startsWith("#HttpOnly_")})
  }
}

/** A small PNG (a lime square) for the photo. */
function pngFile() {
  const w = 64
  const crcTable = Array.from({length: 256}, (_, n) => {
    let c = n
    for (let k = 0; k < 8; k++) c = c & 1 ? 0xedb88320 ^ (c >>> 1) : c >>> 1
    return c >>> 0
  })
  const crc = buf => {
    let c = 0xffffffff
    for (const b of buf) c = crcTable[(c ^ b) & 0xff] ^ (c >>> 8)
    return (c ^ 0xffffffff) >>> 0
  }
  const chunk = (type, data) => {
    const len = Buffer.alloc(4)
    len.writeUInt32BE(data.length)
    const td = Buffer.concat([Buffer.from(type), data])
    const c = Buffer.alloc(4)
    c.writeUInt32BE(crc(td))
    return Buffer.concat([len, td, c])
  }
  const ihdr = Buffer.alloc(13)
  ihdr.writeUInt32BE(w, 0)
  ihdr.writeUInt32BE(w, 4)
  ihdr[8] = 8
  ihdr[9] = 2
  const raw = Buffer.concat(Array.from({length: w}, () => Buffer.concat([Buffer.from([0]), Buffer.alloc(w * 3, Buffer.from([0xd6, 0xff, 0x52]))])))
  const png = Buffer.concat([Buffer.from([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a]), chunk("IHDR", ihdr), chunk("IDAT", deflateSync(raw)), chunk("IEND", Buffer.alloc(0))])
  const file = join(mkdtempSync(join(tmpdir(), "photos-check-")), "village.png")
  writeFileSync(file, png)
  return file
}

const reload = b => b.send("Page.reload", {})
const ok = msg => console.log(`  ok   ${msg}`)
const has = s => `document.body.innerText.includes(${JSON.stringify(s)})`
const noSideScroll = async (b, where) => {
  const [sw, vw] = await b.evaluate(`[document.documentElement.scrollWidth, innerWidth]`)
  if (sw > vw + 1) throw new Error(`${where}: the page is ${sw}px wide on a ${vw}px screen`)
}

try {
  const a = await browser("A")
  await loadJar(a, env.JAR_A)

  // 1. the day list
  await a.go(`${PROGRAMME}/`)
  await a.waitFor(`!!document.querySelector("a.session")`, 60000, "the day list (silent sign-in, sessions on the relay)")
  const n = await a.evaluate(`document.querySelectorAll("a.session").length`)
  const day = await a.evaluate(`document.querySelector("h2").innerText`)
  await noSideScroll(a, "day list")
  ok(`A: ${n} sessions on ${day}, no sideways scrolling at 390px`)
  const tabsAt = await a.evaluate(`Math.round(innerHeight - document.querySelector(".tabs").getBoundingClientRect().bottom)`)
  if (tabsAt !== 0) throw new Error(`the tab bar is ${tabsAt}px above the bottom of the screen`)
  if ((await a.evaluate(`getComputedStyle(document.querySelector(".days")).position`)) !== "sticky") throw new Error("the day chips do not stay in view")
  ok("A: tab bar at the bottom, day chips stay in view")
  // the second visit draws from what the phone kept, before the relay answers
  await reload(a)
  await a.waitFor(`!!document.querySelector("a.session")`, 2000, "the day list from the phone's copy")
  ok("A: a second visit shows the programme at once (kept on the device)")
  // without a network (the venue's WiFi): the service worker opens the page, the kept copy shows
  await a.waitFor(`!!navigator.serviceWorker.controller`, 20000, "the service worker")
  await a.send("Network.emulateNetworkConditions", {offline: true, latency: 0, downloadThroughput: -1, uploadThroughput: -1})
  await reload(a)
  await a.waitFor(`!!document.querySelector("a.session") && ${has("what your phone kept")}`, 20000, "the kept programme offline")
  await a.send("Network.emulateNetworkConditions", {offline: false, latency: 0, downloadThroughput: -1, uploadThroughput: -1})
  ok("A: offline, the page opens and shows the kept programme with a note")
  await a.go(`${PROGRAMME}/`)
  await a.waitFor(`!!document.querySelector("a.session")`, 30000, "the day list online again")
  await a.shot("day")

  // 2. a session: RSVP and comment
  const title = await a.evaluate(`document.querySelector("a.session .session-title").innerText.replace(/^Now /, "")`)
  await a.click("a.session")
  await a.waitFor(`!!document.querySelector("[data-rsvp=accepted]")`, 30000, "the session page")
  const sessionUrl = await a.evaluate("location.href")
  await noSideScroll(a, "session page")
  await a.click("[data-rsvp=accepted]")
  await a.waitFor(`document.querySelector("[data-rsvp=accepted]")?.getAttribute("aria-pressed") === "true"`, 30000, "the answer saved")
  if (!(await a.evaluate(`!!document.querySelector(".people .who")`))) throw new Error("A is not among the people going")
  ok(`A: going to ${JSON.stringify(title)} (kind 31925), listed among the people going`)
  await a.fill("#text", COMMENT)
  await a.click("#comment button")
  await a.waitFor(has(COMMENT), 30000, "the comment")
  ok("A: commented on the session (NIP-22)")
  await a.shot("session")
  const ics = await a.evaluate(`document.querySelector('a[download$=".ics"]')?.href || ""`)
  if (!decodeURIComponent(ics).includes("BEGIN:VEVENT")) throw new Error("no calendar file link")
  ok("A: \"Add to my calendar\" offers an .ics file")
  await a.go(`${PROGRAMME}/#/mine`)
  await a.waitFor(has(title), 20000, "the session under Your sessions")
  ok("A: \"Your sessions\" lists it")

  // 3. a photo from the session
  await a.go(sessionUrl)
  await a.waitFor(`!!document.querySelector('a[href*="#/new?session="][href^="${PHOTOS}"]')`, 30000, "the photo link")
  await a.go(await a.evaluate(`document.querySelector('a[href*="#/new?session="][href^="${PHOTOS}"]').href`))
  await a.waitFor(`!!document.querySelector("#form") && ${has("From the session")}`, 60000, "the share form naming the session")
  const {root} = (await a.send("DOM.getDocument")).result
  const {nodeId} = (await a.send("DOM.querySelector", {nodeId: root.nodeId, selector: "#file"})).result
  await a.send("DOM.setFileInputFiles", {nodeId, files: [pngFile()]})
  await a.waitFor(`!!document.querySelector("#images img")`, 30000, "the uploaded picture")
  await a.fill("#title", TITLE)
  await a.click("#form button[type=submit]")
  await a.waitFor(`/#\\/p\\/[0-9a-f]{64}$/.test(location.hash) && ${has("At " + title)}`, 30000, "the post naming the session")
  const photoUrl = await a.evaluate("location.href")
  ok("A: shared a photo from the session; the post says where it was taken")
  await a.go(sessionUrl)
  await a.waitFor(`!!document.querySelector(".session-page .photo-tile")`, 30000, "the photo on the session page")
  ok("A: the session page shows the photo")
  await a.shot("photos")

  // 4. another participant sees the answer and the comment
  if (env.JAR_B) {
    const b = await browser("B")
    await loadJar(b, env.JAR_B)
    await b.go(sessionUrl)
    await b.waitFor(`${has(COMMENT)} && !!document.querySelector(".people .who")`, 60000, "A's answer and comment")
    ok("B: sees A going and A's comment")
  }

  // 5. announce, edit, cancel a session
  const OWN = `Announced ${RUN}`
  await a.go(`${PROGRAMME}/#/`)
  await a.waitFor(`!!document.querySelector('a[href^="#/new"]')`, 30000, "the announce button")
  await a.click('a[href^="#/new"]')
  await a.waitFor(`!!document.querySelector("#form #title")`, 30000, "the announce form")
  await noSideScroll(a, "announce form")
  await a.fill("#title", OWN)
  const ownDate = await a.evaluate(`document.querySelector("#date").value`)
  await a.fill("#start", "19:00")
  await a.fill("#end", "20:30")
  await a.fill("#location", "The garden")
  await a.fill("#description", "Bring a jumper.")
  await a.shot("announce")
  await a.click("#form button[type=submit]")
  await a.waitFor(`location.hash.startsWith("#/s/naddr1") && ${has(OWN)} && !!document.querySelector("#cancel")`, 30000, "the announced session's page")
  const ownUrl = await a.evaluate("location.href")
  const ownWhen = await a.evaluate(`document.querySelector(".when strong").innerText`)
  if (!/19:00|7:00/.test(ownWhen)) throw new Error(`the session starts at ${ownWhen}, expected 19:00 village time`)
  ok(`A: announced ${JSON.stringify(OWN)} (${ownWhen}), Edit and Cancel shown to the host`)
  if (env.JAR_B) {
    const b = await browser("B2")
    await loadJar(b, env.JAR_B)
    await b.go(ownUrl)
    await b.waitFor(`${has(OWN)} && ${has("Announced by")} && !document.querySelector("#cancel")`, 60000, "A's session for B")
    ok("B: sees A's session, announced by A, without Edit or Cancel")
    const [y, mo, d] = ownDate.split("-").map(Number)
    await b.go(`${CALENDAR}/d/${y}/${mo}/${d}`)
    await b.waitFor(has(OWN), 90000, "A's session in the calendar app")
    ok("B: the calendar app shows A's session (\"From participants\")")
  }
  await a.click('a[href^="#/edit/"]')
  await a.waitFor(`document.querySelector("#title")?.value === ${JSON.stringify(OWN)}`, 30000, "the edit form, filled in")
  await a.fill("#title", `${OWN} (moved)`)
  await a.click("#form button[type=submit]")
  await a.waitFor(`location.href === ${JSON.stringify(ownUrl)} && ${has(OWN + " (moved)")}`, 30000, "the edited session on the same link")
  ok("A: edited the title; the session keeps its link")
  await a.evaluate(`window.confirm = () => true; document.querySelector("#cancel").click()`)
  await a.waitFor(`location.hash.startsWith("#/d/") && !document.body.innerText.includes(${JSON.stringify(OWN)})`, 30000, "the session gone from the day")
  await a.go(ownUrl)
  await a.waitFor(has("not in the programme"), 30000, "the cancelled session's link")
  ok("A: cancelled it (kind 5); gone from the day and its link says so")

  // 6. search, all of "Yours" in one file, a poll about the session
  const word = title.split(/\s+/).find(w => w.length > 4) || title
  await a.go(`${PROGRAMME}/#/search?q=${encodeURIComponent(word.toLowerCase())}`)
  await a.waitFor(`!!document.querySelector("#results a.session") && ${has(title)}`, 30000, "the search result")
  ok(`A: search for ${JSON.stringify(word.toLowerCase())} finds the session`)
  await a.go(`${PROGRAMME}/#/mine`)
  await a.waitFor(`!!document.querySelector('a[download="open-source-village.ics"]')`, 30000, "Add all to my calendar")
  const all = decodeURIComponent(await a.evaluate(`document.querySelector('a[download="open-source-village.ics"]').href`))
  if (!all.includes("TRIGGER:-PT15M")) throw new Error("the calendar file has no reminder")
  ok("A: \"Yours\" offers all sessions as one calendar file, with reminders")
  const QUESTION = `More time for questions? ${RUN}`
  await a.go(sessionUrl)
  await a.waitFor(`!!document.querySelector('a[href*="#/new?session="][href^="${VOTE}"]')`, 30000, "Start a poll")
  await a.go(await a.evaluate(`document.querySelector('a[href*="#/new?session="][href^="${VOTE}"]').href`))
  await a.waitFor(`!!document.querySelector("#new-form") && ${has("About the session")}`, 60000, "the poll form naming the session")
  await a.fill('#new-form input[name="title"]', QUESTION)
  await a.evaluate(`(() => { const [x, y] = document.querySelectorAll('#new-form input[name="option"]');
    for (const [i, v] of [[x, "Yes"], [y, "No"]]) { Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value").set.call(i, v); i.dispatchEvent(new Event("input", {bubbles: true})) } })()`)
  await a.fill('#new-form select[name="duration"]', "900")
  await a.click('#new-form button[type="submit"]')
  await a.waitFor(`location.hash.startsWith("#/poll/") && ${has(QUESTION)} && ${has("About " + title)}`, 30000, "the poll naming the session")
  ok("A: started a poll from the session; the polls app says what it is about")
  await a.go(sessionUrl)
  await a.waitFor(`[...document.querySelectorAll("a.poll")].some(p => p.innerText.includes(${JSON.stringify(QUESTION)}))`, 30000, "the poll on the session page")
  ok("A: the session page lists the poll")
  await a.shot("polls")

  if (!env.KEEP) {
    await a.go(photoUrl)
    await a.waitFor(`!!document.querySelector("#delete")`, 30000, "the delete button")
    await a.evaluate(`window.confirm = () => true; document.querySelector("#delete").click()`)
    await a.waitFor(`location.hash === "#/"`, 30000, "the post deleted")
  }
  procs.forEach(p => p.kill())
  process.exit(0)
} catch (e) {
  console.log("  FAIL " + e.message)
  procs.forEach(p => p.kill())
  process.exit(1)
}
