// Reactions under a message, in a channel or a direct message (#issues 2026-11-15): tap a chip to
// add or take back yours; hold it (or right-click) for who reacted that way and when; the button
// to add one sits where the reactions are, not at the far right.
import type React from 'react'
import { useRef, useState } from 'react'
import { topEmojis } from '../core/actions'
import { displayName } from '../core/model'
import { Avatar, fmtDay, fmtTime, Sheet } from './common'
import { SmilePlusIcon } from './icons'

export const MORE_EMOJIS = [
  '👍', '❤️', '😂', '🎉', '🙏', '👀', '🔥', '👏', '😍', '🤔', '😮', '😢',
  '😅', '🙌', '💪', '✅', '❌', '💯', '🚀', '✨', '🌱', '☕', '🍕', '🍻',
  '😊', '😎', '🤗', '🥳', '😴', '🤯', '😬', '🙈', '👋', '🤝', '👌', '✌️',
  '💡', '📌', '📅', '⏰', '🎶', '📷', '🌞', '🌧️', '❄️', '🌈', '🐝', '🌻',
]

/** One emoji under a message: who gave it (pubkey → when, in seconds), and whether I did. */
export interface Chip { emoji: string; who: Map<string, number>; mine: boolean }

/** Hold (0.5 s) or right-click: LONG; a tap: TAP (not after a hold). */
function useHold(long: () => void, tap: () => void) {
  const t = useRef<ReturnType<typeof setTimeout> | null>(null)
  const held = useRef(false)
  const clear = () => { if (t.current) clearTimeout(t.current); t.current = null }
  return {
    // the message under it has its own hold (its actions): this one is the chip's alone
    onPointerDown: (e: React.PointerEvent) => { e.stopPropagation(); held.current = false; clear(); t.current = setTimeout(() => { held.current = true; long() }, 500) },
    onPointerUp: clear, onPointerLeave: clear, onPointerCancel: clear,
    // right-click (no click follows it); on a phone the hold above fired already
    onContextMenu: (e: React.MouseEvent) => { e.preventDefault(); e.stopPropagation(); if (!held.current) { clear(); long() } },
    onClick: () => { if (held.current) { held.current = false; return } tap() },
  }
}

function ChipButton({ c, onToggle, onWho }: { c: Chip; onToggle: () => void; onWho: () => void }) {
  const hold = useHold(onWho, onToggle)
  return (
    <button className={c.mine ? 'on' : ''} title={[...c.who.keys()].map((k) => displayName(k)).join(', ')} aria-label={`${c.emoji} ${c.who.size}: hold to see who`} {...hold}>
      <span>{c.emoji}</span> {c.who.size}
    </button>
  )
}

/** The chips and the add button. With no reactions yet, the add button shows on hover only
 *  (laptops), in the same place. */
export function ReactionChips({ chips, onToggle, onAdd, disabled = false }: { chips: Chip[]; onToggle: (emoji: string) => void; onAdd?: () => void; disabled?: boolean }) {
  const [who, setWho] = useState<Chip | null>(null)
  if (!chips.length && !onAdd) return null
  return (
    <div className={'reactions' + (chips.length ? '' : ' rx-empty')}>
      {chips.map((c) => <ChipButton key={c.emoji} c={c} onToggle={() => { if (!disabled) onToggle(c.emoji) }} onWho={() => setWho(c)} />)}
      {onAdd && !disabled && <button className="react-add" aria-label="Add a reaction" title="Add a reaction" onClick={onAdd}><SmilePlusIcon /></button>}
      {who && <WhoReacted chip={who} onClose={() => setWho(null)} />}
    </div>
  )
}

/** Everyone who reacted with that emoji, latest first, with the day and time. */
export function WhoReacted({ chip, onClose }: { chip: Chip; onClose: () => void }) {
  const list = [...chip.who].sort((a, b) => b[1] - a[1])
  return (
    <Sheet title={`${chip.emoji} · ${list.length} ${list.length === 1 ? 'reaction' : 'reactions'}`} onClose={onClose}>
      <div className="who-reacted">
        {list.map(([pk, at]) => (
          <div key={pk} className="who-row">
            <Avatar pubkey={pk} size={32} />
            <span className="t">{displayName(pk)}</span>
            <time>{fmtDay(new Date(at * 1000))} · {fmtTime(at)}</time>
          </div>
        ))}
      </div>
    </Sheet>
  )
}

/** Pick an emoji: the ones used most, then all of them. */
export function EmojiSheet({ onPick, onClose, isOn = () => false }: { onPick: (emoji: string) => void; onClose: () => void; isOn?: (emoji: string) => boolean }) {
  const [more, setMore] = useState(false)
  const pick = (e: string) => { onClose(); onPick(e) }
  return (
    <Sheet title="Add a reaction" onClose={onClose}>
      <div className="emoji-row">
        {topEmojis(5).map((e) => <button key={e} className={isOn(e) ? 'on' : ''} aria-label={`React with ${e}`} onClick={() => pick(e)}>{e}</button>)}
        <button aria-label="More emojis" aria-expanded={more} className={'more' + (more ? ' on' : '')} onClick={() => setMore(!more)}><SmilePlusIcon /></button>
      </div>
      {more && <div className="emoji-grid">{MORE_EMOJIS.map((e) => <button key={e} className={isOn(e) ? 'on' : ''} aria-label={`React with ${e}`} onClick={() => pick(e)}>{e}</button>)}</div>}
    </Sheet>
  )
}
