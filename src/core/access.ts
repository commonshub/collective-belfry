import { finalizeEvent, generateSecretKey, nip44, type NostrEvent } from 'nostr-tools'
import { sha256 } from '@noble/hashes/sha256'
import { bytesToHex } from '@noble/hashes/utils'
import { ACCESS, DISCOVERY } from '../config'
import { kvGet, kvSet } from './db'
import { all, type Ev } from './store'
import { currentSigner, enqueue, me, publishNow, queryOnce } from './sync'

// Who may use the village, as Nostr events (Belfry skill, section 11). No API to call:
// the read-only state comes from belfry.json, everything else is signed events on the
// groups relay. Requests are NIP-17 gift wraps to Belfry's service key.

export interface AccessInfo {
  relay: string
  service: string
  admins: string[]
  needs_admin: boolean
  synced: boolean
}

let info: AccessInfo | null = null
const listeners = new Set<() => void>()

export const accessInfo = () => info

export function onAccessInfo(l: () => void) {
  listeners.add(l)
  return () => { listeners.delete(l) }
}

/** The read-only state; cached for offline use. */
export async function loadAccessInfo(): Promise<AccessInfo | null> {
  info ??= (await kvGet<AccessInfo>('access-info')) ?? null
  try {
    const res = await fetch(DISCOVERY, { cache: 'no-store' })
    const doc = (await res.json()) as { access?: AccessInfo }
    if (doc.access) {
      info = doc.access
      await kvSet('access-info', info)
    }
  } catch { /* offline: the cached state */ }
  listeners.forEach((l) => l())
  return info
}

export const isAdmin = () => !!info?.admins.includes(me())

// ---- asking to get in -----------------------------------------------------------------

const TWO_DAYS = 2 * 24 * 3600
const past = () => Math.floor(Date.now() / 1000) - Math.floor(Math.random() * TWO_DAYS)

export const normalizeCode = (code: string) => code.trim().toUpperCase()

export interface RequestInput { note?: string; claim?: string; admin?: boolean; name?: string }

/** Send a request (kind 8537 rumor) to Belfry, gift-wrapped (NIP-59) so only Belfry reads it. */
export async function requestAccess(input: RequestInput) {
  const a = info ?? (await loadAccessInfo())
  if (!a) throw new Error("Can't reach the village. Check your connection and try again.")
  const signer = currentSigner()
  const tags: string[][] = []
  if (input.claim) tags.push(['claim', normalizeCode(input.claim)])
  if (input.admin) tags.push(['role', 'admin'])
  if (input.name?.trim()) tags.push(['name', input.name.trim().slice(0, 80)])
  const rumor = { kind: ACCESS.request, created_at: Math.floor(Date.now() / 1000), tags, content: (input.note ?? '').trim(), pubkey: signer.pubkey }
  const seal = await signer.sign({ kind: 13, created_at: past(), tags: [], content: await signer.encrypt(a.service, JSON.stringify(rumor)) })
  const oneTime = generateSecretKey()
  const wrap = finalizeEvent({
    kind: 1059,
    created_at: past(),
    tags: [['p', a.service]],
    content: nip44.v2.encrypt(JSON.stringify(seal), nip44.v2.utils.getConversationKey(oneTime, a.service)),
  }, oneTime)
  await publishNow('groups', wrap)
  await kvSet('access-request', { at: Date.now(), kind: input.claim ? 'code' : input.admin ? 'admin' : 'request' })
  return wrap.id
}

export const lastRequest = () => kvGet<{ at: number; kind: 'code' | 'admin' | 'request' }>('access-request')

// ---- the state, replayed from the events (same rules as Belfry's access.py) ------------

const tag = (e: Ev, k: string) => e.tags.find((t) => t[0] === k)?.[1]
const etags = (e: Ev) => e.tags.filter((t) => t[0] === 'e').map((t) => ({ id: t[1], marker: t[3] ?? '' }))
const nowSec = () => Math.floor(Date.now() / 1000)

