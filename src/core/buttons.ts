// Buttons on messages (collective-belfry docs/actions.md): presses and answers from the store,
// sending a press, and the demo hooks. The parsing and the rules are in buttoncore.ts.
// (src/core/actions.ts is the app's write actions; this is the message buttons' part.)

import { encryptFor, encryptGroupEvent, isPrivateGroup } from './enc/runtime'
import { finalizeEvent, getEventHash, getPublicKey, type NostrEvent } from 'nostr-tools'
import { sha256 } from '@noble/hashes/sha256'
import { bytesToHex, hexToBytes } from '@noble/hashes/utils'
import { addEvents, all, type Ev } from './store'
import { currentSigner, isLocalOnly, me, publishNow } from './sync'
import { belfryGroupsKey, belfryHandlerKeys } from './access'
import { DEMO_GROUPS, knownGroupsKey } from './dm'
import { buttonState, isBelfryAction, parseButtons, PRESS_NS, pressTemplate, RESULT_NS, type AnswerInfo, type Button, type ButtonState, type PressInfo } from './buttoncore'

export * from './buttoncore'

const tag = (e: NostrEvent, k: string) => e.tags.find((t) => t[0] === k)?.[1]
const hasL = (e: NostrEvent, ns: string) => e.tags.some((t) => t[0] === 'L' && t[1] === ns)

/** The buttons a message carries (kinds 9 and 1111 only). */
export function buttonsOf(e: NostrEvent): Button[] {
  if (e.kind !== 9 && e.kind !== 1111) return []
  if (!e.tags.some((t) => t[0] === 'button')) return []
  return parseButtons(e.tags)
}

// message id → its presses and answers, rebuilt when the store changes
let cacheList: Ev[] | undefined
let index = new Map<string, { presses: PressInfo[]; answers: AnswerInfo[] }>()
function byMessage(): typeof index {
  const list = all(me())
  if (list === cacheList) return index
  cacheList = list
  index = new Map()
  const slot = (id: string) => {
    let s = index.get(id)
    if (!s) index.set(id, (s = { presses: [], answers: [] }))
    return s
  }
  for (const e of list) {
    if (e.kind !== 1985) continue
    const m = tag(e, 'e')
    if (!m) continue
    if (hasL(e, PRESS_NS)) {
      const button = e.tags.find((t) => t[0] === 'l' && t[2] === PRESS_NS)?.[1]
      if (button) slot(m).presses.push({ id: e.id, pubkey: e.pubkey, button, created_at: e.created_at })
    } else if (hasL(e, RESULT_NS)) {
      const l = e.tags.find((t) => t[0] === 'l' && t[2] === RESULT_NS)?.[1] ?? ''
      slot(m).answers.push({
        pubkey: e.pubkey, l, press: tag(e, 'press') ?? '', content: e.content ?? '', code: tag(e, 'code'),
        closed: tag(e, 'closed') === '1', created_at: e.created_at,
      })
    }
  }
  return index
}

/** The keys that answer for the belfry: belfry.json `nostr` and `groups_key` (and, before those
 *  are known, the groups key the app already knows). */
export function belfryKeys(): string[] {
  const keys = new Set(belfryHandlerKeys())
  const gk = knownGroupsKey()
  if (gk) keys.add(gk)
  if (isLocalOnly()) keys.add(getPublicKey(hexToBytes(DEMO_GROUPS)))
  return [...keys]
}

export interface MessageButtons extends ButtonState {
  /** my latest press on this message (any button), if any */
  press: PressInfo | null
}

export function messageButtons(e: NostrEvent, buttons: Button[]): MessageButtons {
  const slot = byMessage().get(e.id) ?? { presses: [], answers: [] }
  const mine = slot.presses.filter((p) => p.pubkey === me())
  const press = mine.reduce<PressInfo | null>((m, p) => (!m || p.created_at >= m.created_at ? p : m), null)
  const st = buttonState({ message: { id: e.id, pubkey: e.pubkey, buttons }, me: me(), press, presses: slot.presses, answers: slot.answers, belfryKeys: belfryKeys() })
  return { ...st, press }
}

/** Send a press at once (not through the outbox): it means something only now. Throws when it
 *  can't be sent. */
export async function press(e: NostrEvent, group: string, button: Button): Promise<NostrEvent> {
  if (isLocalOnly() && demoMode.status === 'fail-send') throw new Error('no connection to the relay')
  const t = pressTemplate({ id: e.id, pubkey: e.pubkey, group }, button.id)
  const tpl = { ...t, created_at: Math.floor(Date.now() / 1000) }
  // a private group (docs/encrypted.md §24a): the press is encrypted into the group like any group
  // event and, for a belfry: action, a copy goes to the groups key as a people conversation
  if (!isLocalOnly() && isPrivateGroup(group)) {
    const w = await encryptGroupEvent(tpl)
    if (w) {
      for (const b of w.before) await publishNow('groups', await currentSigner().sign(b))
      w.beforeOut?.()
      await publishNow('groups', await currentSigner().sign(w.event))
      const rumor = { ...tpl, pubkey: me(), id: getEventHash({ ...tpl, pubkey: me() } as NostrEvent), sig: '' } as NostrEvent
      if (isBelfryAction(button.action)) await pressToGroupsKey(rumor, e, button)
      addEvents([rumor]) // shown at once; its 8572 opens to the same rumor
      return rumor
    }
  }
  const ev = await currentSigner().sign(tpl)
  if (!isLocalOnly()) await publishNow('groups', ev)
  addEvents([ev])
  if (isLocalOnly()) demoAnswer(e, group, button, ev)
  return ev
}

