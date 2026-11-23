// Meetings' pure rules (no I/O), shared with the vectors (tools/vectors/check.mjs).

/** SPEC 19.63 (vectors/meetings.json `removal`): who is no longer among a meeting's people. Of the
 *  chat's 9000 / 9001 signed by the MAKER or a belfry admin (ADMINS), the latest per `p` decides
 *  (9001 removed, a later 9000 brings back); anyone else's are ignored; the maker is never removed. */
export function removedOf(events: { kind: number; pubkey: string; created_at: number; tags: string[][] }[], groupId: string, maker: string, admins: string[]): Set<string> {
  const by = new Set([maker, ...admins])
  const last = new Map<string, { at: number; out: boolean }>()
  for (const x of events) {
    if ((x.kind !== 9000 && x.kind !== 9001) || !by.has(x.pubkey) || x.tags.find((t) => t[0] === 'h')?.[1] !== groupId) continue
    const p = x.tags.find((t) => t[0] === 'p')?.[1]
    if (!p || p === maker) continue
    const had = last.get(p)
    if (!had || x.created_at > had.at || (x.created_at === had.at && x.kind === 9001)) last.set(p, { at: x.created_at, out: x.kind === 9001 })
  }
  return new Set([...last].filter(([, v]) => v.out).map(([k]) => k))
}
