// The web chat against the shared vectors (vectors/*.json, copied byte for byte from belfry-ios / belfry-android):
// bundles src/core/model.ts for Node with the browser modules stubbed, then checks every case.
//   node tools/vectors/check.mjs
import { build } from 'esbuild'
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import path from 'node:path'
const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '../..')
const stubs = path.join(root, 'tools/vectors/stubs.ts')
const out = path.join(root, 'tools/vectors/.model.mjs')
await build({
  entryPoints: [path.join(root, 'tools/vectors/entry.ts')], bundle: true, platform: 'node', format: 'esm', outfile: out, logLevel: 'error',
  plugins: [{ name: 'stubs', setup(b) { b.onResolve({ filter: /^(\.\/(store|sync|access)|\.\.\/config)$/ }, () => ({ path: stubs })) } }],
})
const M = await import(out + '?' + Date.now())
const vec = (f) => JSON.parse(readFileSync(path.join(root, 'vectors', f), 'utf8'))
let bad = 0
const check = (name, ok, got) => { if (!ok) bad++; console.log((ok ? 'ok   ' : 'FAIL ') + name + (ok ? '' : ' ' + JSON.stringify(got).slice(0, 600))) }
const eq = (a, b) => JSON.stringify(a) === JSON.stringify(b)

// groups.json: names (never the id), named, about, type, parent, closed, prompt, members, admins
for (const c of vec('groups.json').cases) {
  globalThis.__EVENTS = c.events; globalThis.__ME = c.me
  const got = M.allGroups().map((g) => {
    const named = g.metaAt.name !== undefined
    return { id: g.id, name: named ? g.rawName : null, named, about: g.about, type: M.channelType(g), parent: g.parent ?? null, closed: g.closed, prompt: g.prompt ?? null,
      members: [...g.members].sort(), admins: [...g.admins].sort(), applications: g.applications.map((a) => ({ pubkey: a.pubkey, answer: a.answer, at: a.at })) }
  })
  for (const want of c.expect.groups) {
    const g = got.find((x) => x.id === want.id)
    const pick = (o) => ({ id: o.id, name: o.name, named: o.named, about: o.about, type: o.type, parent: o.parent, closed: o.closed, prompt: o.prompt, members: [...o.members].sort(), admins: [...o.admins].sort(), applications: o.applications })
    check(`groups ${c.name}: ${want.id}`, !!g && eq(pick(g), pick(want)), { got: g && pick(g), want: pick(want) })
  }
}

// channel-refs.json "plain": references as #name, an unknown one as the kind's word
const cr = vec('channel-refs.json')
const metaEvents = Object.entries(cr.groups).map(([id, name], i) => ({ id: 'm' + i, pubkey: cr.encode[0].pubkey, kind: 39000, created_at: 1, tags: [['d', id], ['name', name]], content: '', sig: '' }))
globalThis.__EVENTS = metaEvents; globalThis.__ME = 'f'.repeat(64)
for (const p of cr.plain) check(`channel-refs plain: ${p.plain}`, M.plainRefs(p.text) === p.plain, M.plainRefs(p.text))

// reply-reactions.json: readers key on the last e tag, never on k
for (const c of vec('reply-reactions.json').cases) {
  globalThis.__EVENTS = c.events; globalThis.__ME = c.me
  const rx = M.reactions(c.group)
  for (const [target, want] of Object.entries(c.expect.reactions)) {
    const got = (rx.get(target) ?? []).map((r) => ({ emoji: r.emoji, who: [...r.who].sort(), mine: r.mine?.id ?? null }))
    const norm = (l) => [...l].map((r) => ({ ...r, who: [...r.who].sort() })).sort((a, b) => (a.emoji < b.emoji ? -1 : 1))
    check(`reply-reactions ${c.name}: ${target.slice(0, 8)}`, eq(norm(got), norm(want)), { got, want })
  }
  // what a client writes: k = the target's kind
  for (const w of c.write ?? []) {
    const tags = [['h', c.group], ['e', w.target], ['p', w.author], ['k', String(w.target_kind)]] // = actions.ts react()
    check(`reply-reactions write: k ${w.target_kind}`, eq(tags, w.tags), { tags, want: w.tags })
  }
}
// references.json (SPEC 19.44): names inside the text, the box at the end
const refs = vec('references.json')
for (const c of refs.cases) { const got = M.parts(c.text, refs.domain); check(`references ${c.name}`, eq(got, c.parts), { got, want: c.parts }) }
// ticket-prices.json (from belfry-ios, the server's price_of as reference): the price now, and its words
for (const c of vec('ticket-prices.json').cases) {
  const r = M.pricesOf(c.tags, c.now)
  const p = r.price
  const got = { priced: r.priced, amount: p?.amount ?? '', currency: p?.currency ?? '', vat: p?.vat ?? '', vat_included: !!p?.vatIncluded, until: p?.until ?? null, on_sale: r.onSale }
  const line = !r.priced ? 'Free' : r.onSale === false ? 'Not on sale now' : p ? M.priceFull(p) : 'Paid'
  check(`ticket-prices ${c.name}`, eq(got, c.out) && line === c.line, { got, line, want: c.out, wantLine: c.line })
}
console.log(bad ? `vectors: ${bad} failed` : 'vectors: all ok')

// guest-signin.json (belfry-ios 5d1ea9a, docs/guests.md "An address that has an account"): the verify body and each answer's next step
{
  const gout = path.join(root, 'tools/vectors/.guestjoin.mjs')
  await build({
    entryPoints: [path.join(root, 'src/core/guestjoin.ts')], bundle: true, platform: 'node', format: 'esm', outfile: gout, logLevel: 'error',
    plugins: [{ name: 'stubs', setup(b) { b.onResolve({ filter: /^(\.\/(store|sync|nav|guestmode|roles|auth)|\.\.\/config)$/ }, () => ({ path: stubs })) } }],
  })
  const G = await import(gout + '?' + Date.now())
  const v = vec('guest-signin.json')
  const t = v.request.typed
  check('guest-signin body', eq(G.verifyBody(t.email, t.code, t.name, t.session_key, t.client), v.request.body), G.verifyBody(t.email, t.code, t.name, t.session_key, t.client))
  check('guest-signin body without name', eq(G.verifyBody(t.email, t.code, undefined, t.session_key, t.client), v.request.body_without_name), G.verifyBody(t.email, t.code, undefined, t.session_key, t.client))
  for (const a of v.answers) {
    const next = G.verifyNext(a.status, a.body)
    let extra = true, got = { next }
    if (a.next === 'signed_in') {
      const r = G.signedInFrom(a.body, new Uint8Array(32), t.email, v.domain)
      got = { next, address: r.address, open: r.open, moved: r.moved, pubkey: r.session.pubkey, bunker: r.session.bunkerUrl }
      extra = r.address === a.address && r.open === a.open && r.moved === a.moved && r.session.pubkey === a.body.pubkey && r.session.bunkerUrl === a.body.bunker_url
    }
    if (a.next === 'sign_in_instead') extra = (a.body.moved === true) === a.moved
    check(`guest-signin ${a.name}`, next === a.next && extra, got)
  }
}
process.exit(bad ? 1 : 0)
