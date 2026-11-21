import { finalizeEvent, getPublicKey, nip04, nip44, type NostrEvent } from 'nostr-tools'
import type { BunkerPointer } from 'nostr-tools/nip46'
import type { Subscription } from 'nostr-tools/relay'
import { bytesToHex, randomBytes } from '@noble/hashes/utils'
import { sock, within, type Sock } from './conn'

// The member's signer (NIP-46, kind 24133) through the connection manager's socket to the
// bunker relay: never a socket on the side. Before a request a quiet socket is checked; a request
// that gets no answer in TIMEOUT drops the socket and is asked once more on a fresh one (signing
// and decrypting again is harmless).

const TIMEOUT = 10_000
const KIND = 24133
/** no answer, or the socket went away under the request: asked once more on a fresh socket */
const retryable = (e: unknown) => e instanceof Error && /timeout|timed out|connection closed|not connected/i.test(e.message)

export interface Remote {
  call(method: string, params: string[]): Promise<string>
  stop(): void
}

export function remoteSigner(clientSecret: Uint8Array, bp: BunkerPointer): Remote {
  const me = getPublicKey(clientSecret)
  const conv = nip44.v2.utils.getConversationKey(clientSecret, bp.pubkey)
  const s: Sock = sock(bp.relays[0])
  const waiting = new Map<string, { ok: (v: string) => void; fail: (e: Error) => void }>()
  let sub: Subscription | undefined
  let connected: Promise<void> | null = null // "connect" sent on this socket

  const listen = (r: NonNullable<Sock['relay']>) => {
    connected = null
    sub = r.subscribe([{ kinds: [KIND], authors: [bp.pubkey], '#p': [me], since: Math.floor(Date.now() / 1000) - 10 }], {
      onevent: async (e: NostrEvent) => {
        let o: { id?: string; result?: string; error?: string }
        try { o = JSON.parse(nip44.v2.decrypt(e.content, conv)) } catch {
          try { o = JSON.parse(await nip04.decrypt(clientSecret, e.pubkey, e.content)) } catch { return }
        }
        const w = o.id ? waiting.get(o.id) : undefined
        if (!w || o.result === 'auth_url') return
        waiting.delete(o.id!)
        if (o.error) w.fail(new Error(o.error))
        else w.ok(o.result ?? '')
      },
      onclose: () => { if (sub === mine) sub = undefined }, // the relay ended it: asked again before the next request
    })
    const mine = sub
  }
  const off = s.listen({ open: listen, down: () => { sub = undefined; connected = null } })
  if (s.relay?.connected) listen(s.relay)

  const ask = async (method: string, params: string[]): Promise<string> => {
    const r = await s.fresh()
    if (!sub) listen(r)
    const id = bytesToHex(randomBytes(8))
    const ev = finalizeEvent({ kind: KIND, created_at: Math.floor(Date.now() / 1000), tags: [['p', bp.pubkey]], content: nip44.v2.encrypt(JSON.stringify({ id, method, params }), conv) }, clientSecret)
    const answer = new Promise<string>((ok, fail) => waiting.set(id, { ok, fail }))
    try {
      await r.publish(ev)
      return await within(answer, TIMEOUT, 'the signer did not answer')
    } finally {
      waiting.delete(id)
    }
  }
  const once = async (method: string, params: string[]) => {
    try {
      if (method !== 'connect') await (connected ??= ask('connect', [bp.pubkey, bp.secret || '']).then(() => {}, (e) => { connected = null; throw e }))
      return await ask(method, params)
    } catch (e) {
      // no answer (or the publish timed out): the socket is dead, drop it (it reconnects)
      if (retryable(e)) s.drop(e instanceof Error ? e.message : '')
      throw e
    }
  }
  return {
    async call(method, params) {
      s.wanted = true
      try { return await once(method, params) } catch (e) {
        if (!retryable(e)) throw e
        return once(method, params) // once more on a fresh socket
      }
    },
    stop() { off(); sub?.close(); waiting.forEach((w) => w.fail(new Error('stopped'))); waiting.clear() },
  }
}
