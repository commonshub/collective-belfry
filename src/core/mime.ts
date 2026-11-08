// Just enough MIME (RFC 2045–2047) for e-mails that arrive as Nostr Mail (kind 1301): the text,
// the subject and sender, attachments; and a plain-text message for answers.

export interface Part { name: string; type: string; data: Uint8Array }
export interface Mail {
  headers: Map<string, string>
  subject: string
  from: string
  text: string
  /** the whole text, quoted history included */
  full: string
  attachments: Part[]
}

const dec = (bytes: Uint8Array, charset = 'utf-8') => {
  try { return new TextDecoder(charset.toLowerCase(), { fatal: false }).decode(bytes) } catch { return new TextDecoder().decode(bytes) }
}

function b64bytes(s: string): Uint8Array {
  const bin = atob(s.replace(/[^A-Za-z0-9+/=]/g, ''))
  const out = new Uint8Array(bin.length)
  for (let i = 0; i < bin.length; i++) out[i] = bin.charCodeAt(i)
  return out
}

function qpBytes(s: string, header = false): Uint8Array {
  if (header) s = s.replace(/_/g, ' ')
  s = s.replace(/=\r?\n/g, '')
  const out: number[] = []
  for (let i = 0; i < s.length; i++) {
    const c = s[i]
    if (c === '=' && /^[0-9A-Fa-f]{2}$/.test(s.slice(i + 1, i + 3))) { out.push(parseInt(s.slice(i + 1, i + 3), 16)); i += 2 }
    else for (const b of new TextEncoder().encode(c)) out.push(b)
  }
  return new Uint8Array(out)
}

/** RFC 2047 encoded words: =?utf-8?B?…?= and =?iso-8859-1?Q?…?= */
export function decodeWords(v: string): string {
  return v
    .replace(/\?=\s+=\?/g, '?==?')
    .replace(/=\?([^?]+)\?([BbQq])\?([^?]*)\?=/g, (_, cs: string, enc: string, t: string) =>
      dec(enc.toUpperCase() === 'B' ? b64bytes(t) : qpBytes(t, true), cs.replace(/\*.*$/, '')))
}

function split(raw: string): [string, string] {
  const m = /\r?\n\r?\n/.exec(raw)
  return m ? [raw.slice(0, m.index), raw.slice(m.index + m[0].length)] : [raw, '']
}

function headersOf(block: string): Map<string, string> {
  const h = new Map<string, string>()
  for (const line of block.replace(/\r?\n[ \t]+/g, ' ').split(/\r?\n/)) {
    const i = line.indexOf(':')
    if (i > 0) {
      const k = line.slice(0, i).trim().toLowerCase()
      if (!h.has(k)) h.set(k, line.slice(i + 1).trim())
    }
  }
  return h
}

function param(header: string | undefined, name: string): string {
  if (!header) return ''
  const star = new RegExp(`;\\s*${name}\\*=([^';]*)'[^']*'([^;]*)`, 'i').exec(header)
  if (star) { try { return decodeURIComponent(star[2]) } catch { return star[2] } }
  const m = new RegExp(`;\\s*${name}=("([^"]*)"|[^;]*)`, 'i').exec(header)
  return m ? decodeWords((m[2] ?? m[1]).trim()) : ''
}

function body(h: Map<string, string>, b: string): Uint8Array {
  const enc = (h.get('content-transfer-encoding') ?? '').toLowerCase()
  if (enc === 'base64') return b64bytes(b)
  if (enc === 'quoted-printable') return qpBytes(b)
  return new TextEncoder().encode(b)
}

function walk(raw: string, out: { text: string; html: string; attachments: Part[] }, depth = 0) {
  const [hb, b] = split(raw)
  const h = headersOf(hb)
  const ct = h.get('content-type') ?? 'text/plain'
  const type = ct.split(';')[0].trim().toLowerCase()
  if (type.startsWith('multipart/') && depth < 8) {
    const boundary = param(ct, 'boundary')
    if (!boundary) return
    const parts = b.split(new RegExp(`\\r?\\n?--${boundary.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')}(?:--)?[ \\t]*\\r?\\n?`))
    for (const p of parts.slice(1)) if (p.trim()) walk(p, out, depth + 1)
    return
  }
  const name = param(h.get('content-disposition'), 'filename') || param(ct, 'name')
  const attached = /^attachment/i.test(h.get('content-disposition') ?? '') || (!!name && !type.startsWith('text/'))
  if (attached || !type.startsWith('text/')) {
    if (type !== 'message/delivery-status') out.attachments.push({ name: name || 'attachment', type, data: body(h, b) })
    return
  }
  const text = dec(body(h, b), param(ct, 'charset') || 'utf-8')
  if (type === 'text/html') { if (!out.html) out.html = text } else if (!out.text) out.text = text
}

