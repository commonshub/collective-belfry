// Browser check of the polls app (services/vote) in headless Chromium over the DevTools protocol:
// silent sign-in with the participant's Authentik session, set up voting on this device, open a
// poll, vote anonymously, and see the vote counted.
// Usage: COOKIE_JAR=<curl jar with the Authentik session> node vote-check.mjs <app url> <out prefix>
//   CHROME: the Chromium binary (default: Playwright's); WIDTH (390), WAIT (ms per step, 12000).
import { spawn } from "node:child_process"
import { readFileSync, writeFileSync } from "node:fs"

const [url, out = "vote"] = process.argv.slice(2)
const chrome = process.env.CHROME || process.env.HOME + "/.cache/ms-playwright/chromium-1223/chrome-linux64/chrome"
const width = Number(process.env.WIDTH || 390)
const wait = Number(process.env.WAIT || 12000)
const port = 9400 + Math.floor(Math.random() * 50)
const p = spawn(chrome, ["--headless=new", "--no-sandbox", "--hide-scrollbars", `--window-size=${width},844`,
  `--remote-debugging-port=${port}`, `--user-data-dir=/tmp/vote-check-${port}`, "about:blank"], { stdio: "ignore" })
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
const text = () => ev("document.querySelector('#app')?.innerText || ''")
const until = async (pred, ms = wait) => { for (let t = 0; t < ms; t += 250) { if (await ev(pred)) return true; await sleep(250) } return false }
const fail = async msg => { await shot("fail"); console.log("FAIL", msg, "\n", await text(), "\nlogs:", logs.join("\n")); p.kill(); process.exit(1) }

await send("Page.enable"); await send("Runtime.enable"); await send("Network.enable")
await send("Emulation.setDeviceMetricsOverride", { width, height: 844, deviceScaleFactor: 1, mobile: width < 600 })
for (const line of readFileSync(process.env.COOKIE_JAR, "utf8").split("\n")) {
  if (!line || line.startsWith("# ")) continue
  const f = line.replace(/^#HttpOnly_/, "").split("\t")
  if (f.length < 7) continue
  await send("Network.setCookie", { name: f[5], value: f[6], domain: f[0].replace(/^\./, ""), path: f[2], httpOnly: line.startsWith("#HttpOnly_") })
}

await send("Page.navigate", { url })
if (!(await until("!!document.getElementById('signout')", 2 * wait))) await fail("not signed in silently")
console.log("ok   signed in silently at", await ev("location.href"))
if (await until("!!document.getElementById('register')", 4000)) {
  await ev("document.getElementById('register').click()")
  if (!(await until("/Ready/.test(document.getElementById('notice')?.textContent || '')"))) await fail("voting set-up did not finish")
  console.log("ok   voting set up on this device:", await ev("document.getElementById('notice').textContent"))
} else console.log("ok   voting was already set up")
await shot("1-list")

await ev("location.hash = '#/new'")
await until("!!document.getElementById('new-form')")
const title = `Smoke poll ${Date.now()}`
await ev(`(() => { const f = document.getElementById('new-form'); f.elements.title.value = ${JSON.stringify(title)};
  const o = f.querySelectorAll('input[name=option]'); o[0].value = 'Yes'; o[1].value = 'No'; f.requestSubmit(); })()`)
if (!(await until("/^#\\/poll\\//.test(location.hash) && !!document.getElementById('vote-form')"))) await fail("poll not created or no vote form")
console.log("ok   poll opened:", await ev("location.hash"))
await shot("2-poll")

await ev("document.querySelector('#vote-form input[value=\"0\"]').click(); document.getElementById('vote-form').requestSubmit()")
if (!(await until("/You voted/.test(document.querySelector('#app').innerText)", 3 * wait))) await fail("vote not counted")
console.log("ok   voted anonymously:", (await text()).split("\n").find(l => /voted/.test(l)))
await shot("3-voted")
console.log("logs:", logs.join("\n") || "none")
ws.close(); p.kill(); process.exit(0)
