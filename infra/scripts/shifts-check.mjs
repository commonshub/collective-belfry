// Drives the programme app's Shifts tab (services/village, programme.<domain>) in headless Chromium
// at phone width:
//
//   1. As a steward with an active pass (JAR, an Authentik session; the participant's e-mail added
//      with `python -m belfry_onboarding add-steward`): signed in silently, the tab bar gains
//      "Shifts" once the server names the role, and the tab lists the shifts (SHIFT, a title that
//      must be among them) with who signed up, without sign-up buttons.
//   2. As a volunteer, with the browser answering /api/shifts itself (no real volunteer needed):
//      two of three shifts, "Sign up" brings the third and the pass, a refusal shows the
//      onboarding service's message, and "Yours" lists the shift.
//
//   CHROME_BIN=~/.cache/ms-playwright/chromium-1223/chrome-linux64/chrome JAR=g.jar \
//   SHIFT="Programme check: kitchen" PROGRAMME_URL=http://localhost:3900 \
//   node infra/scripts/shifts-check.mjs [shot-prefix]
import {spawn} from "node:child_process"
import {readFileSync, writeFileSync} from "node:fs"

const [shots] = process.argv.slice(2)
const env = process.env
if (!env.JAR) (console.error("JAR is not set (see the header)"), process.exit(2))
const PROGRAMME = (env.PROGRAMME_URL || "http://localhost:3900").replace(/\/$/, "")
const SHIFT = env.SHIFT || ""
const sleep = ms => new Promise(r => setTimeout(r, ms))

const port = 9300 + Math.floor(Math.random() * 600)
const proc = spawn(
  env.CHROME_BIN || "chromium",
  ["--headless=new", "--no-sandbox", "--hide-scrollbars", "--window-size=390,844", `--remote-debugging-port=${port}`, `--user-data-dir=/tmp/shifts-check-${port}`, "about:blank"],
  {stdio: "ignore"},
)
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
let onPaused = null
ws.onmessage = e => {
  const m = JSON.parse(e.data)
  if (m.id && pending[m.id]) {
    pending[m.id](m)
    delete pending[m.id]
  }
  if (m.method === "Runtime.exceptionThrown") errors.push((m.params.exceptionDetails.exception?.description || "").slice(0, 200))
  if (m.method === "Fetch.requestPaused" && onPaused) onPaused(m.params)
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
  throw new Error(`timeout waiting for ${what}; at ${await evaluate("location.href")}: ${(await text())?.slice(0, 400)}; errors: ${errors.slice(-2).join(" | ")}`)
}
const shot = async label => {
  if (!shots) return
  const s = await send("Page.captureScreenshot", {format: "png"})
  if (s.result) writeFileSync(`${shots}-${label}.png`, Buffer.from(s.result.data, "base64"))
}
const ok = msg => console.log("ok  ", msg)
const has = s => `document.body.innerText.includes(${JSON.stringify(s)})`

await send("Page.enable")
await send("Runtime.enable")
await send("Network.enable")
await send("Emulation.setDeviceMetricsOverride", {width: 390, height: 844, deviceScaleFactor: 1, mobile: true})
for (const line of readFileSync(env.JAR, "utf8").split("\n")) {
  if (!line || line.startsWith("# ")) continue
  const f = line.replace(/^#HttpOnly_/, "").split("\t")
  if (f.length < 7) continue
  await send("Network.setCookie", {name: f[5], value: f[6], domain: f[0].replace(/^\./, ""), path: f[2], httpOnly: line.startsWith("#HttpOnly_")})
}

try {
  // 1. the steward: the real board, read only
  await send("Page.navigate", {url: `${PROGRAMME}/`})
  await waitFor(`!!document.querySelector("nav.tabs")`, 45000, "the signed-in programme")
  await waitFor(`!!document.querySelector('nav.tabs a[href="#/shifts"]')`, 20000, "the Shifts tab")
  ok("the tab bar offers Shifts to a steward")
  await evaluate(`location.hash = "#/shifts"`)
  await waitFor(has("Stewards see every shift"), 20000, "the steward's board")
  if (SHIFT) await waitFor(has(SHIFT), 5000, `the shift "${SHIFT}"`)
  if (await evaluate(`!!document.querySelector("button[data-shift]")`)) throw new Error("a steward got sign-up buttons")
  ok(`the board lists the shifts${SHIFT ? ` ("${SHIFT}")` : ""}, without sign-up buttons`)
  if (await evaluate(`document.documentElement.scrollWidth > innerWidth + 1`)) throw new Error("the Shifts tab scrolls sideways")
  await shot("1-steward")

  // 2. a volunteer, answered by the browser
  const shift = (i, mine, taken) => ({
    id: i, title: `Check shift ${i}`, description: "", place: "Commons Hub", capacity: 2, taken, people: mine ? ["You"] : [], mine,
    start: `2027-01-2${5 + i} 10:00:00`, end: `2027-01-2${5 + i} 12:00:00`,
  })
  let board = {role: "volunteer", count: 2, min_shifts: 3, change_hours: 48, shifts: [shift(1, true, 1), shift(2, true, 2), shift(3, false, 0)]}
  const posts = []
  onPaused = async p => {
    const respond = body =>
      send("Fetch.fulfillRequest", {
        requestId: p.requestId,
        responseCode: 200,
        responseHeaders: [{name: "Content-Type", value: "application/json"}],
        body: Buffer.from(JSON.stringify(body)).toString("base64"),
      })
    const path = new URL(p.request.url).pathname
    if (p.request.method === "GET") return respond(board)
    posts.push(path)
    if (path === "/api/shifts/3/signup") {
      board = {...board, count: 3, shifts: [shift(1, true, 1), shift(2, true, 2), shift(3, true, 1)]}
      return respond({result: "ok", count: 3, min_shifts: 3})
    }
    return respond({result: "refused", message: "This shift starts in less than 48 hours. Ask a steward to swap it."})
  }
  await send("Fetch.enable", {patterns: [{urlPattern: `${PROGRAMME}/api/shifts*`}]})
  await send("Page.reload")
  await waitFor(has("of the 3 shifts that keep your volunteer pass"), 30000, "the volunteer's board")
  ok("a volunteer sees 2 of 3 shifts")
  await evaluate(`document.querySelector('button[data-shift="3"][data-action="signup"]').click()`)
  await waitFor(has("your volunteer pass is active"), 15000, "the third shift")
  if (posts[0] !== "/api/shifts/3/signup") throw new Error(`sign-up went to ${posts[0]}`)
  ok("Sign up takes the third shift: the pass is active")
  await shot("2-volunteer")
  await evaluate(`document.querySelector('button[data-shift="1"][data-action="leave"]').click()`)
  await waitFor(has("Ask a steward to swap it"), 15000, "the refusal")
  ok("leaving too late shows the onboarding service's message")
  await evaluate(`location.hash = "#/mine"`)
  await waitFor(has("Shift: Check shift 3"), 30000, "the shift in Yours")
  ok("Yours lists the volunteer's shifts")
  await shot("3-yours")
  console.log("errors:", errors.join(" | ") || "none")
} catch (e) {
  await shot("fail")
  console.log("FAIL", e.message)
  process.exitCode = 1
} finally {
  ws.close()
  proc.kill()
}
