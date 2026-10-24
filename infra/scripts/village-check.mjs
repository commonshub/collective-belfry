// Drives the directory and the marketplace (services/village) and the wallet's pay requests in
// headless Chromium, as two participants with active passes and managed keys:
//
//   1. B signs in to the directory silently, opts in with a short "about", and is listed;
//      A (signed in the same way) finds B by name.
//   2. B opens the wallet once (publishes the receiving key, kind 10019), then offers something for
//      5 tokens on the marketplace, with a photo uploaded to Blossom.
//   3. A receives tokens (TOKEN_A, an unspent cashuB token), opens B's offer, presses "Pay":
//      the wallet opens with the payment filled in, sends a 5-token nutzap that names the offer,
//      and returns to the offer, which then says "You paid 5 tokens".
//   4. B sees the payment under the offer.
//   5. A's "What's new" in the directory lists B joining the directory and B's offer.
//
//   CHROME_BIN=~/.cache/ms-playwright/chromium-1223/chrome-linux64/chrome \
//   JAR_A=a.jar JAR_B=b.jar TOKEN_A=cashuB… \
//   PEOPLE_URL=http://localhost:3600 MARKET_URL=http://localhost:3700 WALLET_URL=http://localhost:3400 \
//   node infra/scripts/village-check.mjs [shot-prefix]
//
// The jars are curl cookie jars of the participants' Authentik sessions
// (SMOKE_KEEP_PASS=1 SAVE_AK_JAR=<file> make smoke, once per participant). The smoke test redeems
// the pass's own tokens, so mint fresh ones for TOKEN_A:
//   docker compose -f infra/docker-compose.yml exec -T onboarding python -c 'import os
//   from belfry_onboarding.clients.mint import MintClient
//   print(MintClient(os.environ["MINT_URL"], os.environ["MINT_PUBLIC_URL"]).mint_token(20))'
// Leaves B listed and the offer published.
import {spawn} from "node:child_process"
import {readFileSync, writeFileSync, mkdtempSync} from "node:fs"
import {tmpdir} from "node:os"
import {join} from "node:path"
import {deflateSync} from "node:zlib"

const [shots] = process.argv.slice(2)
const env = process.env
for (const v of ["JAR_A", "JAR_B", "TOKEN_A"]) if (!env[v]) (console.error(`${v} is not set (see the header)`), process.exit(2))
const PEOPLE = (env.PEOPLE_URL || "http://localhost:3600").replace(/\/$/, "")
const MARKET = (env.MARKET_URL || "http://localhost:3700").replace(/\/$/, "")
const WALLET = (env.WALLET_URL || "http://localhost:3400").replace(/\/$/, "")
const RUN = Math.random().toString(36).slice(2, 7)
const NAME_B = `Check Seller ${RUN}`
const TITLE = `Hand-made mug ${RUN}`
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

/** A small PNG (a lime square) for the listing photo. */
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
  const file = join(mkdtempSync(join(tmpdir(), "village-check-")), "mug.png")
  writeFileSync(file, png)
  return file
}

const ok = msg => console.log(`  ok   ${msg}`)

