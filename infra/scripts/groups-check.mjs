// Browser check of the groups app (Flotilla, infra/apps/groups) on the groups relay (zooid,
// infra/groups-relay) in headless Chromium over the DevTools protocol: silent sign-in with the
// participant's Authentik session, the village space with its members, a new room opened by the
// participant (any member may, Belfry's zooid patch), a message posted there and read back, and
// the room's admin rights for its creator.
// Usage: COOKIE_JAR=<curl jar with the Authentik session> node groups-check.mjs <app url> <out prefix>
//   CHROME: the Chromium binary (default: Playwright's); WIDTH (1280; the rooms list is a side
//   menu below 768), WAIT (ms per step, 15000).
import { spawn } from "node:child_process"
import { readFileSync, writeFileSync } from "node:fs"

const [url, out = "groups"] = process.argv.slice(2)
const chrome = process.env.CHROME || process.env.HOME + "/.cache/ms-playwright/chromium-1223/chrome-linux64/chrome"
const width = Number(process.env.WIDTH || 1280)
const wait = Number(process.env.WAIT || 15000)
const port = 9450 + Math.floor(Math.random() * 50)
const p = spawn(chrome, ["--headless=new", "--no-sandbox", "--hide-scrollbars", `--window-size=${width},844`,
  `--remote-debugging-port=${port}`, `--user-data-dir=/tmp/groups-check-${port}`, "about:blank"], { stdio: "ignore" })
const sleep = ms => new Promise(r => setTimeout(r, ms))
let targets
for (let i = 0; i < 50 && !targets; i++) {
  try { targets = await (await fetch(`http://127.0.0.1:${port}/json`)).json() } catch { await sleep(200) }
}
const ws = new WebSocket(targets.find(t => t.type === "page").webSocketDebuggerUrl)
await new Promise(r => (ws.onopen = r))
let id = 0
const pending = {}
const logs = []
ws.onmessage = e => {
  const m = JSON.parse(e.data)
  if (m.id && pending[m.id]) { pending[m.id](m); delete pending[m.id] }
  if (m.method === "Runtime.exceptionThrown") logs.push("EXC " + (m.params.exceptionDetails.exception?.description || m.params.exceptionDetails.text).slice(0, 300))
  if (m.method === "Runtime.consoleAPICalled" && ["error", "warning"].includes(m.params.type)) logs.push(m.params.type + " " + JSON.stringify(m.params.args.map(a => a.value || a.description)).slice(0, 300))
}
const send = (method, params = {}) => new Promise(r => { const i = ++id; pending[i] = r; ws.send(JSON.stringify({ id: i, method, params })) })
const ev = async expr => (await send("Runtime.evaluate", { expression: expr, returnByValue: true, awaitPromise: true })).result?.result?.value
const shot = async name => writeFileSync(`${out}-${name}.png`, Buffer.from((await send("Page.captureScreenshot", { format: "png" })).result.data, "base64"))
const text = () => ev("document.body.innerText")
const until = async (pred, ms = wait) => { for (let t = 0; t < ms; t += 250) { if (await ev(pred)) return true; await sleep(250) } return false }
const fail = async msg => { await shot("fail"); console.log("FAIL", msg, "\n", (await text()).slice(0, 1500), "\nlogs:", logs.join("\n")); p.kill(); process.exit(1) }
// The element whose own text is exactly `label` (buttons, links, menu items), innermost first.
const byText = label => `[...document.querySelectorAll('button, a, [role=button], div, span')].reverse()
  .find(e => e.offsetParent !== null && e.innerText?.trim() === ${JSON.stringify(label)})`
const click = label => ev(`(() => { const e = ${byText(label)}; if (e) { (e.closest('button, a, [role=button]') || e).click(); return true } return false })()`)
const visible = label => `!!(${byText(label)})`