/** §24a: the copy of a belfry: press to the groups key (a service key, its own device), as a people
 *  conversation: the press's tags without the author's p, ["p", groups key], ["e", press id, "", "press"],
 *  the button as on the message, ["k", its kind]; content = the message as JSON. */
async function pressToGroupsKey(press: NostrEvent, e: NostrEvent, button: Button) {
  const gk = belfryGroupsKey() || knownGroupsKey() // belfry.json's groups_key first
  if (!gk) throw new Error('the groups key is not known')
  const bt = e.tags.find((t) => t[0] === 'button' && t[1] === button.id) ?? ['button', button.id]
  const r = { kind: press.kind, pubkey: me(), created_at: press.created_at, content: JSON.stringify({ id: e.id, pubkey: e.pubkey, created_at: e.created_at, kind: e.kind, tags: e.tags, content: e.content }),
    tags: [...press.tags.filter((t) => t[0] !== 'p'), ['p', gk], ['e', press.id, '', 'press'], bt, ['k', String(e.kind)]] }
  const out = await encryptFor({ ...r, id: getEventHash(r as NostrEvent) }, [gk])
  if (!out) throw new Error('not encrypted')
  for (const ev of out.events) await publishNow('main', ev)
}

// ---- demo (tests): window.demoButtons ------------------------------------------------------------
const hex = (n: number) => bytesToHex(crypto.getRandomValues(new Uint8Array(n)))
const AUTHOR_SK = bytesToHex(sha256('osv-demo-button-author'))
type DemoMode = { status: 'ok' | 'error' | 'none' | 'fail-send'; code?: string; text?: string; closed?: boolean; delay?: number }
let demoMode: DemoMode = { status: 'ok', text: 'Published by Demo.', closed: true, delay: 600 }

function demoAnswer(e: NostrEvent, group: string, button: Button, p: NostrEvent) {
  const mode = demoMode
  if (mode.status === 'none') return
  // the belfry answers its own actions, the demo author answers the others
  const sk = isBelfryAction(button.action) ? hexToBytes(DEMO_GROUPS) : hexToBytes(AUTHOR_SK)
  setTimeout(() => {
    addEvents([finalizeEvent({
      kind: 1985, created_at: Math.floor(Date.now() / 1000), content: mode.text ?? '',
      tags: [['L', RESULT_NS], ['l', mode.status, RESULT_NS], ['e', e.id], ['press', p.id], ['p', p.pubkey], ['h', group],
        ...(mode.status === 'error' ? [['code', mode.code ?? 'failed']] : mode.closed ? [['closed', '1']] : [])],
    }, sk)])
  }, mode.delay ?? 600)
}

export const demoButtons = {
  /** Post a message with BUTTONS (button tags) in CHANNEL; by the demo author unless MINE. */
  post(channel: string, text: string, buttons: string[][], opts: { kind?: number; root?: string; mine?: boolean } = {}): string {
    const tags = [['h', channel], ...buttons]
    if (opts.kind === 1111 && opts.root) tags.push(['E', opts.root, '', ''], ['e', opts.root, '', ''], ['K', '9'], ['k', '9'])
    const t = { kind: opts.kind ?? 9, created_at: Math.floor(Date.now() / 1000), content: text, tags }
    const ev = finalizeEvent(t, hexToBytes(AUTHOR_SK))
    addEvents([ev])
    return ev.id
  },
  /** How the next presses are answered. */
  answer(m: DemoMode) { demoMode = { delay: 600, ...m } },
  /** Someone else's press answered ok + closed (closes the buttons for everyone). */
  closeBySomeone(message: string, group: string, text: string) {
    addEvents([finalizeEvent({
      kind: 1985, created_at: Math.floor(Date.now() / 1000), content: text,
      tags: [['L', RESULT_NS], ['l', 'ok', RESULT_NS], ['e', message], ['press', hex(32)], ['p', hex(32)], ['h', group], ['closed', '1']],
    }, hexToBytes(DEMO_GROUPS))])
  },
  /** A forged answer (signed by the demo author for a belfry: button): must be ignored. */
  forge(message: string, group: string, pressId: string, text: string) {
    addEvents([finalizeEvent({
      kind: 1985, created_at: Math.floor(Date.now() / 1000), content: text,
      tags: [['L', RESULT_NS], ['l', 'ok', RESULT_NS], ['e', message], ['press', pressId], ['p', me()], ['h', group], ['closed', '1']],
    }, hexToBytes(AUTHOR_SK))])
  },
  presses: (message: string) => all(me()).filter((e) => e.kind === 1985 && hasL(e, PRESS_NS) && tag(e, 'e') === message),
}
