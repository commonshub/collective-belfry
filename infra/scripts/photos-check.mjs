// Drives the photos app (services/village, photos.<domain>) and the wallet's pay requests in
// headless Chromium, as two participants with active passes and managed keys:
//
//   1. B opens the wallet once (publishes the receiving key, kind 10019), then shares a picture on
//      photos: uploaded to Blossom, with alt text, title, caption and hashtags (NIP-68, kind 20).
//   2. A (signed in silently) finds it in the feed and under its hashtag, likes it and comments.
//   3. A receives tokens (TOKEN_A, an unspent cashuB token) and tips 1 token: the wallet opens
//      with the tip filled in, sends a nutzap naming the picture ("e" tag, wallet patch 0004) and
//      returns to the picture, which then says "You tipped 1 token".
//   4. B sees the like, the comment and the tip, then deletes the post; A's feed no longer has it.
//
//   CHROME_BIN=~/.cache/ms-playwright/chromium-1223/chrome-linux64/chrome \
//   JAR_A=a.jar JAR_B=b.jar TOKEN_A=cashuB… \
//   PHOTOS_URL=http://localhost:3800 WALLET_URL=http://localhost:3400 \
//   node infra/scripts/photos-check.mjs [shot-prefix]
//
// Jars and TOKEN_A as for infra/scripts/village-check.mjs (see its header). KEEP=1 leaves the post.
import {spawn} from "node:child_process"
import {readFileSync, writeFileSync, mkdtempSync} from "node:fs"
import {tmpdir} from "node:os"
import {join} from "node:path"
import {deflateSync} from "node:zlib"


const [shots] = process.argv.slice(2)
const env = process.env
for (const v of ["JAR_A", "JAR_B", "TOKEN_A"]) if (!env[v]) (console.error(`${v} is not set (see the header)`), process.exit(2))
const PHOTOS = (env.PHOTOS_URL || "http://localhost:3800").replace(/\/$/, "")
const WALLET = (env.WALLET_URL || "http://localhost:3400").replace(/\/$/, "")
const RUN = Math.random().toString(36).slice(2, 7)
const TITLE = `Village at dusk ${RUN}`
const TAG = `check${RUN}`
const COMMENT = `What a view ${RUN}`
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

const ok = msg => console.log(`  ok   ${msg}`)
const has = s => `document.body.innerText.includes(${JSON.stringify(s)})`

