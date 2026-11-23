// Direct conversations' pure rules (no I/O), shared with the vectors (tools/vectors/check.mjs).

/** The people of a direct rumor: its author and its p tags, me included (vectors/sealed.json `notes`). */
export function dmParticipants(rumor: { pubkey: string; tags: string[][] }, me: string): string[] {
  return [...new Set([rumor.pubkey, me, ...rumor.tags.filter((t) => t[0] === 'p' && t[1]).map((t) => t[1])])].sort()
}
/** Notes to myself: a rumor whose only participant is me (one p = me; also a rumor of mine with no
 *  p tag, as older Android builds wrote notes). */
export const isNoteRumor = (rumor: { pubkey: string; tags: string[][] }, me: string) => { const p = dmParticipants(rumor, me); return p.length === 1 && p[0] === me }
