// What counts as a picture, shared by e-mails, DMs, channels and to-dos (canvas v179/v180, note
// n-email-pictures; the same rule in the iOS and Android apps): type image/jpeg, png, gif, webp,
// heic or heif, or no type (or application/octet-stream) but a picture file extension. Shown in the
// message; anything else stays a file row. SVG, BMP… are not pictures here.

const TYPES = /^image\/(jpeg|png|gif|webp|heic|heif)$/
const EXT = /\.(jpe?g|png|gif|webp|heic|heif)$/i

/** NAME: the file's name, or failing that its URL (the extension is read from its path). */
export function isPicture(mime: string | undefined, name?: string): boolean {
  const t = (mime ?? '').toLowerCase().split(';')[0].trim()
  if (TYPES.test(t)) return true
  if (t && t !== 'application/octet-stream') return false
  return !!name && EXT.test(name.split(/[?#]/)[0])
}

/** A picture's type for a blob URL when it came without one (the browser shows it either way). */
export function pictureType(mime: string | undefined, name?: string): string {
  const t = (mime ?? '').toLowerCase().split(';')[0].trim()
  if (TYPES.test(t)) return t
  const e = (name ?? '').split(/[?#]/)[0].match(EXT)?.[1].toLowerCase()
  return e === 'jpg' || e === 'jpeg' ? 'image/jpeg' : e ? `image/${e}` : 'application/octet-stream'
}

/** "HEIC", "JPG": the badge of a picture's fallback row. */
export function pictureBadge(mime: string | undefined, name?: string): string {
  const e = (name ?? '').split(/[?#]/)[0].match(/\.([a-z0-9]{1,5})$/i)?.[1]
  if (e) return e.toUpperCase().slice(0, 4)
  const t = pictureType(mime, name)
  return t.startsWith('image/') ? t.slice(6).replace('jpeg', 'jpg').toUpperCase().slice(0, 4) : 'FILE'
}

/** A message's medium shown as a picture (its name, else its URL, for the extension). */
export const isPictureMedia = (m: { mime: string; name?: string; url: string }) => isPicture(m.mime, m.name || m.url)
/** Pictures and videos: what the Photos page and counts hold; the rest is Files. */
export const isVisual = (m: { mime: string; name?: string; url: string }) => isPictureMedia(m) || m.mime.startsWith('video/')
