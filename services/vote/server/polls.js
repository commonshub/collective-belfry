// Anonymous polls: one Semaphore vote per active pass and poll.
//
// Each poll has its own group. When a poll opens, every registered identity of an active pass
// joins it; a pass that registers later joins every open poll it is not in yet. A pass is in a
// poll's group once and its entry never changes, so it has exactly one nullifier per poll even
// when the participant replaces the identity (new device): the new identity counts from the
// next poll on. Every root the group had is accepted while the poll is open, so a proof made
// before a later member joined stays valid.
import { createHash, randomBytes } from "node:crypto"
import { Group, verifyProof } from "@semaphore-protocol/core"
import { tx } from "./db.js"

export const MAX_OPTIONS = 10
const MIN_DURATION_S = 10 * 60
// a programme session a poll may be about: `31923:<pubkey>:<d>` (NIP-52)
const SESSION_COORDINATE = /^31923:[0-9a-f]{64}:[^\r\n]{1,200}$/
const MAX_DURATION_S = 31 * 24 * 3600

export class PollError extends Error {
  constructor(status, message) {
    super(message)
    this.status = status
  }
}

const now = () => Math.floor(Date.now() / 1000)

/** Scope of a poll: a field element derived from its id (the nullifier is per scope). */
export function pollScope(id) {
  const h = createHash("sha256").update(`belfry-vote:${id}`).digest("hex")
  return (BigInt(`0x${h}`) >> 8n).toString()
}

function isCommitment(value) {
  return typeof value === "string" && /^[0-9]{1,78}$/.test(value)
}

