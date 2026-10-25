// Drives the village wallet (infra/apps/wallet, patch 0007) in headless Chromium for a participant
// who holds their own Nostr key (joined with it on the invite page, or moved to it):
//
//   1. Device 1: "Use your own signer". A stand-in NIP-07 extension answers getPublicKey,
//      signEvent and nip44 with OWN_SECRET; the signing itself runs in the onboarding container
//      (belfry_onboarding.nip46check), so this script needs no Nostr library. The wallet makes a
//      seed and a nutzap key, keeps them on the community relay encrypted to the participant's
//      key (kind 30078, d=osv-wallet) and publishes kind 10019. A token link (TOKEN=cashuB…, the
//      pass's tokens) is received.
//   2. Device 2 (a fresh browser profile): the same signer finds the backup, so the wallet has the
//      same seed and restores the tokens from the mint.
//   3. With SECOND_JAR (the Authentik session of a participant with a managed key): device 2 sends
//      that participant 2 tokens as a nutzap, signed by the own signer; they redeem it and send 1
//      back, which device 2 redeems.
//
//   CHROME_BIN=~/.cache/ms-playwright/chromium-1223/chrome-linux64/chrome OWN_SECRET=<hex> \
//   [TOKEN=cashuB…] [SECOND_JAR=b.jar] node infra/scripts/wallet-own-signer-check.mjs http://localhost:3400
import {spawn} from "node:child_process"
import {mkdtempSync, readFileSync, rmSync} from "node:fs"
import {tmpdir} from "node:os"
import {join} from "node:path"
import {createInterface} from "node:readline"

const [walletUrl] = process.argv.slice(2)
const secret = process.env.OWN_SECRET || ""
if (!walletUrl || !/^[0-9a-f]{64}$/.test(secret)) {
  console.error("usage: OWN_SECRET=<hex> [TOKEN=…] [SECOND_JAR=…] wallet-own-signer-check.mjs <wallet url>")
  process.exit(2)
}
const sleep = ms => new Promise(r => setTimeout(r, ms))
const procs = []
const dirs = []

// -- the stand-in signer: a Python process in the onboarding container, one JSON line per call --
const SIGNER = `
import json, sys
from coincurve import PrivateKey
from belfry_onboarding.nip46check import conversation_key, nip44_decrypt, nip44_encrypt, sign_event
sk = bytes.fromhex(sys.stdin.readline().strip())
pub = PrivateKey(sk).public_key_xonly.format().hex()
for line in sys.stdin:
    r = json.loads(line); m, p = r["method"], r["params"]
    try:
        if m == "getPublicKey": out = pub
        elif m == "signEvent": out = sign_event(sk, p[0])
        elif m == "nip44.encrypt": out = nip44_encrypt(conversation_key(sk, bytes.fromhex(p[0])), p[1])
        elif m == "nip44.decrypt": out = nip44_decrypt(conversation_key(sk, bytes.fromhex(p[0])), p[1])
        else: raise ValueError("unknown method " + m)
        print(json.dumps({"id": r["id"], "ok": True, "value": out}), flush=True)
    except Exception as e:
        print(json.dumps({"id": r["id"], "ok": False, "value": str(e)}), flush=True)
`
const exec = (process.env.ONB_EXEC || "docker compose -f infra/docker-compose.yml --profile app exec -T onboarding").split(" ")
const signer = spawn(exec[0], [...exec.slice(1), "python", "-u", "-c", SIGNER], {stdio: ["pipe", "pipe", "inherit"]})
procs.push(signer)
signer.stdin.write(secret + "\n") // on stdin, not in the arguments (ps)
const answers = {}
let callId = 0
createInterface({input: signer.stdout}).on("line", line => {
  const r = JSON.parse(line)
  answers[r.id]?.(r)
  delete answers[r.id]
})
const signerCall = (method, params) =>
  new Promise(resolve => {
    const id = ++callId
    answers[id] = resolve
    signer.stdin.write(JSON.stringify({id, method, params}) + "\n")
  })