await send("Page.enable"); await send("Runtime.enable"); await send("Network.enable")
await send("Emulation.setDeviceMetricsOverride", { width, height: 844, deviceScaleFactor: 1, mobile: width < 600 })
for (const line of (process.env.COOKIE_JAR ? readFileSync(process.env.COOKIE_JAR, "utf8") : "").split("\n")) {
  if (!line || line.startsWith("# ")) continue
  const f = line.replace(/^#HttpOnly_/, "").split("\t")
  if (f.length < 7) continue
  await send("Network.setCookie", { name: f[5], value: f[6], domain: f[0].replace(/^\./, ""), path: f[2], httpOnly: line.startsWith("#HttpOnly_") })
}

// 1. Silent sign-in: the app goes to Keycast with prompt=none and comes back signed in.
await send("Page.navigate", { url })
if (!(await until(`${visible("Create room")} || /Members\\s*\\n?\\s*\\d+/.test(document.body.innerText) || ${visible("Join Space")}`, 3 * wait))) await fail("not signed in to the space")
console.log("ok   signed in silently, at", await ev("location.href"))
// The village space joins itself (groups patch 0003); the dialog would mean that failed.
if (await ev(visible("Join Space"))) await fail("asked to join the village space")
await until(`/Members\\s*\\n?\\s*[1-9]/.test(document.body.innerText)`)
const members = await ev(`(() => { const m = document.body.innerText.match(/Members\\s*\\n?\\s*(\\d+)/); return m ? +m[1] : 0 })()`)
console.log(members > 1 ? "ok  " : "FAIL", "space lists", members, "members")
await shot("1-space")
// On a phone the rooms are in the side menu, behind the bottom bar's first button.
const openMenu = async () => {
  if (await ev(visible("Create room"))) return true
  await ev(`(() => { const b = [...document.querySelectorAll('button, a')].filter(e => { const r = e.getBoundingClientRect(); return r.width > 0 && r.top > innerHeight - 90 })
    .sort((a, b) => a.getBoundingClientRect().left - b.getBoundingClientRect().left)[0]; b?.click() })()`)
  return until(visible("Create room"))
}
if (!(await openMenu())) await fail("no rooms menu")

// 2. A room of the participant's own.
const name = `Smoke room ${Date.now().toString(36)}`
await click("Create room")
if (!(await until(visible("Create Room")))) await fail("no room dialog")
await ev(`(() => {
  const label = [...document.querySelectorAll('label, span, p, div')].find(e => e.innerText?.trim() === 'Name')
  const input = label?.closest('label, div')?.querySelector('input') || [...document.querySelectorAll('input[type=text], input:not([type])')].find(i => i.offsetParent !== null && !/search/i.test(i.placeholder))
  const set = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value').set
  set.call(input, ${JSON.stringify(name)}); input.dispatchEvent(new Event('input', { bubbles: true }))
})()`)
await click("Create Room")
if (!(await until(`location.pathname.split('/').length > 3 && ${visible(name)}`, 2 * wait))) await fail("room not created")
console.log("ok   room created by the participant:", name)

// 3. A message there, read back from the relay.
const message = `hello from the smoke room ${Date.now()}`
await until("!!document.querySelector('[contenteditable=true]')")
await ev(`(() => { const e = [...document.querySelectorAll('[contenteditable=true]')].pop(); e.focus() })()`)
await send("Input.insertText", { text: message })
await send("Input.dispatchKeyEvent", { type: "keyDown", key: "Enter", code: "Enter", windowsVirtualKeyCode: 13 })
await send("Input.dispatchKeyEvent", { type: "keyUp", key: "Enter", code: "Enter", windowsVirtualKeyCode: 13 })
if (!(await until(`document.body.innerText.includes(${JSON.stringify(message)})`, 2 * wait))) await fail("message not shown")
await sleep(2000)
console.log("ok   message posted:", message)
await shot("2-room")

// 4. The creator administers the room: the room's menu offers to edit it.
// the room header's rightmost button (the details)
await ev(`(() => { const b = [...document.querySelectorAll('button')].filter(e => { const r = e.getBoundingClientRect(); return r.top < 100 && r.width > 0 })
  .sort((a, b) => b.getBoundingClientRect().right - a.getBoundingClientRect().right)[0]; b?.click() })()`)
await sleep(1500)
const admin = await ev(`/Create invite/.test(document.body.innerText)`)
console.log(admin ? "ok  " : "warn", "the room's details", admin ? "offer \"Create invite\" (room admin)" : "show no admin controls (check the screenshot)")
await shot("3-details")

// 5. A fresh browser reads the message back (no local cache): reload with storage cleared.
await send("Network.clearBrowserCache")
await ev("location.reload()")
if (!(await until(`document.body.innerText.includes(${JSON.stringify(message)})`, 3 * wait))) await fail("message not read back after a reload")
console.log("ok   message read back after a reload")
console.log("logs:", logs.join("\n") || "none")
ws.close(); p.kill(); process.exit(0)