export interface Grant { pubkey: string; role: string; until?: number; by: string; at: number; id: string }
export interface Invite { id: string; role: string; days?: number; until?: number; max?: number; expires?: number; by: string; at: number; uses: number }
export interface PendingRequest { wrapId: string; pubkey: string; role?: string; at: number }

export function accessState() {
  const service = info?.service ?? ''
  const list = all(me()).filter((e) => e.kind >= ACCESS.grant && e.kind <= ACCESS.receipt).sort((a, b) => a.created_at - b.created_at || (a.id < b.id ? -1 : 1))
  // admins that hold no admin grant are stewards (admins by their pass)
  const granted = new Set(list.filter((e) => e.kind === ACCESS.grant && tag(e, 'role') === 'admin').map((e) => tag(e, 'p')))
  const stewards = (info?.admins ?? []).filter((p) => !granted.has(p))
  const grants = new Map<string, Grant>()
  const revoked = new Map<string, number>()
  const invites = new Map<string, Invite>()
  const requests = new Map<string, PendingRequest>()
  const handled = new Set<string>()
  const uses: string[] = []
  const admins = (at: number) => new Set([...stewards, ...[...grants.values()].filter((g) => g.role === 'admin' && (!g.until || g.until > at)).map((g) => g.pubkey)])
  for (const e of list) {
    const author = e.pubkey
    if (e.kind === ACCESS.receipt) {
      if (author !== service) continue
      for (const { id, marker } of etags(e)) {
        if (marker === 'approval') handled.add(id)
        else if (tag(e, 'p')) requests.set(id, { wrapId: id, pubkey: tag(e, 'p')!, role: tag(e, 'role'), at: e.created_at })
      }
      continue
    }
    if (author !== service && !admins(e.created_at).has(author)) continue
    if (e.kind === ACCESS.grant) {
      const p = tag(e, 'p')
      if (!p) continue
      grants.set(p, { pubkey: p, role: (tag(e, 'role') ?? 'member').toLowerCase(), until: Number(tag(e, 'until')) || undefined, by: author, at: e.created_at, id: e.id })
      for (const { id, marker } of etags(e)) {
        if (marker === 'request') handled.add(id)
        if (marker === 'invite' && author === service) uses.push(id)
      }
    } else if (e.kind === ACCESS.revoke) {
      const p = tag(e, 'p')
      if (p) { grants.delete(p); revoked.set(p, e.created_at) }
      for (const { id } of etags(e)) { invites.delete(id); handled.add(id) }
    } else if (e.kind === ACCESS.invite) {
      const n = (k: string) => Number(tag(e, k)) || undefined
      invites.set(e.id, { id: e.id, role: tag(e, 'role') ?? 'member', days: n('days'), until: n('until'), max: n('max'), expires: n('expires'), by: author, at: e.created_at, uses: 0 })
    }
  }
  for (const id of uses) { const i = invites.get(id); if (i) i.uses++ }
  const now = nowSec()
  const members = [...grants.values()].filter((g) => !g.until || g.until > now)
  const memberSet = new Set(members.map((g) => g.pubkey))
  const pending = [...requests.values()].filter((r) => {
    const later = Math.max(grants.get(r.pubkey)?.at ?? 0, revoked.get(r.pubkey) ?? 0)
    return !handled.has(r.wrapId) && !memberSet.has(r.pubkey) && later < r.at
  })
  return { members: members.sort((a, b) => b.at - a.at), invites: [...invites.values()].sort((a, b) => b.at - a.at), pending: pending.sort((a, b) => b.at - a.at) }
}

export const myGrant = () => accessState().members.find((g) => g.pubkey === me())

// ---- admin actions (signed by the admin: they count as the admin's own acts) ----------

const ROLE = /^[a-z0-9][a-z0-9_-]{0,31}$/

