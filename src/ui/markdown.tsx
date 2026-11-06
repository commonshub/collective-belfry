import type { ReactNode } from 'react'

// A small Markdown subset for descriptions, rendered as React elements (never as HTML, so a
// description can't inject markup): paragraphs, line breaks, # headings, - / * / 1. lists,
// > quotes, **bold**, *italic* / _italic_, `code`, ~~strike~~, [text](https://…) and bare links.

const SAFE_URL = /^(https?:\/\/|mailto:)/i

function inline(text: string, key: string): ReactNode[] {
  const out: ReactNode[] = []
  const re = /(\*\*([^*]+)\*\*|__([^_]+)__|`([^`]+)`|~~([^~]+)~~|\[([^\]]+)\]\(([^)\s]+)\)|\*([^*\s][^*]*)\*|_([^_\s][^_]*)_|(https?:\/\/[^\s<>()]+[^\s<>().,;:!?'"]))/g
  let last = 0
  let m: RegExpExecArray | null
  let i = 0
  while ((m = re.exec(text))) {
    if (m.index > last) out.push(text.slice(last, m.index))
    const k = `${key}-${i++}`
    if (m[2] ?? m[3]) out.push(<strong key={k}>{inline(m[2] ?? m[3], k)}</strong>)
    else if (m[4]) out.push(<code key={k}>{m[4]}</code>)
    else if (m[5]) out.push(<s key={k}>{inline(m[5], k)}</s>)
    else if (m[6] && m[7]) {
      out.push(SAFE_URL.test(m[7])
        ? <a key={k} href={m[7]} target="_blank" rel="noreferrer noopener">{inline(m[6], k)}</a>
        : m[0])
    } else if (m[8] ?? m[9]) out.push(<em key={k}>{inline(m[8] ?? m[9], k)}</em>)
    else if (m[10]) out.push(<a key={k} href={m[10]} target="_blank" rel="noreferrer noopener">{m[10]}</a>)
    last = m.index + m[0].length
  }
  if (last < text.length) out.push(text.slice(last))
  return out
}

function lines(block: string[], key: string): ReactNode[] {
  return block.flatMap((l, i) => (i ? [<br key={`${key}-br${i}`} />, ...inline(l, `${key}-${i}`)] : inline(l, `${key}-${i}`)))
}

export function Markdown({ text, className, style }: { text: string; className?: string; style?: React.CSSProperties }) {
  const blocks: ReactNode[] = []
  const src = text.replace(/\r\n?/g, '\n').split('\n')
  let i = 0
  let n = 0
  while (i < src.length) {
    const line = src[i]
    const key = `b${n++}`
    if (!line.trim()) { i++; continue }
    const h = /^(#{1,3})\s+(.*)$/.exec(line)
    if (h) {
      blocks.push(<div key={key} className={'md-h md-h' + h[1].length}>{inline(h[2], key)}</div>)
      i++
      continue
    }
    if (/^\s*([-*+]|\d+[.)])\s+/.test(line)) {
      const ordered = /^\s*\d+[.)]\s+/.test(line)
      const items: string[] = []
      while (i < src.length && /^\s*([-*+]|\d+[.)])\s+/.test(src[i])) items.push(src[i++].replace(/^\s*([-*+]|\d+[.)])\s+/, ''))
      const lis = items.map((t, j) => <li key={`${key}-${j}`}>{inline(t, `${key}-${j}`)}</li>)
      blocks.push(ordered ? <ol key={key}>{lis}</ol> : <ul key={key}>{lis}</ul>)
      continue
    }
    if (/^>\s?/.test(line)) {
      const q: string[] = []
      while (i < src.length && /^>\s?/.test(src[i])) q.push(src[i++].replace(/^>\s?/, ''))
      blocks.push(<blockquote key={key}>{lines(q, key)}</blockquote>)
      continue
    }
    const p: string[] = []
    while (i < src.length && src[i].trim() && !/^(#{1,3}\s|>\s?|\s*([-*+]|\d+[.)])\s+)/.test(src[i])) p.push(src[i++])
    blocks.push(<p key={key}>{lines(p, key)}</p>)
  }
  return <div className={'md' + (className ? ' ' + className : '')} style={style}>{blocks}</div>
}
