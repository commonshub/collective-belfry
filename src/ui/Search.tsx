// Search messages: in one channel (from its header) or in all of them. Every word must appear
// (accents and case ignored); a result opens its channel on that message.
import { useMemo, useState } from 'react'
import { channelIcon, displayName, groups, messages, type Message } from '../core/model'
import { useVersion } from '../core/store'
import { Avatar, back, fmtDay, fmtTime, go } from './common'
import { SearchIcon } from './icons'

const fold = (s: string) => s.normalize('NFD').replace(/\p{M}/gu, '').toLowerCase()

export function SearchView({ channel }: { channel?: string }) {
  useVersion()
  const [q, setQ] = useState('')
  const [everywhere, setEverywhere] = useState(!channel)
  const all = groups()
  const here = channel ? all.find((g) => g.id === channel) : undefined
  const words = fold(q).split(/\s+/).filter(Boolean)
  const results = useMemo(() => {
    if (!words.length) return []
    const scope = everywhere || !channel ? all.map((g) => g.id) : [channel]
    const out: { m: Message; gid: string }[] = []
    for (const gid of scope)
      for (const m of messages(gid)) {
        const hay = fold(`${m.text} ${displayName(m.ev.pubkey)} ${m.media.map((x) => x.name ?? '').join(' ')}`)
        if (words.every((w) => hay.includes(w))) out.push({ m, gid })
      }
    return out.sort((a, b) => b.m.ev.created_at - a.m.ev.created_at).slice(0, 200)
  }, [q, everywhere, channel, all.length]) // eslint-disable-line react-hooks/exhaustive-deps
  return (
    <div className="screen">
      <div className="topbar search-bar">
        <button className="icon-btn" aria-label="Back" onClick={() => back(channel ? 'c/' + encodeURIComponent(channel) : 'c')}>‹</button>
        <label className="search-field">
          <SearchIcon />
          <input autoFocus type="search" enterKeyHint="search" placeholder={here && !everywhere ? `Search ${channelIcon(here)}${here.name}` : 'Search all channels'}
            value={q} onChange={(e) => setQ(e.target.value)} aria-label="Search messages" />
        </label>
      </div>
      {channel && (
        <div className="search-scope" role="radiogroup" aria-label="Where to search">
          <button role="radio" aria-checked={!everywhere} className={!everywhere ? 'on' : ''} onClick={() => setEverywhere(false)}>{channelIcon(here)}{here?.name ?? channel}</button>
          <button role="radio" aria-checked={everywhere} className={everywhere ? 'on' : ''} onClick={() => setEverywhere(true)}>All channels</button>
        </div>
      )}
      <div className="scroll">
        {!words.length && <div className="empty">Words from a message, or a name.</div>}
        {words.length > 0 && !results.length && <div className="empty">Nothing found.</div>}
        {results.length > 0 && <div className="search-count">{results.length === 200 ? '200+' : results.length} {results.length === 1 ? 'result' : 'results'}</div>}
        {results.map(({ m, gid }) => {
          const g = all.find((x) => x.id === gid)
          return (
            <button key={m.ev.id} className="search-hit" onClick={() => go(`c/${encodeURIComponent(gid)}/m/${m.ev.id}`)}>
              <Avatar pubkey={m.ev.pubkey} size={32} />
              <span className="t">
                <span className="who"><b>{displayName(m.ev.pubkey)}</b>{(everywhere || !channel) && <span className="in">{channelIcon(g)}{g?.name ?? gid}</span>}
                  <time>{fmtDay(new Date(m.ev.created_at * 1000))} · {fmtTime(m.ev.created_at)}</time></span>
                <span className="snip">{snippet(m.text || m.media.map((x) => x.name || 'Photo').join(', '), words)}</span>
              </span>
            </button>
          )
        })}
      </div>
    </div>
  )
}

/** The text around the first match, the matched words marked. */
function snippet(text: string, words: string[]) {
  const f = fold(text)
  const at = Math.max(0, Math.min(...words.map((w) => { const i = f.indexOf(w); return i < 0 ? Infinity : i })) - 40)
  const cut = (at > 0 ? '…' : '') + text.slice(at, at + 180) + (text.length > at + 180 ? '…' : '')
  const fc = fold(cut)
  const marks: [number, number][] = []
  for (const w of words) { let i = fc.indexOf(w); while (i >= 0) { marks.push([i, i + w.length]); i = fc.indexOf(w, i + w.length) } }
  marks.sort((a, b) => a[0] - b[0])
  const out: (string | JSX.Element)[] = []
  let pos = 0
  for (const [a, b] of marks) {
    if (a < pos) continue
    out.push(cut.slice(pos, a), <mark key={a}>{cut.slice(a, b)}</mark>)
    pos = b
  }
  out.push(cut.slice(pos))
  return out
}