const SHIM = `(() => {
  const pending = {}; let n = 0
  window.__nostrResolve = (id, ok, value) => { const p = pending[id]; delete pending[id]; ok ? p.res(value) : p.rej(new Error(value)) }
  const call = (method, ...params) => new Promise((res, rej) => { const id = ++n; pending[id] = {res, rej}; window.__nostrBridge(JSON.stringify({id, method, params})) })
  window.nostr = {
    getPublicKey: () => call("getPublicKey"),
    signEvent: event => call("signEvent", event),
    nip44: { encrypt: (pk, text) => call("nip44.encrypt", pk, text), decrypt: (pk, text) => call("nip44.decrypt", pk, text) },
  }
})()`

async function browser(name, withSigner) {
  const port = 9300 + Math.floor(Math.random() * 600)
  const dir = mkdtempSync(join(tmpdir(), "belfry-wallet-"))
  dirs.push(dir)
  const proc = spawn(
    process.env.CHROME_BIN || "chromium",
    ["--headless=new", "--no-sandbox", "--hide-scrollbars", `--user-data-dir=${dir}`, `--remote-debugging-port=${port}`, "about:blank"],
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
  const send = (method, params = {}) =>
    new Promise(r => {
      const i = ++id
      pending[i] = r
      ws.send(JSON.stringify({id: i, method, params}))
    })
  const evaluate = async expression => (await send("Runtime.evaluate", {expression, returnByValue: true, awaitPromise: true})).result?.result?.value
  ws.onmessage = async e => {
    const m = JSON.parse(e.data)
    if (m.id && pending[m.id]) {
      pending[m.id](m)
      delete pending[m.id]
    }
    if (m.method === "Runtime.bindingCalled" && m.params.name === "__nostrBridge") {
      const call = JSON.parse(m.params.payload)
      const r = await signerCall(call.method, call.params)
      send("Runtime.evaluate", {expression: `window.__nostrResolve(${call.id}, ${r.ok}, ${JSON.stringify(r.value)})`})
    }
  }
  const waitFor = async (expression, ms, what) => {
    const deadline = Date.now() + ms
    while (Date.now() < deadline) {
      if (await evaluate(expression)) return
      await sleep(500)
    }
    throw new Error(`${name}: timeout waiting for ${what}; page: ${(await evaluate("document.body.innerText"))?.replace(/\s+/g, " ").slice(0, 300)}`)
  }
  await send("Page.enable")
  await send("Runtime.enable")
  await send("Network.enable")
  if (withSigner) {
    await send("Runtime.addBinding", {name: "__nostrBridge"})
    await send("Page.addScriptToEvaluateOnNewDocument", {source: SHIM})
  }
  return {send, evaluate, waitFor}
}

async function loadJar(b, jar) {
  for (const line of readFileSync(jar, "utf8").split("\n")) {
    if (!line || line.startsWith("# ")) continue
    const f = line.replace(/^#HttpOnly_/, "").split("\t")
    if (f.length < 7) continue
    await b.send("Network.setCookie", {name: f[5], value: f[6], domain: f[0].replace(/^\./, ""), path: f[2], httpOnly: line.startsWith("#HttpOnly_")})
  }
}

async function cleanup() {
  procs.forEach(p => p.kill())
  await sleep(500)
  for (const d of dirs) {
    try {
      rmSync(d, {recursive: true, force: true, maxRetries: 5, retryDelay: 200})
    } catch {
      // Chromium was still writing its profile: left in the temp directory
    }
  }
}

const READY = `document.body.innerText.includes("others can send you tokens")`
const balance = async b => {
  const m = (await b.evaluate(`document.body.innerText.replace(/\\s+/g, " ")`)).match(/([\d,.]+) tokens? Mint/)
  return m ? Number(m[1].replace(/[,.]/g, "")) : null
}
// useLocalStorage keeps a string as it is (older versions: JSON)
const mnemonic = b => b.evaluate(`(() => { const v = localStorage.getItem("cashu.mnemonic") || ""; try { return JSON.parse(v) } catch { return v } })()`)
const own = (await signerCall("getPublicKey", [])).value

async function openWithOwnSigner(name) {
  const b = await browser(name, true)
  await b.send("Page.navigate", {url: walletUrl})
  // the silent sign-in with Keycast comes back without a session first
  await b.waitFor(`!!document.querySelector('[data-testid="village-own-signer"]')`, 60000, "the own-signer button")
  await b.evaluate(`document.querySelector('[data-testid="village-own-signer"]').click()`)
  await b.waitFor(READY, 60000, "the signed-in card (backup read or made, 10019 published)")
  if (!(await b.evaluate(`document.body.innerText.includes("with your own signer")`))) throw new Error(`${name}: not signed in with the own signer`)
  return b
}

try {
  const a1 = await openWithOwnSigner("device 1")
  const seed = await mnemonic(a1)
  if (!seed || seed.split(" ").length !== 12) throw new Error("device 1: no wallet seed")
  console.log(`  ok   device 1: signed in with the own signer (${own.slice(0, 12)}…); seed and nutzap key from the encrypted backup on the relay (made on the first sign-in)`)
  let held = 0
  if (process.env.TOKEN) {
    await a1.send("Page.navigate", {url: "about:blank"})
    await a1.send("Page.navigate", {url: `${walletUrl.replace(/\/$/, "")}/#token=${process.env.TOKEN}`})
    await a1.waitFor(`[...document.querySelectorAll("button")].filter(b => b.innerText.trim() === "RECEIVE").length > 1`, 30000, "the receive dialog")
    await a1.evaluate(`[...document.querySelectorAll("button")].filter(b => b.innerText.trim() === "RECEIVE").at(-1).click()`)
    await a1.waitFor(`document.body.innerText.includes("Received")`, 30000, "the received notice")
    await sleep(1500)
    held = (await balance(a1)) ?? 0
    console.log(`  ok   device 1: the pass's tokens received (${held})`)
  }

  const a2 = await openWithOwnSigner("device 2")
  if ((await mnemonic(a2)) !== seed) throw new Error("device 2: another seed than device 1 (backup not found)")
  if (held) {
    await a2.waitFor(`document.body.innerText.includes("${held} tokens")`, 45000, `the restored balance of ${held}`)
  }
  console.log(`  ok   device 2: the backup on the relay gave the same seed${held ? `, ${held} tokens restored from the mint` : ""}`)

  if (process.env.SECOND_JAR) {
    const b = await browser("managed", false)
    await loadJar(b, process.env.SECOND_JAR)
    await b.send("Page.navigate", {url: walletUrl})
    await b.waitFor(READY, 60000, "the managed participant's signed-in card")
    const managed = await b.evaluate(`JSON.parse(localStorage.getItem("belfry.session")).pubkey`)
    const sendTo = async (from, to, amount) => {
      await from.evaluate(`document.querySelector('[data-testid="village-send"]').click()`)
      await from.waitFor(`!!document.querySelector('input[data-testid="village-recipient"], [data-testid="village-recipient"] input')`, 10000, "the send dialog")
      await from.evaluate(`(() => {
        const set = (sel, v) => { const i = document.querySelector("input" + sel + ", " + sel + " input"); Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value").set.call(i, v); i.dispatchEvent(new Event("input", {bubbles: true})) }
        set('[data-testid="village-recipient"]', ${JSON.stringify(to)})
        set('[data-testid="village-amount"]', "${amount}")
        return true })()`)
      await from.evaluate(`document.querySelector('[data-testid="village-send-confirm"]').click()`)
      await from.waitFor(`document.body.innerText.includes("Sent ${amount} token")`, 30000, "the sent notice")
    }
    await sendTo(a2, managed, 2)
    await b.waitFor(`document.body.innerText.includes("Received 2 tokens")`, 45000, "the managed participant redeeming the nutzap")
    console.log("  ok   device 2 sent 2 tokens to a managed participant (nutzap signed by the own signer); redeemed")
    await sleep(2000)
    await sendTo(b, own, 1)
    await a2.waitFor(`document.body.innerText.includes("Received 1 token")`, 45000, "device 2 redeeming the nutzap")
    console.log("  ok   the managed participant sent 1 token back to the own key; device 2 redeemed it")
  }
  await cleanup()
  process.exit(0)
} catch (e) {
  console.log("  FAIL " + e.message)
  await cleanup()
  process.exit(1)
}
