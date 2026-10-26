// The polls on Nostr: a note from the polls key when a poll opens, and a reply with the result
// when it closes. Votes never become events (a signed event names its author); the result note
// pins the published proofs by their SHA-256 (`x` tag), so anyone can fetch them, recount and
// check that the count Belfry signed is the count the proofs give.
//
// Kind 1 notes (NIP-10 reply for the result, NIP-14 `subject`), tags `t` poll, `r` the poll's
// page, `a` the programme session the poll is about, if any. The polls key also keeps a kind-0
// profile ("Open Source Village polls"). The community relay takes events only from whitelisted
// keys and after NIP-42 AUTH: the onboarding service whitelists POLLS_PUBKEY.
import { createHash } from "node:crypto"
import { finalizeEvent, getPublicKey } from "nostr-tools/pure"
import { SimplePool } from "nostr-tools/pool"

const sha256 = text => createHash("sha256").update(text).digest("hex")

/** The exact body of `GET /api/polls/<id>/proofs`, whose hash the result note carries. */
export const proofsBody = proofs => JSON.stringify({ proofs })

function closesText(ts, tz) {
  return new Intl.DateTimeFormat("en-GB", { timeZone: tz, weekday: "long", day: "numeric", month: "long", hour: "2-digit", minute: "2-digit" }).format(
    new Date(ts * 1000),
  )
}

/** The unsigned note announcing an open poll. */
export function announcementTemplate(poll, { publicUrl, tz = "Europe/Brussels" }) {
  const url = `${publicUrl}/#/poll/${poll.id}`
  const lines = [
    `New poll: ${poll.title}`,
    ...(poll.session_title ? [`About the session “${poll.session_title}”.`] : []),
    "",
    ...poll.options.map(o => `• ${o}`),
    "",
    `Anonymous: one vote per pass, results when it closes on ${closesText(poll.closes_at, tz)}.`,
    `Vote: ${url}`,
  ]
  const tags = [
    ["subject", poll.title.slice(0, 200)],
    ["t", "poll"],
    ["r", url],
  ]
  if (poll.session) tags.push(["a", poll.session])
  return { kind: 1, created_at: Math.floor(Date.now() / 1000), tags, content: lines.join("\n") }
}

/** The unsigned reply with a closed poll's result, pinning its proofs. */
export function resultTemplate(poll, proofs, { publicUrl, relayUrl = "" }) {
  const url = `${publicUrl}/#/poll/${poll.id}`
  const total = poll.results.reduce((a, b) => a + b, 0)
  const order = poll.options.map((o, i) => [o, poll.results[i]]).sort((a, b) => b[1] - a[1])
  const lines = [
    `Result: ${poll.title}`,
    "",
    ...order.map(([o, n]) => `• ${o}: ${n}${total ? ` (${Math.round((n * 100) / total)}%)` : ""}`),
    "",
    `${poll.votes} of ${poll.members} participants voted.`,
    `Every vote is a published zero-knowledge proof; recount it: ${publicUrl}/api/polls/${poll.id}/proofs`,
  ]
  const tags = [
    ["e", poll.announce_event, relayUrl, "root"],
    ["subject", `Result: ${poll.title}`.slice(0, 200)],
    ["t", "poll"],
    ["r", url],
    ["x", sha256(proofsBody(proofs))],
  ]
  if (poll.session) tags.push(["a", poll.session])
  return { kind: 1, created_at: Math.floor(Date.now() / 1000), tags, content: lines.join("\n") }
}

export function profileTemplate({ publicUrl }) {
  return {
    kind: 0,
    created_at: Math.floor(Date.now() / 1000),
    tags: [],
    content: JSON.stringify({
      name: "Open Source Village polls",
      display_name: "Open Source Village polls",
      about: "Anonymous polls of Open Source Village: one vote per pass, results when a poll closes. Run by Belfry.",
      picture: `${publicUrl}/icon-192.png`,
      website: publicUrl,
    }),
  }
}

/**
 * Publishes what is due, every `intervalMs` and after a poll opens (`kick()`). `publish(event)`
 * defaults to the community relay with NIP-42 AUTH; tests pass their own.
 */
export function createAnnouncer({ polls, secretKey, relayUrl, relayHint = relayUrl, publicUrl, tz, publish, intervalMs = 30000, log = console }) {
  const base = publicUrl.replace(/\/$/, "")
  let pool = null
  const send =
    publish ||
    (async event => {
      pool ||= new SimplePool()
      // NIP-42: the relay asks for AUTH before it takes the event
      await Promise.any(pool.publish([relayUrl], event, { onauth: async template => finalizeEvent(template, secretKey), maxWait: 10000 }))
    })
  const sign = template => finalizeEvent(template, secretKey)
  let running = null
  let profileDone = false

  async function run() {
    const since = polls.announceSince()
    if (!profileDone) {
      await send(sign(profileTemplate({ publicUrl: base })))
      profileDone = true
    }
    for (const poll of polls.toAnnounce(since)) {
      const event = sign(announcementTemplate(poll, { publicUrl: base, tz }))
      await send(event)
      polls.markAnnounced(poll.id, event.id)
      log.log(`poll ${poll.id} announced on Nostr: ${event.id}`)
    }
    for (const poll of polls.toReport()) {
      const event = sign(resultTemplate(poll, polls.proofs(poll.id), { publicUrl: base, relayUrl: relayHint }))
      await send(event)
      polls.markReported(poll.id, event.id)
      log.log(`poll ${poll.id} result on Nostr: ${event.id}`)
    }
  }

  /** One round (never two at once); errors are logged and the next round retries. */
  function kick() {
    running ||= run()
      .catch(err => log.warn(`announcing polls on Nostr: ${err.message || err}`))
      .finally(() => (running = null))
    return running
  }

  let timer = null
  return {
    pubkey: getPublicKey(secretKey),
    kick,
    start() {
      kick()
      timer = setInterval(kick, intervalMs)
      timer.unref?.()
    },
    stop() {
      clearInterval(timer)
      pool?.close([relayUrl])
    },
  }
}
