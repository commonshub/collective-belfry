// Reporting content to the stewards, and hiding what they hid. A "Report" link opens a small form
// under itself (reason + note); the report goes to this app's server (NIP-98 signed, body hashed),
// which passes it to the onboarding service: stored in Odoo, mailed to the stewards. Only
// stewards see reports and who sent them.
import { REPORT_REASONS } from "./model.js"
import { nip98 } from "./nostr.js"
import { esc } from "./ui.js"

let hiddenPromise = null
/** What stewards hid (a Set of event ids, offer coordinates and directory pubkeys), once per page
 * load; an unreachable list hides nothing. */
export function hiddenRefs() {
  hiddenPromise ||= fetch("/api/hidden")
    .then(r => (r.ok ? r.json() : { hidden: [] }))
    .then(b => new Set(b.hidden || []))
    .catch(() => new Set())
  return hiddenPromise
}

/** A "Report" link for one piece of content: {kind, ref, pubkey, url?, excerpt?}. */
export function reportLink(target, label = "Report") {
  return `<button type="button" class="link small report" data-report="${esc(JSON.stringify(target))}">${esc(label)}</button>`
}

function form(target) {
  const what = { profile: "this entry", listing: "this offer", photo: "this photo", comment: "this comment" }[target.kind] || "this"
  return `
    <form class="card stack report-form">
      <strong>Report ${esc(what)} to the stewards</strong>
      <div class="chips">${REPORT_REASONS.map(
        ([k, n], i) => `<label class="check"><input type="radio" name="reason" value="${k}" ${i === 0 ? "checked" : ""} /> ${esc(n)}</label>`,
      ).join("")}</div>
      <label>What is wrong? (optional)<textarea name="note" rows="2" maxlength="1000"></textarea></label>
      <p class="small muted">Only the stewards see your report and that it came from you.</p>
      <div class="actions tight"><button class="primary" type="submit">Send report</button><button type="button" class="link" data-cancel>Cancel</button></div>
    </form>`
}

async function send(target, reason, note) {
  const body = JSON.stringify({
    target_kind: target.kind,
    target_ref: target.ref,
    target_pubkey: target.pubkey,
    target_url: target.url || location.href,
    target_excerpt: (target.excerpt || "").slice(0, 280),
    reason,
    note,
  })
  const res = await fetch("/api/report", {
    method: "POST",
    headers: { "Content-Type": "application/json", Authorization: await nip98("POST", "/api/report", body) },
    body,
  })
  if (!res.ok) throw new Error((await res.json().catch(() => ({}))).error || `report failed (${res.status})`)
}

/** Wire every "Report" link inside `root` (call after rendering). */
export function bindReports(root = document) {
  for (const button of root.querySelectorAll("[data-report]")) {
    button.onclick = () => {
      if (button.nextElementSibling?.classList.contains("report-form")) return
      const target = JSON.parse(button.dataset.report)
      button.insertAdjacentHTML("afterend", form(target))
      const f = button.nextElementSibling
      f.querySelector("[data-cancel]").onclick = () => f.remove()
      f.onsubmit = async ev => {
        ev.preventDefault()
        const submit = f.querySelector("button[type=submit]")
        submit.disabled = true
        try {
          await send(target, f.reason.value, f.note.value.trim())
          f.outerHTML = '<p class="notice good small report-done">Thank you. The stewards will look at it.</p>'
          button.remove()
        } catch (e) {
          submit.disabled = false
          f.insertAdjacentHTML("beforeend", `<p class="notice bad small">${esc(e.message)}</p>`)
        }
      }
    }
  }
}
