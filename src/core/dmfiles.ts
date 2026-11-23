// Pictures, files and audio recordings in direct messages (the lead's contract, 2026-11-21;
// belfry-android vectors/dm-files.json): each file is one NIP-17 kind 15 rumor. The device
// encrypts the file with AES-256-GCM (a fresh 32-byte key and 12-byte nonce per file, no
// associated data, the 16-byte tag after the ciphertext) and uploads the ciphertext. Readers
// download, check x, decrypt, check ox: anything wrong shows UNREADABLE, never the bytes.
// Pure (WebCrypto only), so the shared vectors run against it in node.

export const FILE_KIND = 15
export const MAX_FILE = 25_000_000 // a member's file (canvas v104)
export const MAX_FILE_GUEST = 5_000_000
export const UNREADABLE = "This file can't be opened"
export const tooLarge = (max: number) => `This file is too large (${Math.round(max / 1e6)} MB at most)`

const hex = (b: Uint8Array) => Array.from(b, (x) => x.toString(16).padStart(2, '0')).join('')
const unhex = (s: string) => new Uint8Array((s.match(/../g) ?? []).map((h) => parseInt(h, 16)))
const isHex = (s: unknown, bytes: number): s is string => typeof s === 'string' && s.length === bytes * 2 && /^[0-9a-f]+$/.test(s)
const buf = (b: Uint8Array) => b.buffer.slice(b.byteOffset, b.byteOffset + b.byteLength) as ArrayBuffer

export async function sha256hex(b: Uint8Array): Promise<string> {
  return hex(new Uint8Array(await crypto.subtle.digest('SHA-256', buf(b))))
}

async function gcm(key: Uint8Array, nonce: Uint8Array, data: Uint8Array, how: 'encrypt' | 'decrypt'): Promise<Uint8Array> {
  const k = await crypto.subtle.importKey('raw', buf(key), 'AES-GCM', false, [how])
  const out = how === 'encrypt'
    ? await crypto.subtle.encrypt({ name: 'AES-GCM', iv: buf(nonce) }, k, buf(data))
    : await crypto.subtle.decrypt({ name: 'AES-GCM', iv: buf(nonce) }, k, buf(data))
  return new Uint8Array(out)
}

export interface Sealed { cipher: Uint8Array; key: string; nonce: string; x: string; ox: string }

/** PLAIN encrypted for upload (KEY and NONCE: the vectors' fixed ones; fresh random otherwise). */
export async function encryptFile(plain: Uint8Array, key?: string, nonce?: string): Promise<Sealed> {
  const k = key ? unhex(key) : crypto.getRandomValues(new Uint8Array(32))
  const n = nonce ? unhex(nonce) : crypto.getRandomValues(new Uint8Array(12))
  const cipher = await gcm(k, n, plain, 'encrypt')
  return { cipher, key: hex(k), nonce: hex(n), x: await sha256hex(cipher), ox: await sha256hex(plain) }
}

/** A thumbnail: the file's key, its own nonce. */
export async function encryptThumb(plain: Uint8Array, key: string, nonce?: string): Promise<{ cipher: Uint8Array; nonce: string }> {
  const n = nonce ? unhex(nonce) : crypto.getRandomValues(new Uint8Array(12))
  return { cipher: await gcm(unhex(key), n, plain, 'encrypt'), nonce: hex(n) }
}

export interface FileMeta { mime: string; size: number; name?: string; dim?: string; blurhash?: string; thumb?: string; thumbNonce?: string; duration?: number; group?: string }

/** The kind 15 tags, in the contract's order. */
export function fileTags(others: string[], s: Sealed, m: FileMeta): string[][] {
  const t: string[][] = [
    ...others.map((p) => ['p', p]),
    ['file-type', m.mime], ['encryption-algorithm', 'aes-gcm'], ['decryption-key', s.key], ['decryption-nonce', s.nonce],
    ['x', s.x], ['ox', s.ox], ['size', String(m.size)],
  ]
  if (m.name) t.push(['name', m.name])
  if (m.dim) t.push(['dim', m.dim])
  if (m.blurhash) t.push(['blurhash', m.blurhash])
  if (m.thumb && m.thumbNonce) t.push(['thumb', m.thumb], ['thumb-nonce', m.thumbNonce])
  if (m.duration !== undefined) t.push(['duration', String(Math.round(m.duration))])
  if (m.group) t.push(['group', m.group])
  return t
}

const tagOf = (tags: string[][], k: string) => tags.find((t) => t[0] === k)?.[1]