export function htmlText(html: string): string {
  const doc = new DOMParser().parseFromString(html.replace(/<br\s*\/?>/gi, '\n').replace(/<\/(p|div|li|tr|h\d)>/gi, '$&\n'), 'text/html')
  doc.querySelectorAll('script,style,blockquote').forEach((n) => n.remove())
  return doc.body.textContent ?? ''
}

/** The new text of a reply: no quoted lines, nothing after "On … wrote:". */
export function withoutQuote(text: string): string {
  const out: string[] = []
  for (const line of text.replace(/\r\n/g, '\n').split('\n')) {
    if (/^\s*(On .{4,200} wrote:|Le .{4,200} a écrit\s?:|-----\s*Original Message\s*-----)\s*$/.test(line)) break
    if (line.startsWith('>')) continue
    out.push(line.trimEnd())
  }
  return out.join('\n').replace(/\n{3,}/g, '\n\n').trim()
}

export function parseMail(raw: string): Mail {
  const [hb] = split(raw)
  const headers = headersOf(hb)
  const out = { text: '', html: '', attachments: [] as Part[] }
  walk(raw, out)
  return {
    headers,
    subject: decodeWords(headers.get('subject') ?? ''),
    from: decodeWords(headers.get('from') ?? ''),
    text: withoutQuote(out.text || htmlText(out.html)),
    full: (out.text || htmlText(out.html)).replace(/\r\n/g, '\n').trim(),
    attachments: out.attachments,
  }
}

/** "Ana <ana@x.org>" → { name: 'Ana', address: 'ana@x.org' } */
export function addressOf(v: string): { name: string; address: string } {
  const m = /^\s*"?([^"<]*?)"?\s*<([^>]+)>\s*$/.exec(v)
  if (m) return { name: m[1].trim(), address: m[2].trim().toLowerCase() }
  return { name: '', address: v.trim().toLowerCase() }
}

const ascii = (s: string) => /^[\x20-\x7e]*$/.test(s)
const word = (s: string) => (ascii(s) ? s : `=?UTF-8?B?${btoa(String.fromCharCode(...new TextEncoder().encode(s)))}?=`)
const wrap76 = (s: string) => s.replace(/(.{76})/g, '$1\r\n')

function b64utf8(s: string) {
  const bytes = new TextEncoder().encode(s)
  let bin = ''
  for (let i = 0; i < bytes.length; i += 0x8000) bin += String.fromCharCode(...bytes.subarray(i, i + 0x8000))
  return btoa(bin)
}

/** A plain-text e-mail (UTF-8, base64 body), ready for the bridge. */
export function buildMail(m: { fromName: string; from: string; to: string; subject: string; text: string; inReplyTo?: string; references?: string; domain: string }): { raw: string; messageId: string } {
  const messageId = `<${crypto.randomUUID()}@${m.domain}>`
  const name = m.fromName.replace(/["\\\r\n]/g, '')
  const h = [
    `From: ${name ? (ascii(name) ? `"${name}"` : word(name)) + ' ' : ''}<${m.from}>`,
    `To: <${m.to}>`,
    `Subject: ${word(m.subject.replace(/[\r\n]+/g, ' '))}`,
    `Date: ${new Date().toUTCString().replace('GMT', '+0000')}`,
    `Message-ID: ${messageId}`,
    ...(m.inReplyTo ? [`In-Reply-To: ${m.inReplyTo}`, `References: ${[m.references, m.inReplyTo].filter(Boolean).join(' ')}`] : []),
    'MIME-Version: 1.0',
    'Content-Type: text/plain; charset=utf-8',
    'Content-Transfer-Encoding: base64',
  ]
  return { raw: h.join('\r\n') + '\r\n\r\n' + wrap76(b64utf8(m.text)) + '\r\n', messageId }
}
