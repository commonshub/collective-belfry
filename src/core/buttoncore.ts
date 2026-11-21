// Buttons on messages (collective-belfry docs/actions.md): the pure part, no imports, so the
// shared vectors (docs/actions-vectors.json) run against it in node.
//
// A message (kind 9 / 1111) carries up to 4 ["button", id, label, style?, action?] tags. A press
// is a kind 1985 label `belfry/action` by whoever tapped; the handler answers with a kind 1985
// label `belfry/action-result`. Only the handler's answers count: the message's author for a
// button without action, the belfry's key or its groups key for a `belfry:` action.

export type ButtonStyle = '' | 'primary' | 'danger'
export interface Button { id: string; label: string; style: ButtonStyle; action: string }

export const PRESS_NS = 'belfry/action'
export const RESULT_NS = 'belfry/action-result'
export const MAX_BUTTONS = 4
export const ANSWER_TIMEOUT = 20 // seconds before "No answer yet."
/** The belfry: actions this app knows (docs/actions.md §4). */
export const KNOWN_ACTIONS = ['belfry:website-publish', 'belfry:offer-decide', 'belfry:request-close']

const ID = /^[a-z0-9-]{1,32}$/
const STYLES: ButtonStyle[] = ['', 'primary', 'danger']

/** The buttons TAGS carry, in tag order: bad tags, repeated ids and anything after the fourth
 *  are left out; an unknown style is plain; labels are trimmed. */
export function parseButtons(tags: unknown[]): Button[] {
  const out: Button[] = []
  const ids = new Set<string>()
  for (const t of tags ?? []) {
    if (!Array.isArray(t) || t.length < 3 || t[0] !== 'button') continue
    if (t.slice(1, 5).some((x) => typeof x !== 'string')) continue
    const id = t[1] as string
    const label = (t[2] as string).trim()
    if (!ID.test(id) || !label || [...label].length > 40 || ids.has(id)) continue
    const style = (STYLES.includes(t[3] as ButtonStyle) ? t[3] : '') as ButtonStyle
    ids.add(id)
    out.push({ id, label, style, action: (t[4] as string | undefined) ?? '' })
    if (out.length === MAX_BUTTONS) break
  }
  return out
}

export const isBelfryAction = (action: string) => action.startsWith('belfry:')
export const knownAction = (action: string) => !isBelfryAction(action) || KNOWN_ACTIONS.includes(action)

/** The press event's template (docs/actions.md §2). */
export function pressTemplate(message: { id: string; pubkey: string; group: string }, button: string) {
  return {
    kind: 1985,
    content: '',
    tags: [['L', PRESS_NS], ['l', button, PRESS_NS], ['e', message.id], ['p', message.pubkey], ['h', message.group]],
  }
}

export interface PressInfo { id: string; pubkey: string; button: string; created_at: number }
export interface AnswerInfo { pubkey: string; l: string; press: string; content: string; code?: string; closed?: boolean; created_at: number }
export interface Mine { status: 'ok' | 'error'; code?: string; text: string }
export interface ButtonState { closed: boolean; closed_text?: string; mine: Mine | null }

/** The fixed sentences (docs/actions-vectors.json `fixed_text`). */
export const FIXED: Record<string, string> = {
  not_allowed: "You're not allowed to do this.",
  unknown_action: "This belfry doesn't know this button yet.",
  no_target: "This message doesn't say what to act on.",
  gone: 'This is no longer there.',
  expired: 'That took too long. Try again.',
  failed: "That didn't work. Try again.",
  '': "That didn't work.",
  timeout: 'No answer yet.',
  unknown_belfry_action: 'Update the app to use this.',
  'confirm:belfry:website-publish': 'Publish this version as the website? Everyone will see it at https://<domain>.',
  'confirm:belfry:offer-decide': 'Answer this offer? The answer is public in the thread; the code goes privately to them.',
  'confirm:belfry:request-close': 'Close this request? It stays readable, and people can still comment.',
}

/** The sentence for an error answer: its content, else the code's fixed sentence. */
export const errorText = (code: string | undefined, content: string) =>
  content.trim() || (code && code in FIXED && !code.includes(':') && code !== 'timeout' ? FIXED[code] : FIXED[''])

/** Who handles BUTTON of a message by AUTHOR: their keys. */
export const handlersOf = (b: { action: string }, author: string, belfryKeys: string[]) =>
  isBelfryAction(b.action) ? belfryKeys : [author]

/** What a message's buttons show (docs/actions.md §3): closed for everyone (by the latest closing
 *  answer), and the answer to my latest press. PRESSES: the presses known (any author), to tell
 *  which button an answer is about; an answer to an unknown press counts when its signer handles
 *  one of the message's buttons. */
export function buttonState(a: {
  message: { id: string; pubkey: string; buttons: { id: string; action: string }[] }
  me: string
  press: PressInfo | null
  presses?: PressInfo[]
  answers: AnswerInfo[]
  belfryKeys: string[]
}): ButtonState {
  const byId = new Map<string, PressInfo>()
  for (const p of a.presses ?? []) byId.set(p.id, p)
  if (a.press) byId.set(a.press.id, a.press)
  const trusted = (ans: AnswerInfo) => {
    const p = byId.get(ans.press)
    const bs = p ? a.message.buttons.filter((b) => b.id === p.button) : a.message.buttons
    return bs.some((b) => handlersOf(b, a.message.pubkey, a.belfryKeys).includes(ans.pubkey))
  }
  const good = a.answers.filter((x) => (x.l === 'ok' || x.l === 'error') && trusted(x))
  const latest = (list: AnswerInfo[]) => list.reduce<AnswerInfo | undefined>((m, x) => (!m || x.created_at >= m.created_at ? x : m), undefined)
  const closing = latest(good.filter((x) => x.l === 'ok' && x.closed))
  const mineAns = a.press ? latest(good.filter((x) => x.press === a.press!.id)) : undefined
  const mine: Mine | null = !mineAns ? null
    : mineAns.l === 'ok' ? { status: 'ok', text: mineAns.content }
    : { status: 'error', code: mineAns.code ?? '', text: errorText(mineAns.code, mineAns.content) }
  return closing ? { closed: true, closed_text: closing.content, mine } : { closed: false, mine }
}