/** What a kind 15 says about its file; null when a required tag is missing or malformed. */
export interface FileRef { url: string; mime: string; key: string; nonce: string; x: string; ox: string; size: number; name: string; dim?: [number, number]; duration?: number; thumb?: string; thumbNonce?: string; group?: string }
export function fileRefOf(r: { content: string; tags: string[][] }): FileRef | null {
  const t = r.tags
  const key = tagOf(t, 'decryption-key'), nonce = tagOf(t, 'decryption-nonce'), x = tagOf(t, 'x'), ox = tagOf(t, 'ox')
  if (tagOf(t, 'encryption-algorithm') !== 'aes-gcm' || !isHex(key, 32) || !isHex(nonce, 12) || !isHex(x, 32) || !isHex(ox, 32)) return null
  const url = r.content.trim()
  if (!/^https?:\/\/\S+$/.test(url)) return null // the download is checked (x, GCM, ox) whatever serves it
  const size = Number(tagOf(t, 'size'))
  const dimM = /^(\d{1,5})x(\d{1,5})$/.exec(tagOf(t, 'dim') ?? '')
  const dur = Number(tagOf(t, 'duration'))
  const thumb = tagOf(t, 'thumb'), thumbNonce = tagOf(t, 'thumb-nonce')
  return {
    url, mime: (tagOf(t, 'file-type') || 'application/octet-stream').toLowerCase(), key, nonce, x, ox,
    name: shownName(tagOf(t, 'name'), (tagOf(t, 'file-type') || '').toLowerCase()),
    size: Number.isFinite(size) && size >= 0 ? size : 0,
    ...(dimM ? { dim: [Number(dimM[1]), Number(dimM[2])] as [number, number] } : {}),
    ...(Number.isFinite(dur) && dur > 0 ? { duration: dur } : {}),
    ...(thumb && /^https:\/\//.test(thumb) && isHex(thumbNonce, 12) ? { thumb, thumbNonce } : {}),
    ...(isHex(tagOf(t, 'group'), 8) ? { group: tagOf(t, 'group') } : {}),
  }
}

/** §23: a private group's files are encrypted like kind 15: url → the tags that open it (from its
 *  imeta, filled by model.parseMedia), for useMediaUrl. */
export const encryptedMedia = new Map<string, string[][]>()

export type Opened = { ok: true; plain: Uint8Array; mime: string } | { ok: false }

/** A downloaded file (THUMB: its thumbnail) opened with the rumor's TAGS: x of the download, the
 *  GCM tag, then ox of the plain file (not for a thumbnail, which has none). */
export async function openFile(tags: string[][], downloaded: Uint8Array, thumb = false): Promise<Opened> {
  const key = tagOf(tags, 'decryption-key'), nonce = tagOf(tags, thumb ? 'thumb-nonce' : 'decryption-nonce'), x = tagOf(tags, 'x'), ox = tagOf(tags, 'ox')
  if (tagOf(tags, 'encryption-algorithm') !== 'aes-gcm' || !isHex(key, 32) || !isHex(nonce, 12) || !isHex(x, 32) || !isHex(ox, 32)) return { ok: false }
  if (!thumb && (await sha256hex(downloaded)) !== x) return { ok: false }
  let plain: Uint8Array
  try { plain = await gcm(unhex(key), unhex(nonce), downloaded, 'decrypt') } catch { return { ok: false } }
  if (!thumb && (await sha256hex(plain)) !== ox) return { ok: false }
  return { ok: true, plain, mime: (tagOf(tags, 'file-type') || 'application/octet-stream').toLowerCase() }
}

/** Rumors → bubbles in time order: one bubble per (author, group) with its files by created_at
 *  (ties: id) and its caption (a kind 14 of the group); a file or a message without group is its
 *  own bubble. A bubble's time is its first rumor's. */
export interface BubbleIn { id: string; kind: number; pubkey: string; created_at: number; group?: string | null }
export function bubbles<T extends BubbleIn>(rumors: T[]): { files: T[]; caption: T | null; at: number }[] {
  const out = new Map<string, { files: T[]; caption: T | null; at: number; first: string }>()
  const order = (a: T, b: T) => a.created_at - b.created_at || (a.id < b.id ? -1 : a.id > b.id ? 1 : 0)
  for (const r of [...rumors].sort(order)) {
    const k = r.group ? `${r.pubkey}:${r.group}` : r.id
    const b = out.get(k) ?? { files: [], caption: null, at: r.created_at, first: r.id }
    if (r.kind === FILE_KIND) b.files.push(r)
    else if (!b.caption) b.caption = r
    out.set(k, b)
  }
  return [...out.values()].sort((a, b) => a.at - b.at || (a.first < b.first ? -1 : 1)).map(({ files, caption, at }) => ({ files, caption, at }))
}

/** How a file's name shows (dm-files.json "names"): its last path segment, without control
 *  characters or direction marks, trimmed, at most 200 UTF-16 units; else File / Picture /
 *  Audio recording by its type. */
export function shownName(name: string | null | undefined, mime: string): string {
  const last = (name ?? '').split(/[/\\]/).pop() ?? ''
  // eslint-disable-next-line no-control-regex
  const clean = last.replace(/[\u0000-\u001f\u007f-\u009f\u061c\u200e\u200f\u202a-\u202e\u2066-\u2069]/g, '').trim().slice(0, 200)
  if (clean) return clean
  return mime.startsWith('image/') ? 'Picture' : mime.startsWith('audio/') ? 'Audio recording' : 'File'
}

/** "PDF · 1.2 MB", the label of a file's kind. */
export function fileLabel(mime: string): string {
  const sub = mime.split('/')[1] ?? ''
  if (mime === 'application/pdf') return 'PDF'
  if (mime.startsWith('video/')) return 'Video'
  if (mime.startsWith('audio/')) return 'Recording'
  if (mime.startsWith('image/')) return 'Picture'
  if (/word|document/.test(sub)) return 'Document'
  if (/sheet|excel|csv/.test(sub)) return 'Spreadsheet'
  if (/presentation|powerpoint/.test(sub)) return 'Slides'
  if (/zip|compressed|tar/.test(sub)) return 'Archive'
  if (mime.startsWith('text/')) return 'Text'
  return 'File'
}
export const sizeLabel = (n: number) => (n >= 1e6 ? `${(n / 1e6).toFixed(1)} MB` : `${Math.max(1, Math.round(n / 1e3))} kB`)