export function cleanRole(role: string) {
  const r = role.trim().toLowerCase() || 'member'
  if (!ROLE.test(r)) throw new Error('A role is one word: letters, digits, - or _.')
  return r
}

export async function grant(pubkey: string, role: string, days?: number, wrapId?: string) {
  const tags = [['p', pubkey], ['role', cleanRole(role)]]
  if (days) tags.push(['until', String(nowSec() + days * 86400)])
  if (wrapId) tags.push(['e', wrapId, '', 'request'])
  await enqueue('groups', { kind: ACCESS.grant, tags })
}

export async function revoke(pubkey: string, wrapId?: string) {
  const tags = [['p', pubkey]]
  if (wrapId) tags.push(['e', wrapId, '', 'request'])
  await enqueue('groups', { kind: ACCESS.revoke, tags, content: wrapId ? 'declined' : '' })
}

const ALPHABET = 'ABCDEFGHJKLMNPQRSTUVWXYZ23456789'

export async function createInvite(role: string, days?: number, max?: number) {
  const r = crypto.getRandomValues(new Uint8Array(8))
  const raw = [...r].map((b) => ALPHABET[b % ALPHABET.length]).join('')
  const code = `OSV-${raw.slice(0, 4)}-${raw.slice(4)}`
  const tags = [['code', bytesToHex(sha256(normalizeCode(code)))], ['role', cleanRole(role)]]
  if (days) tags.push(['days', String(days)])
  if (max) tags.push(['max', String(max)])
  const localId = await enqueue('groups', { kind: ACCESS.invite, tags })
  // only the hash is published: keep the code on this device to show it again
  const codes = (await kvGet<Record<string, string>>('invite-codes')) ?? {}
  codes[localId] = code
  codes['hash:' + tags[0][1]] = code
  await kvSet('invite-codes', codes)
  return code
}

export async function inviteCode(e: Ev) {
  const codes = (await kvGet<Record<string, string>>('invite-codes')) ?? {}
  return codes['hash:' + tag(e, 'code')]
}

export async function withdrawInvite(id: string) {
  await enqueue('groups', { kind: ACCESS.revoke, tags: [['e', id, '', 'invite']] })
}

// ---- the requests' notes: in the DMs Belfry sent to this admin ------------------------

export interface RequestNote { requester: string; note: string; name?: string }

/** Unwraps this admin's gift wraps from Belfry until every wanted request has its note. */
export async function requestNotes(wanted: string[]): Promise<Map<string, RequestNote>> {
  const out = new Map<string, RequestNote>()
  const service = info?.service
  if (!service || !wanted.length) return out
  const cache = (await kvGet<Record<string, RequestNote>>('request-notes')) ?? {}
  for (const id of wanted) if (cache[id]) out.set(id, cache[id])
  if (wanted.every((id) => out.has(id))) return out
  const signer = currentSigner()
  const wraps = (await queryOnce('groups', { kinds: [1059], '#p': [me()], limit: 200 })).sort((a, b) => b.created_at - a.created_at)
  const seen = (await kvGet<string[]>('wraps-seen')) ?? []
  for (const w of wraps) {
    if (wanted.every((id) => out.has(id))) break
    if (seen.includes(w.id)) continue
    seen.push(w.id)
    try {
      const seal = JSON.parse(await signer.decrypt(w.pubkey, w.content)) as NostrEvent
      if (seal.pubkey !== service) continue
      const rumor = JSON.parse(await signer.decrypt(service, seal.content)) as NostrEvent
      const t = (k: string) => rumor.tags.find((x) => x[0] === k)?.[1]
      const req = t('request')
      if (!req || !t('requester')) continue
      cache[req] = { requester: t('requester')!, note: t('note') ?? '', name: /^(.+) \(npub1/.exec(rumor.content.split('\n')[1] ?? '')?.[1] }
      out.set(req, cache[req])
    } catch { /* not for us or not readable */ }
  }
  await kvSet('request-notes', cache)
  await kvSet('wraps-seen', seen.slice(-500))
  return out
}
