// Website proposals scoped to a part of the site (Belfry e598f3f; skills/belfry-homepage/SKILL.md,
// docs/actions.md): one ["path", p] tag per path on the 30023, none for the whole site. The rules
// mirror the server's website.valid_path / parse_paths exactly, so the chat never sends a scope the
// belfry would refuse (an invalid scope is not published, it never falls back to the whole site).
import { nip19 } from 'nostr-tools'
import { queryOnce, me, queryAuthed } from './sync'
import { all } from './store'

export const MAX_PATHS = 10
export const MAX_PATH_LEN = 200
const SEGMENT = /^[A-Za-z0-9_-][A-Za-z0-9._-]*$/

/** A folder (/ios/screenshots/) or one file with an extension (/android/index.html); "/" = all. */
export function validPath(p: string): boolean {
  if (!p.startsWith('/') || p.length > MAX_PATH_LEN) return false
  if (p === '/') return true
  const segs = p.endsWith('/') ? p.slice(1, -1).split('/') : p.slice(1).split('/')
  if (!segs.every((s) => SEGMENT.test(s))) return false
  return p.endsWith('/') || segs[segs.length - 1].includes('.')
}

/** What someone typed in "Only this part of the site" (paths separated by spaces or commas):
 *  the sorted scope, [] for the whole site, or an error to show. */
export function parsePaths(input: string): { paths: string[] } | { error: string } {
  const list = input.split(/[\s,]+/).filter(Boolean)
  if (!list.length) return { paths: [] }
  if (list.length > MAX_PATHS) return { error: `At most ${MAX_PATHS} paths.` }
  const bad = list.find((p) => !validPath(p))
  if (bad) return { error: `“${bad.slice(0, 60)}” isn’t a path: a folder starts and ends with / (like /ios/screenshots/), a file has an extension (like /android/index.html); letters, digits, - _ and . only.` }
  if (list.includes('/')) return { paths: [] }
  return { paths: [...new Set(list)].sort() }
}

/** "/ios/screenshots/", "/a/ and /b/", "/a/, /b/ and /c/" (the server's scope_words). */
export const scopeWords = (scope: string[]) =>
  scope.length === 1 ? scope[0] : scope.slice(0, -1).join(', ') + ' and ' + scope[scope.length - 1]

/** A website proposal's title starts with what it changes ("Website: /ios/screenshots/ — …",
 *  "Website: the whole site — …"); an older "Website: … — " start is replaced. */
export function websiteTitle(title: string, scope: string[]): string {
  const rest = title.replace(/^Website:\s.*?\s—\s*/, '').trim() || 'a new version'
  return `Website: ${scope.length ? scopeWords(scope) : 'the whole site'} — ${rest}`
}

/** The site an nsite ref names: nsite:<npub>[/<name>], preview.<domain>/p/<npub>[@<name>] or
 *  https://<npub>.<gateway>/ (no name = the root site). */
export function siteOf(ref: string): { pubkey: string; name?: string } | null {
  const m = /(npub1[02-9ac-hj-np-z]{58})(?:[/@]([a-z0-9-]{1,13}))?/.exec(ref)
  if (!m) return null
  try {
    const d = nip19.decode(m[1])
    return d.type === 'npub' ? { pubkey: d.data as string, name: ref.startsWith('https://npub1') ? undefined : m[2] } : null
  } catch { return null }
}

/** The files of that version (its manifest's path tags: 35128 d=<name>, or 15128 for a root
 *  site, on the belfry's relay); null when it can't be read. */
export async function siteFiles(ref: string): Promise<string[] | null> {
  const s = siteOf(ref)
  if (!s) return null
  const kind = s.name ? 35128 : 15128
  const mine = (e: { kind: number; pubkey: string; tags: string[][] }) => e.kind === kind && e.pubkey === s.pubkey && (!s.name || e.tags.some((t) => t[0] === 'd' && t[1] === s.name))
  const held = all(me()).filter(mine)
  const evs = held.length ? held : (await queryAuthed('main', s.name ? { kinds: [kind], authors: [s.pubkey], '#d': [s.name] } : { kinds: [kind], authors: [s.pubkey] }, 6000).catch(() => [])).filter(mine)
  const ev = [...evs].sort((a, b) => b.created_at - a.created_at)[0]
  if (!ev) return null
  return ev.tags.filter((t) => t[0] === 'path' && typeof t[1] === 'string' && t[1].startsWith('/')).map((t) => t[1])
}

/** The one folder every file is in (the deepest), or null when some file sits at the root. */
export function rootFolder(files: string[]): string | null {
  if (!files.length) return null
  const dirs = files.map((f) => f.slice(0, f.lastIndexOf('/') + 1))
  let common = dirs[0]
  for (const d of dirs) while (!d.startsWith(common)) common = common.slice(0, common.slice(0, -1).lastIndexOf('/') + 1)
  return common.length > 1 && validPath(common) ? common : null
}