try {
  const a = await browser("A")
  const b = await browser("B")
  await loadJar(a, env.JAR_A)
  await loadJar(b, env.JAR_B)

  // 1. Directory: B opts in, A finds B.
  await b.go(`${PEOPLE}/#/me`)
  await b.waitFor(`!!document.querySelector("#entry")`, 60000, "the directory entry form (silent sign-in)")
  await b.fill("#name", NAME_B)
  await b.fill("#about", "Ceramics and Rust. Ask me about either.")
  await b.evaluate(`document.querySelector("#listed").checked = true`)
  await b.click("#entry button[type=submit]")
  await b.waitFor(`document.body.innerText.includes("You are listed in the directory")`, 30000, "the saved notice")
  ok("B: signed in silently, opted in to the directory with a profile")
  await b.shot("directory-entry")

  await a.go(`${PEOPLE}/`)
  await a.waitFor(`!!document.querySelector("#search")`, 60000, "the directory (silent sign-in)")
  await a.waitFor(`!document.body.innerText.includes("Loading")`, 30000, "the list")
  await a.fill("#search", NAME_B)
  await a.waitFor(`document.body.innerText.includes(${JSON.stringify(NAME_B)})`, 10000, "B in the directory")
  ok("A: finds B in the directory")
  await a.shot("directory")

  // 2. B's wallet publishes the receiving key; B offers something with a photo.
  await b.go(`${WALLET}/`)
  await b.waitFor(`document.body.innerText.includes("others can send you tokens")`, 60000, "B's wallet ready")
  ok("B: wallet signed in, receiving key published")

  await b.go(`${MARKET}/#/new`)
  await b.waitFor(`!!document.querySelector("#form")`, 60000, "the offer form (silent sign-in)")
  await b.fill("#title", TITLE)
  await b.fill("#summary", "Blue glaze, holds a big coffee")
  await b.fill("#description", "Made at the ceramics workshop.\nPick it up at the workshop tent.")
  await b.fill("#price", "5")
  await b.fill("#category", "stuff")
  await b.fill("#location", "Workshop tent")
  const {root} = (await b.send("DOM.getDocument")).result
  const {nodeId} = (await b.send("DOM.querySelector", {nodeId: root.nodeId, selector: "#file"})).result
  await b.send("DOM.setFileInputFiles", {nodeId, files: [pngFile()]})
  await b.waitFor(`!!document.querySelector("#images img")`, 30000, "the uploaded photo")
  const photo = await b.evaluate(`document.querySelector("#images img").src`)
  ok(`B: photo uploaded to Blossom (${photo})`)
  const blob = await fetch(photo)
  if (!blob.ok || !(blob.headers.get("content-type") || "").startsWith("image/")) throw new Error(`the photo URL answers ${blob.status} ${blob.headers.get("content-type")}`)
  await b.click("#form button[type=submit]")
  await b.waitFor(`location.hash.startsWith("#/l/naddr1") && document.body.innerText.includes(${JSON.stringify(TITLE)})`, 30000, "the published offer")
  const offerUrl = await b.evaluate("location.href")
  ok(`B: offer published (${offerUrl.split("#")[1].slice(0, 24)}…)`)
  await b.shot("offer")

  // 3. A gets their tokens, then pays the offer through the wallet.
  await a.go(`${WALLET}/`)
  await a.waitFor(`document.body.innerText.includes("others can send you tokens")`, 60000, "A's wallet ready")
  await a.go("about:blank")
  await a.go(`${WALLET}/#token=${env.TOKEN_A}`)
  await a.waitFor(`[...document.querySelectorAll("button")].filter(b => b.innerText.trim() === "RECEIVE").length > 1`, 60000, "the receive dialog")
  await a.evaluate(`[...document.querySelectorAll("button")].filter(b => b.innerText.trim() === "RECEIVE").at(-1).click()`)
  await a.waitFor(`document.body.innerText.includes("Received")`, 30000, "the received notice")
  ok("A: pass tokens received in the wallet")

  await a.go(`${MARKET}/`)
  await a.waitFor(`document.body.innerText.includes(${JSON.stringify(TITLE)})`, 60000, "B's offer in the list")
  await a.shot("market")
  await a.evaluate(`[...document.querySelectorAll("a.listing")].find(x => x.innerText.includes(${JSON.stringify(TITLE)})).click()`)
  await a.waitFor(`!!document.querySelector("#pay")`, 30000, "the Pay button")
  ok("A: sees the offer with the photo and a Pay button")
  await a.click("#pay")
  await a.waitFor(`location.host === ${JSON.stringify(new URL(WALLET).host)} && !!document.querySelector('[data-testid="village-send-confirm"]')`, 60000, "the wallet's send dialog")
  const prefilled = await a.evaluate(`(() => { const v = s => (document.querySelector("input" + s + ", " + s + " input") || {}).value; return [v('[data-testid="village-recipient"]'), v('[data-testid="village-amount"]')] })()`)
  if (!prefilled[0]?.startsWith("npub1") || prefilled[1] !== "5") throw new Error(`send dialog not filled in: ${prefilled}`)
  ok("A: the wallet opens with B and 5 tokens filled in")
  await a.shot("pay")
  await a.click('[data-testid="village-send-confirm"]')
  await a.waitFor(`location.host === ${JSON.stringify(new URL(MARKET).host)} && document.body.innerText.includes("You paid 5 tokens")`, 60000, "back at the offer, paid")
  ok("A: paid 5 tokens (nutzap naming the offer) and is back at the offer")
  await a.shot("paid")

  // 4. B sees the payment (a fresh load: the offer page is still open in B's browser).
  await b.go("about:blank")
  await b.go(offerUrl)
  await b.waitFor(`document.body.innerText.includes("5 tokens from")`, 30000, "the payment under B's offer")
  ok("B: the payment shows under the offer")
  await b.shot("payments")

  // 5. What's new.
  await a.go(`${PEOPLE}/#/activity`)
  await a.waitFor(`document.body.innerText.includes(${JSON.stringify(`offers ${TITLE} for 5 tokens`)}) && document.body.innerText.includes("joined the directory")`, 60000, "B's offer and directory entry in What's new")
  ok("A: What's new lists B joining the directory and B's offer")
  await a.shot("activity")

  const errs = [...a.errors, ...b.errors].filter(e => !/ResizeObserver/.test(e))
  if (errs.length) console.log(`  note page errors: ${errs.slice(0, 3).join(" | ")}`)
  procs.forEach(p => p.kill())
  process.exit(0)
} catch (e) {
  console.log("  FAIL " + e.message)
  procs.forEach(p => p.kill())
  process.exit(1)
}