try {
  const a = await browser("A")
  const b = await browser("B")
  await loadJar(a, env.JAR_A)
  await loadJar(b, env.JAR_B)

  // 1. B's wallet publishes the receiving key; B shares a picture.
  await b.go(`${WALLET}/`)
  await b.waitFor(has("others can send you tokens"), 60000, "B's wallet ready")
  ok("B: wallet signed in, receiving key published")

  await b.go(`${PHOTOS}/#/new`)
  await b.waitFor(`!!document.querySelector("#form")`, 60000, "the share form (silent sign-in)")
  const {root} = (await b.send("DOM.getDocument")).result
  const {nodeId} = (await b.send("DOM.querySelector", {nodeId: root.nodeId, selector: "#file"})).result
  await b.send("DOM.setFileInputFiles", {nodeId, files: [pngFile()]})
  await b.waitFor(`!!document.querySelector("#images img")`, 30000, "the uploaded picture")
  const photo = await b.evaluate(`document.querySelector("#images img").src`)
  const blob = await fetch(photo)
  if (!blob.ok || !(blob.headers.get("content-type") || "").startsWith("image/")) throw new Error(`the picture URL answers ${blob.status} ${blob.headers.get("content-type")}`)
  ok(`B: picture shrunk and uploaded to Blossom (${photo})`)
  await b.fill("[data-alt='0']", "Tents and string lights at dusk")
  await b.fill("#title", TITLE)
  await b.fill("#caption", "From the hill behind the workshop tent.")
  await b.fill("#hashtags", `#${TAG} #village`)
  await b.fill("#location", "The hill")
  await b.shot("share")
  await b.click("#form button[type=submit]")
  await b.waitFor(`/^#\\/p\\/[0-9a-f]{64}$/.test(location.hash) && ${has(TITLE)}`, 30000, "the published picture")
  const postUrl = await b.evaluate("location.href")
  const alt = await b.evaluate(`document.querySelector(".photo-full img").alt`)
  if (alt !== "Tents and string lights at dusk") throw new Error(`alt text not kept: ${alt}`)
  ok(`B: shared (${postUrl.split("#")[1].slice(0, 16)}…) with alt text, hashtags and place`)
  await b.shot("post")

  // 2. A finds it in the feed and under the hashtag, likes and comments.
  await a.go(`${PHOTOS}/`)
  await a.waitFor(`[...document.querySelectorAll(".photo-tile")].some(t => t.title === ${JSON.stringify(TITLE)})`, 60000, "B's picture in the feed (silent sign-in)")
  ok("A: signed in silently, sees B's picture in the feed")
  await a.shot("feed")
  await a.go(`${PHOTOS}/#/t/${TAG}`)
  await a.waitFor(`document.querySelectorAll(".photo-tile").length === 1`, 30000, "the hashtag feed")
  ok(`A: the hashtag #${TAG} shows exactly that picture`)
  await a.click(".photo-tile")
  await a.waitFor(`!!document.querySelector("#like")`, 30000, "the picture page")
  await a.click("#like")
  await a.waitFor(`document.querySelector("#like")?.innerText === "Liked" && ${has("1 like")}`, 30000, "the like")
  await a.fill("#text", COMMENT)
  await a.click("#comment button")
  await a.waitFor(`document.querySelector("#comments")?.innerText.includes(${JSON.stringify(COMMENT)})`, 30000, "the comment")
  ok("A: liked it and commented")

  // 3. A gets tokens and tips 1 token through the wallet.
  await a.go(`${WALLET}/`)
  await a.waitFor(has("others can send you tokens"), 60000, "A's wallet ready")
  await a.go("about:blank")
  await a.go(`${WALLET}/#token=${env.TOKEN_A}`)
  await a.waitFor(`[...document.querySelectorAll("button")].filter(b => b.innerText.trim() === "RECEIVE").length > 1`, 60000, "the receive dialog")
  await a.evaluate(`[...document.querySelectorAll("button")].filter(b => b.innerText.trim() === "RECEIVE").at(-1).click()`)
  await a.waitFor(has("Received"), 30000, "the received notice")
  ok("A: tokens received in the wallet")
  await a.go(postUrl)
  await a.waitFor(`[...document.querySelectorAll("a.button")].some(x => x.innerText.trim() === "1 token")`, 60000, "the tip buttons")
  await a.shot("photo")
  await a.evaluate(`[...document.querySelectorAll("a.button")].find(x => x.innerText.trim() === "1 token").click()`)
  await a.waitFor(`location.host === ${JSON.stringify(new URL(WALLET).host)} && !!document.querySelector('[data-testid="village-send-confirm"]')`, 60000, "the wallet's send dialog")
  const prefilled = await a.evaluate(`(() => { const v = s => (document.querySelector("input" + s + ", " + s + " input") || {}).value; return [v('[data-testid="village-recipient"]'), v('[data-testid="village-amount"]')] })()`)
  if (!prefilled[0]?.startsWith("npub1") || prefilled[1] !== "1") throw new Error(`send dialog not filled in: ${prefilled}`)
  ok("A: the wallet opens with B and 1 token filled in")
  await a.click('[data-testid="village-send-confirm"]')
  await a.waitFor(`location.host === ${JSON.stringify(new URL(PHOTOS).host)} && ${has("You tipped 1 token")}`, 60000, "back at the picture, tipped")
  ok("A: tipped 1 token (nutzap naming the picture) and is back at the picture")
  await a.shot("tipped")

  // 4. B sees like, comment and tip; then deletes the post.
  await b.go("about:blank")
  await b.go(postUrl)
  await b.waitFor(`${has("1 like")} && ${has(COMMENT)} && ${has("1 token from")}`, 30000, "the like, the comment and the tip on B's picture")
  ok("B: sees the like, the comment and the tip")
  await b.shot("reactions")
  if (!env.KEEP) {
    await b.evaluate("window.confirm = () => true")
    await b.click("#delete")
    await b.waitFor(`location.hash === "#/"`, 30000, "back at the feed after deleting")
    await a.go("about:blank")
    await a.go(`${PHOTOS}/#/t/${TAG}`)
    await a.waitFor(has("No photos yet"), 30000, "the deleted picture gone from the hashtag feed")
    ok("B: deleted the post; A no longer sees it")
  }

  const errs = [...a.errors, ...b.errors].filter(e => !/ResizeObserver/.test(e))
  if (errs.length) console.log(`  note page errors: ${errs.slice(0, 3).join(" | ")}`)
  procs.forEach(p => p.kill())
  process.exit(0)
} catch (e) {
  console.log("  FAIL " + e.message)
  procs.forEach(p => p.kill())
  process.exit(1)
}
