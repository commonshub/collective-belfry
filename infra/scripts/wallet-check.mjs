// Drives the village wallet (infra/apps/wallet) in headless Chromium, as two participants:
//
//   1. A opens the wallet with their Authentik session (COOKIE_JAR): silent sign-in, the seed and
//      the nutzap key derive from A's Nostr key, kind 10019 is published ("others can send you
//      tokens"); a token link (TOKEN=cashuB…, e.g. from the pass's tokens page) is received.
//   2. With SECOND_JAR (B's Authentik session) and SECOND_PUB (B's pubkey hex): B opens the
//      wallet too, A sends B 5 tokens with "Send to a participant" (a NIP-61 nutzap), and B's
//      wallet redeems it.
//
//   CHROME_BIN=~/.cache/ms-playwright/chromium-1223/chrome-linux64/chrome \
//   COOKIE_JAR=a.jar TOKEN=cashuB… [SECOND_JAR=b.jar SECOND_PUB=<hex>] \
//   node infra/scripts/wallet-check.mjs http://localhost:3400 [shot-prefix]
//
// The jars are curl cookie jars (SAVE_AK_JAR=<file> make smoke, twice for two participants).
import {spawn} from "node:child_process"
import {readFileSync, writeFileSync} from "node:fs"

const [walletUrl, shots] = process.argv.slice(2)
if (!walletUrl || !process.env.COOKIE_JAR) {
  console.error("usage: COOKIE_JAR=<jar> [TOKEN=…] [SECOND_JAR=… SECOND_PUB=…] wallet-check.mjs <wallet url> [shot prefix]")
  process.exit(2)
}
const sleep = ms => new Promise(r => setTimeout(r, ms))
const procs = []

async function browser(name) {
  const port = 9300 + Math.floor(Math.random() * 600)
  const proc = spawn(
    process.env.CHROME_BIN || "chromium",
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
  const logs = []
  ws.onmessage = e => {
    const m = JSON.parse(e.data)
    if (m.id && pending[m.id]) {
      pending[m.id](m)
      delete pending[m.id]
    }
    if (m.method === "Runtime.exceptionThrown") logs.push((m.params.exceptionDetails.exception?.description || "").slice(0, 200))
  }
  const send = (method, params = {}) =>
    new Promise(r => {
      const i = ++id
      pending[i] = r
      ws.send(JSON.stringify({id: i, method, params}))
    })
  const evaluate = async expression => {
    const r = await send("Runtime.evaluate", {expression, returnByValue: true, awaitPromise: true})
    return r.result?.result?.value
  }
  const waitFor = async (expression, ms, what) => {
    const deadline = Date.now() + ms
    while (Date.now() < deadline) {
      if (await evaluate(expression)) return
      await sleep(500)
    }
    throw new Error(`${name}: timeout waiting for ${what}; page: ${(await evaluate("document.body.innerText"))?.replace(/\s+/g, " ").slice(0, 300)}`)
  }
  const shot = async label => {
    if (!shots) return
    const s = await send("Page.captureScreenshot", {format: "png"})
    if (s.result) writeFileSync(`${shots}-${name}-${label}.png`, Buffer.from(s.result.data, "base64"))
  }
  await send("Page.enable")
  await send("Runtime.enable")
  await send("Network.enable")
  await send("Emulation.setDeviceMetricsOverride", {width: 390, height: 844, deviceScaleFactor: 2, mobile: true})
  return {send, evaluate, waitFor, shot, logs, ws}
}

async function loadJar(b, jar) {
  for (const line of readFileSync(jar, "utf8").split("\n")) {
    if (!line || line.startsWith("# ")) continue
    const f = line.replace(/^#HttpOnly_/, "").split("\t")
    if (f.length < 7) continue
    await b.send("Network.setCookie", {name: f[5], value: f[6], domain: f[0].replace(/^\./, ""), path: f[2], httpOnly: line.startsWith("#HttpOnly_")})
  }
}

const text = b => b.evaluate(`document.body.innerText.replace(/\\s+/g, " ")`)
const READY = `document.body.innerText.includes("others can send you tokens")`
const balance = async b => {
  const m = (await text(b)).match(/([\d,.]+) tokens? Mint/)
  return m ? Number(m[1].replace(/[,.]/g, "")) : null
}

async function openSignedIn(name, jar) {
  const b = await browser(name)
  await loadJar(b, jar)
  await b.send("Page.navigate", {url: walletUrl})
  await b.waitFor(READY, 60000, "the signed-in card (derived keys, 10019 published)")
  console.log(`  ok   ${name}: signed in silently; seed and nutzap key derived; receiving key published`)
  return b
}

try {
  const a = await openSignedIn("A", process.env.COOKIE_JAR)
  await a.shot("signed-in")
  if (process.env.TOKEN) {
    const before = (await balance(a)) ?? 0
    // a token link opens the wallet fresh (cashu.me reads the hash once, when it loads)
    await a.send("Page.navigate", {url: "about:blank"})
    await a.send("Page.navigate", {url: `${walletUrl.replace(/\/$/, "")}/#token=${process.env.TOKEN}`})
    await a.waitFor(`[...document.querySelectorAll("button")].filter(b => b.innerText.trim() === "RECEIVE").length > 1`, 30000, "the receive dialog")
    await a.evaluate(`[...document.querySelectorAll("button")].filter(b => b.innerText.trim() === "RECEIVE").at(-1).click()`)
    await a.waitFor(`document.body.innerText.includes("Received")`, 30000, "the received notice")
    await sleep(1500)
    console.log(`  ok   A: token link received (balance ${before} -> ${await balance(a)})`)
    await a.shot("received")
  }
  if (process.env.SECOND_JAR && process.env.SECOND_PUB) {
    const b = await openSignedIn("B", process.env.SECOND_JAR)
    const bBefore = (await balance(b)) ?? 0
    await a.evaluate(`document.querySelector('[data-testid="village-send"]').click()`)
    await a.waitFor(`!!document.querySelector('input[data-testid="village-recipient"], [data-testid="village-recipient"] input')`, 10000, "the send dialog")
    await a.evaluate(`(() => {
      const set = (sel, v) => { const i = document.querySelector("input" + sel + ", " + sel + " input"); Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value").set.call(i, v); i.dispatchEvent(new Event("input", {bubbles: true})) }
      set('[data-testid="village-recipient"]', ${JSON.stringify(process.env.SECOND_PUB)})
      set('[data-testid="village-amount"]', "5")
      return true })()`)
    await a.evaluate(`document.querySelector('[data-testid="village-send-confirm"]').click()`)
    await a.waitFor(`document.body.innerText.includes("Sent 5 tokens")`, 30000, "the sent notice")
    console.log("  ok   A: sent 5 tokens to B as a nutzap (kind 9321 on the community relay)")
    await b.waitFor(`document.body.innerText.includes("Received 5 tokens")`, 45000, "B redeeming the nutzap")
    await sleep(1500)
    console.log(`  ok   B: nutzap redeemed (balance ${bBefore} -> ${await balance(b)})`)
    await b.shot("nutzap")
    await sleep(7000) // notifications gone: the layout under the app bar
    await b.shot("settled")
    const spentErrors = (await text(b)).includes("Already Spent")
    if (spentErrors) throw new Error("B: an old nutzap was redeemed again (Already Spent)")
  }
  procs.forEach(p => p.kill())
  process.exit(0)
} catch (e) {
  console.log("  FAIL " + e.message)
  procs.forEach(p => p.kill())
  process.exit(1)
}
