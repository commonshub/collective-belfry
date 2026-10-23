// Drives a headless Chromium through the chat app's bunker login (Coracle, /login/bunker):
// pastes a bunker:// URL, presses Continue and waits until the session for the expected pubkey
// exists. Not part of the smoke test (needs a browser); run it by hand after `make smoke`:
//
//   CHROME_BIN=~/.cache/ms-playwright/chromium-1223/chrome-linux64/chrome \
//   node infra/scripts/chat-login-check.mjs http://localhost:3300 'bunker://...' <pubkey hex> [shot.png]
//
// The bunker URL comes from the apps dashboard ("Connect to Nostr App") or from the smoke test
// (SAVE_BUNKER_URL=<file> make smoke). Exit code 0 on success; the page's console errors and a
// screenshot help otherwise.
//
// Sign in with Open Source Village (Keycast patch 0008): pass "-" as the bunker URL.
//   COOKIE_JAR=<curl jar of the participant's Authentik session> (SAVE_AK_JAR=<file> make smoke):
//     opening the app signs in silently, no click; the session for <pubkey> appears.
//   without COOKIE_JAR: the silent attempt fails and the login screen offers the button.
import {spawn} from "node:child_process"
import {readFileSync, writeFileSync} from "node:fs"

const [chatUrl, bunkerUrl, pubkey, shot] = process.argv.slice(2)
if (!chatUrl || !bunkerUrl || !pubkey) {
  console.error("usage: chat-login-check.mjs <chat url> <bunker url> <pubkey hex> [screenshot.png]")
  process.exit(2)
}
const chrome = process.env.CHROME_BIN || "chromium"
const port = 9000 + Math.floor(Math.random() * 1000)
const proc = spawn(
  chrome,
  ["--headless=new", "--no-sandbox", "--hide-scrollbars", "--window-size=1200,900", `--remote-debugging-port=${port}`, "about:blank"],
  {stdio: "ignore"},
)
const sleep = ms => new Promise(r => setTimeout(r, ms))
let targets
for (let i = 0; i < 50 && !targets; i++) {
  try {
    targets = await (await fetch(`http://127.0.0.1:${port}/json`)).json()
  } catch {
    await sleep(200)
  }
}
if (!targets) {
  console.error("chromium did not start")
  process.exit(1)
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
  if (m.method === "Runtime.consoleAPICalled" && ["error", "warning"].includes(m.params.type)) {
    logs.push(m.params.type + " " + m.params.args.map(a => a.value ?? a.description ?? "").join(" ").slice(0, 200))
  }
  if (m.method === "Runtime.exceptionThrown") {
    logs.push("exception " + (m.params.exceptionDetails.exception?.description || m.params.exceptionDetails.text).slice(0, 200))
  }
}
const send = (method, params = {}) =>
  new Promise(r => {
    const i = ++id
    pending[i] = r
    ws.send(JSON.stringify({id: i, method, params}))
  })
const evaluate = async expression => {
  const r = await send("Runtime.evaluate", {expression, returnByValue: true, awaitPromise: true})
  if (r.result?.exceptionDetails) throw new Error(r.result.exceptionDetails.exception?.description || "evaluate failed")
  return r.result?.result?.value
}
const waitFor = async (expression, timeoutMs, what) => {
  const deadline = Date.now() + timeoutMs
  while (Date.now() < deadline) {
    if (await evaluate(expression)) return true
    await sleep(500)
  }
  throw new Error(`timeout waiting for ${what}`)
}
const finish = async code => {
  if (shot) {
    const s = await send("Page.captureScreenshot", {format: "png"})
    if (s.result) writeFileSync(shot, Buffer.from(s.result.data, "base64"))
  }
  if (logs.length) console.log("browser log:\n  " + logs.slice(0, 12).join("\n  "))
  ws.close()
  proc.kill()
  process.exit(code)
}

const sessionFor = `Object.values(localStorage).some(v => typeof v === "string" && v.includes(${JSON.stringify(pubkey)}))`

const ssoCheck = async () => {
  if (process.env.COOKIE_JAR) {
    for (const line of readFileSync(process.env.COOKIE_JAR, "utf8").split("\n")) {
      if (!line || line.startsWith("# ")) continue
      const f = line.replace(/^#HttpOnly_/, "").split("\t")
      if (f.length < 7) continue
      await send("Network.setCookie", {
        name: f[5],
        value: f[6],
        domain: f[0].replace(/^\./, ""),
        path: f[2],
        httpOnly: line.startsWith("#HttpOnly_"),
      })
    }
    await send("Page.navigate", {url: chatUrl})
    await waitFor(sessionFor, 45000, "a session for the expected pubkey after the silent sign-in")
    console.log(`  ok   opened the app: signed in silently as ${pubkey.slice(0, 12)}... (no click)`)
    const where = await evaluate("location.pathname")
    console.log(where.startsWith("/oauth") ? `  FAIL still at ${where}` : `  ok   back at ${where}`)
    await sleep(1500)
    return finish(where.startsWith("/oauth") ? 1 : 0)
  }
  await send("Page.navigate", {url: chatUrl})
  await waitFor(
    `[...document.querySelectorAll("button")].some(b => b.textContent.includes("Sign in with Open Source Village"))`,
    45000,
    "the login screen with the sign-in button after the failed silent attempt",
  )
  const where = await evaluate("location.href")
  console.log(`  ok   no session: back at ${where}, the login screen offers "Sign in with Open Source Village"`)
  await evaluate(`[...document.querySelectorAll("button")].find(b => b.textContent.includes("Sign in with Open Source Village")).click()`)
  await waitFor(`location.pathname.startsWith("/api/oauth/authorize") || location.pathname.startsWith("/if/flow")`, 20000, "the signer's sign-in page")
  console.log(`  ok   the button opens the signer: ${(await evaluate("location.origin + location.pathname"))}`)
  return finish(0)
}

try {
  await send("Page.enable")
  await send("Runtime.enable")
  await send("Network.enable")
  if (bunkerUrl === "-") await ssoCheck()
  await send("Page.navigate", {url: chatUrl.replace(/\/$/, "") + "/login/bunker"})
  await waitFor(`!!document.querySelector('input[placeholder^="bunker://"]')`, 30000, "the bunker login form")
  console.log("  ok   bunker login form shown")
  await evaluate(`(() => {
    const input = document.querySelector('input[placeholder^="bunker://"]')
    const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value").set
    setter.call(input, ${JSON.stringify(bunkerUrl)})
    input.dispatchEvent(new Event("input", {bubbles: true}))
    return true
  })()`)
  await evaluate(`(() => {
    const button = [...document.querySelectorAll("button")].find(b => b.textContent.trim() === "Continue")
    button.click()
    return true
  })()`)
  console.log("  ok   bunker URL submitted")
  // welshman keeps the sessions in localStorage; the pubkey appears there once the login finished
  await waitFor(
    sessionFor,
    45000,
    "a session for the expected pubkey",
  )
  console.log(`  ok   session created for ${pubkey.slice(0, 12)}... (NIP-46 through the bunker relay)`)
  await sleep(1500)
  const loginVisible = await evaluate(`!!document.querySelector('a[href="/login"]')`)
  console.log(loginVisible ? "  note the Log In link is still visible" : "  ok   the Log In link is gone")
  await finish(0)
} catch (e) {
  console.log("  FAIL " + e.message)
  await finish(1)
}
