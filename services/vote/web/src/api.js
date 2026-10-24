// The vote API. Requests that need a participant carry a NIP-98 token signed through Keycast;
// votes carry nothing but the proof.
import { sign } from "./session.js"

async function sha256Hex(text) {
  const digest = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(text))
  return [...new Uint8Array(digest)].map(b => b.toString(16).padStart(2, "0")).join("")
}

async function call(method, path, { body, auth = false } = {}) {
  const raw = body === undefined ? "" : JSON.stringify(body)
  const headers = { "Content-Type": "application/json" }
  if (auth) {
    const tags = [
      ["u", `${location.origin}${path}`],
      ["method", method],
    ]
    if (raw) tags.push(["payload", await sha256Hex(raw)])
    const event = await sign({ kind: 27235, created_at: Math.floor(Date.now() / 1000), tags, content: "" })
    headers.Authorization = `Nostr ${btoa(JSON.stringify(event))}`
  }
  const res = await fetch(path, { method, headers, body: raw || undefined })
  const json = await res.json().catch(() => ({}))
  if (!res.ok) {
    const err = new Error(json.error || `request failed (${res.status})`)
    err.status = res.status
    throw err
  }
  return json
}

export const api = {
  polls: () => call("GET", "/api/polls"),
  poll: id => call("GET", `/api/polls/${id}`),
  group: id => call("GET", `/api/polls/${id}/group`),
  myMembership: id => call("GET", `/api/polls/${id}/me`, { auth: true }),
  me: () => call("GET", "/api/me", { auth: true }),
  register: commitment => call("PUT", "/api/me/identity", { body: { commitment }, auth: true }),
  create: poll => call("POST", "/api/polls", { body: poll, auth: true }),
  vote: (id, proof) => call("POST", `/api/polls/${id}/votes`, { body: { proof } }),
}