export function createPolls(db, { treeDepth = 10 } = {}) {
  const maxMembers = 2 ** treeDepth

  const getPoll = id => db.prepare("SELECT * FROM polls WHERE id = ?").get(id)
  const members = id =>
    db
      .prepare("SELECT commitment FROM poll_members WHERE poll_id = ? ORDER BY idx")
      .all(id)
      .map(r => r.commitment)

  function addMember(pollId, passId, commitment) {
    const count = db.prepare("SELECT COUNT(*) AS n FROM poll_members WHERE poll_id = ?").get(pollId).n
    if (count >= maxMembers) return false
    db.prepare("INSERT INTO poll_members (poll_id, pass_id, idx, commitment) VALUES (?, ?, ?, ?)").run(
      pollId,
      passId,
      count,
      commitment,
    )
    const root = new Group(members(pollId).map(BigInt)).root.toString()
    db.prepare("INSERT OR IGNORE INTO poll_roots (poll_id, root) VALUES (?, ?)").run(pollId, root)
    return true
  }

  function summary(poll, { withResults = false } = {}) {
    const options = JSON.parse(poll.options)
    const closed = poll.closes_at <= now()
    const out = {
      id: poll.id,
      scope: poll.scope,
      title: poll.title,
      description: poll.description,
      options,
      created_at: poll.created_at,
      closes_at: poll.closes_at,
      open: !closed,
      members: db.prepare("SELECT COUNT(*) AS n FROM poll_members WHERE poll_id = ?").get(poll.id).n,
      votes: db.prepare("SELECT COUNT(*) AS n FROM votes WHERE poll_id = ?").get(poll.id).n,
      session: poll.session || "",
      session_title: poll.session_title || "",
      // the Nostr notes about the poll (server/announce.js), once published
      nostr: { announcement: poll.announce_event || "", result: poll.result_event || "" },
      tree_depth: treeDepth,
    }
    // Results only once the poll has closed, so early votes do not steer later ones.
    if (closed && withResults) {
      const counts = options.map(() => 0)
      for (const r of db.prepare("SELECT option, COUNT(*) AS n FROM votes WHERE poll_id = ? GROUP BY option").all(poll.id)) {
        counts[r.option] = r.n
      }
      out.results = counts
    }
    return out
  }

  return {
    /** The pass's registered identity commitment, if any. */
    registration(passId) {
      return db.prepare("SELECT commitment, updated_at FROM registrations WHERE pass_id = ?").get(passId) ?? null
    },

    /** Register or replace the identity of a pass; joins the open polls it is not in yet. */
    register(passId, commitment, activePassIds) {
      if (!isCommitment(commitment)) throw new PollError(400, "commitment must be a decimal field element")
      return tx(db, () => {
        db.prepare(
          `INSERT INTO registrations (pass_id, commitment, updated_at) VALUES (?, ?, ?)
           ON CONFLICT(pass_id) DO UPDATE SET commitment = excluded.commitment, updated_at = excluded.updated_at`,
        ).run(passId, commitment, now())
        const joined = []
        if (!activePassIds.has(passId)) return joined
        const open = db
          .prepare(
            `SELECT id FROM polls WHERE closes_at > ? AND id NOT IN
               (SELECT poll_id FROM poll_members WHERE pass_id = ?)`,
          )
          .all(now(), passId)
        for (const { id } of open) if (addMember(id, passId, commitment)) joined.push(id)
        return joined
      })
    },

    /** Open a poll; every registered identity of an active pass joins it. */
    create(creatorPassId, input, activePassIds) {
      const title = String(input?.title ?? "").trim()
      const description = String(input?.description ?? "").trim()
      const options = Array.isArray(input?.options) ? input.options.map(o => String(o).trim()).filter(Boolean) : []
      const closesAt = Number(input?.closes_at)
      const session = String(input?.session ?? "").trim()
      const sessionTitle = String(input?.session_title ?? "").trim()
      if (session && !SESSION_COORDINATE.test(session)) throw new PollError(400, "session must be a kind-31923 coordinate")
      if (sessionTitle.length > 200) throw new PollError(400, "session_title is at most 200 characters")
      if (!title || title.length > 200) throw new PollError(400, "title is required (at most 200 characters)")
      if (description.length > 2000) throw new PollError(400, "description is at most 2000 characters")
      if (options.length < 2 || options.length > MAX_OPTIONS) {
        throw new PollError(400, `between 2 and ${MAX_OPTIONS} options`)
      }
      if (options.some(o => o.length > 200)) throw new PollError(400, "options are at most 200 characters")
      if (new Set(options).size !== options.length) throw new PollError(400, "options must differ")
      const duration = closesAt - now()
      if (!Number.isInteger(closesAt) || duration < MIN_DURATION_S || duration > MAX_DURATION_S) {
        throw new PollError(400, "closes_at must be between 10 minutes and 31 days from now")
      }
      const id = randomBytes(9).toString("base64url")
      return tx(db, () => {
        db.prepare(
          `INSERT INTO polls (id, scope, title, description, options, creator_pass_id, created_at, closes_at, session, session_title)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)`,
        ).run(id, pollScope(id), title, description, JSON.stringify(options), creatorPassId, now(), closesAt, session, session ? sessionTitle : "")
        const regs = db.prepare("SELECT pass_id, commitment FROM registrations ORDER BY updated_at, pass_id").all()
        for (const r of regs) if (activePassIds.has(r.pass_id)) addMember(id, r.pass_id, r.commitment)
        return summary(getPoll(id))
      })
    },

    /** The newest polls, open ones first; with `session`, only the polls about that session. */
    list(session = "") {
      const rows = session
        ? db.prepare("SELECT * FROM polls WHERE session = ? ORDER BY closes_at <= ? , created_at DESC LIMIT 200").all(session, now())
        : db.prepare("SELECT * FROM polls ORDER BY closes_at <= ? , created_at DESC LIMIT 200").all(now())
      return rows
        .map(p => summary(p, { withResults: true }))
    },

    get(id) {
      const poll = getPoll(id)
      if (!poll) throw new PollError(404, "no such poll")
      return summary(poll, { withResults: true })
    },

    /** The group's commitments in order: what a voter needs to build the Merkle proof. */
    group(id) {
      if (!getPoll(id)) throw new PollError(404, "no such poll")
      return members(id)
    },

    /** The pass's own membership in a poll (the commitment it votes with), if any. */
    membership(id, passId) {
      return db.prepare("SELECT commitment FROM poll_members WHERE poll_id = ? AND pass_id = ?").get(id, passId)?.commitment ?? null
    },

    /** Count an anonymous vote. No account, no signature: only the proof. */
    async vote(id, proof) {
      const poll = getPoll(id)
      if (!poll) throw new PollError(404, "no such poll")
      if (poll.closes_at <= now()) throw new PollError(409, "the poll has closed")
      const options = JSON.parse(poll.options)
      if (!proof || typeof proof !== "object") throw new PollError(400, "proof missing")
      if (Number(proof.merkleTreeDepth) !== treeDepth) throw new PollError(400, `proof must use tree depth ${treeDepth}`)
      if (String(proof.scope) !== poll.scope) throw new PollError(400, "proof is for another poll")
      const option = Number(proof.message)
      if (!Number.isInteger(option) || option < 0 || option >= options.length) {
        throw new PollError(400, "no such option")
      }
      const root = db.prepare("SELECT 1 FROM poll_roots WHERE poll_id = ? AND root = ?").get(id, String(proof.merkleTreeRoot))
      if (!root) throw new PollError(400, "proof is not for this poll's group")
      if (db.prepare("SELECT 1 FROM votes WHERE poll_id = ? AND nullifier = ?").get(id, String(proof.nullifier))) {
        throw new PollError(409, "already voted")
      }
      let valid = false
      try {
        valid = await verifyProof(proof)
      } catch {
        valid = false
      }
      if (!valid) throw new PollError(400, "invalid proof")
      try {
        db.prepare("INSERT INTO votes (poll_id, nullifier, option, proof, created_at) VALUES (?, ?, ?, ?, ?)").run(
          id,
          String(proof.nullifier),
          option,
          JSON.stringify(proof),
          now(),
        )
      } catch {
        throw new PollError(409, "already voted")
      }
      return summary(getPoll(id))
    },

    /** Every counted proof, for anyone to re-verify the tally (after the poll closed). */
    /** When announcing on Nostr started (set on the first call): polls opened before stay quiet. */
    announceSince() {
      db.prepare("INSERT OR IGNORE INTO meta (key, value) VALUES ('announce_since', ?)").run(String(now()))
      return Number(db.prepare("SELECT value FROM meta WHERE key = 'announce_since'").get().value)
    },

    /** Open polls not announced on Nostr yet (polls opened after `since`: older ones stay quiet). */
    toAnnounce(since) {
      return db
        .prepare("SELECT * FROM polls WHERE announce_event = '' AND closes_at > ? AND created_at >= ? ORDER BY created_at")
        .all(now(), since)
        .map(p => summary(p))
    },

    /** Closed polls that were announced and whose result is not published yet, with results. */
    toReport() {
      return db
        .prepare("SELECT * FROM polls WHERE announce_event != '' AND result_event = '' AND closes_at <= ? ORDER BY closes_at")
        .all(now())
        .map(p => ({ ...summary(p, { withResults: true }), announce_event: p.announce_event }))
    },

    markAnnounced(id, eventId) {
      db.prepare("UPDATE polls SET announce_event = ? WHERE id = ?").run(eventId, id)
    },

    markReported(id, eventId) {
      db.prepare("UPDATE polls SET result_event = ? WHERE id = ?").run(eventId, id)
    },

    proofs(id) {
      const poll = getPoll(id)
      if (!poll) throw new PollError(404, "no such poll")
      if (poll.closes_at > now()) throw new PollError(409, "proofs are published when the poll closes")
      return db.prepare("SELECT proof FROM votes WHERE poll_id = ? ORDER BY nullifier").all(id).map(r => JSON.parse(r.proof))
    },
  }
}
