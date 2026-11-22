// The web chat against the shared vectors (vectors/*.json, copied byte for byte from belfry-ios / belfry-android):
// bundles src/core/model.ts for Node with the browser modules stubbed, then checks every case.
//   node tools/vectors/check.mjs
import { build } from 'esbuild'
import { readFileSync } from 'node:fs'
import { createHash } from 'node:crypto'
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
// … and as plain text (one-line previews): each reference as its known name, else its generic word
for (const c of refs.cases) { const got = M.plainOf(c.text, (r) => refs.known[M.refKey(r)], refs.domain); check(`references plain ${c.name}`, got === c.plain, { got, want: c.plain }) }
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

// way-in.json (belfry-ios 426b308, docs/membership.md "Applying from the chat (verified)", "A name and a
// username"): each answer of the way in → the screen the app shows next, its words, the suggestions offered
{
  const dout = path.join(root, 'tools/vectors/.direct.mjs')
  await build({
    entryPoints: [path.join(root, 'src/core/direct.ts')], bundle: true, platform: 'node', format: 'esm', outfile: dout, logLevel: 'error',
    plugins: [{ name: 'stubs', setup(b) { b.onResolve({ filter: /^(\.\/(store|sync|nav|guestmode|roles|auth)|\.\.\/config)$/ }, () => ({ path: stubs })) } }],
  })
  const mem = new Map()
  globalThis.localStorage = { getItem: (k) => mem.get(k) ?? null, setItem: (k, v) => mem.set(k, String(v)), removeItem: (k) => mem.delete(k) }
  globalThis.window ??= globalThis
  const D = await import(dout + '?' + Date.now())
  const v = vec('way-in.json'), dom = v.domain, typed = 'lina.moreau@example.org'
  const ok = (r) => ({ screen: 'signed_in', ...(r.signed.address.includes('@' + dom) ? { nip05: r.signed.address } : {}) })
  const screenOf = (c) => {
    const d = c.body ?? {}
    try {
      if (c.status < 200 || c.status > 299) {
        if (c.call === 'welcome') { const w = D.welcomed(c.status, d, 'Lina Moreau', 'lina'); return { screen: 'signed_in', ...(w !== 'already_set' && w.nip05 ? { nip05: w.nip05 } : {}) } }
        throw D.failureOf(c.status, d)
      }
      if (c.call === 'login/start') return { screen: 'code' }
      if (c.call === 'login/verify' || c.call === 'apply/verify') {
        const r = D.verified(d, typed, dom)
        if (r.state === 'signed_in') return ok(r)
        if (r.state === 'pending') return { screen: 'pending' }
        if (c.call === 'apply/verify') throw new D.DirectFailed('failed')
        return { screen: 'unknown', proof: r.proof }
      }
      if (c.call === 'apply/start') {
        const r = D.applyStarted(c.status, d, typed, dom)
        if (r === 'sent') return { screen: 'code' }
        return r.state === 'signed_in' ? ok(r) : { screen: 'pending' }
      }
      if (c.call === 'apply/status') {
        const st = D.statusOf(d, typed, dom)
        if (st.state === 'approved') return st.signed ? ok(st) : { screen: 'log_in', ...(st.nip05 ? { nip05: st.nip05 } : {}) }
        if (st.state === 'pending') return { screen: 'pending', ...(st.pending ? { pending: st.pending } : {}) }
        return { screen: st.state === 'declined' ? 'declined' : 'start' }
      }
      if (c.call === 'welcome') { const w = D.welcomed(c.status, d, 'Lina Moreau', 'lina'); return { screen: 'signed_in', ...(w !== 'already_set' && w.nip05 ? { nip05: w.nip05 } : {}) } }
      return { screen: '?' }
    } catch (e) {
      const r = D.refusalOf(e instanceof D.DirectFailed ? e : new D.DirectFailed('failed'))
      if (r.screen !== 'stay') return r
      return { screen: 'stay', message: r.message, ...(r.clears_code ? { clears_code: true } : {}), ...(r.suggestions.length ? { suggestions: r.suggestions } : {}) }
    }
  }
  const keys = ['screen', 'message', 'clears_code', 'suggestions', 'proof', 'nip05', 'pending']
  const pick = (o) => Object.fromEntries(keys.filter((k) => o[k] !== undefined).map((k) => [k, o[k]]))
  for (const c of v.cases) { const got = pick(screenOf(c)); check(`way-in ${c.call} ${c.status}: ${c.name}`, eq(got, pick(c)), { got, want: pick(c) }) }
  for (const c of v.username_check) {
    const d = c.body, sug = D.suggestionsOf(d)
    const l = D.usernameLine({ state: d.state, username: d.username, reason: d.reason, suggestions: sug }, dom)
    const line = l.offers.length ? `${l.text} ${D.orList(l.offers)}` : l.text
    check(`way-in username: ${c.name}`, line === c.line && (!c.offers || eq(l.offers, c.offers)), { line, offers: l.offers })
  }
}
// dm-files.json (belfry-android): kind 15 files in direct messages, against src/core/dmfiles.ts
{
  const dout = path.join(root, 'tools/vectors/.dmfiles.mjs')
  await build({ entryPoints: [path.join(root, 'src/core/dmfiles.ts')], bundle: true, platform: 'node', format: 'esm', outfile: dout, logLevel: 'error' })
  const F = await import(dout + '?' + Date.now())
  const v = vec('dm-files.json')
  const hx = (b) => Buffer.from(b).toString('hex'), uh = (s) => new Uint8Array(Buffer.from(s, 'hex'))
  check('dm-files: the unreadable sentence', F.UNREADABLE === v.unreadable, F.UNREADABLE)
  for (const c of v.crypto) {
    const s = await F.encryptFile(uh(c.plain_hex), c.key, c.nonce)
    check(`dm-files crypto: ${c.note}`, hx(s.cipher) === c.ciphertext_hex && s.x === c.x && s.ox === c.ox, { x: s.x, ox: s.ox })
  }
  const others = (r) => r.tags.filter((t) => t[0] === 'p').map((t) => t[1])
  const meta = (r) => { const g = (k) => r.tags.find((t) => t[0] === k)?.[1]; return { mime: g('file-type'), size: Number(g('size')), name: g('name'), dim: g('dim'), blurhash: g('blurhash'), thumb: g('thumb'), thumbNonce: g('thumb-nonce'), duration: g('duration') === undefined ? undefined : Number(g('duration')), group: g('group') } }
  const sendCase = async (name, c) => {
    const g = (k) => c.rumor.tags.find((t) => t[0] === k)?.[1]
    const s = await F.encryptFile(uh(c.plain_hex), g('decryption-key'), g('decryption-nonce'))
    const tags = F.fileTags(others(c.rumor), s, meta(c.rumor))
    check(`dm-files send ${name}: ciphertext, tags in order`, hx(s.cipher) === c.ciphertext_hex && eq(tags, c.rumor.tags), { tags })
    if (c.thumb) { const t = await F.encryptThumb(uh(c.thumb.plain_hex), g('decryption-key'), c.thumb.nonce); check(`dm-files send ${name}: thumbnail`, hx(t.cipher) === c.thumb.ciphertext_hex) }
  }
  for (const k of ['picture', 'file', 'audio']) await sendCase(k, v.send[k])
  for (const [i, f] of v.send.group.files.entries()) await sendCase(`group ${i + 1}`, f)
  for (const c of v.read) {
    const r = await F.openFile(c.tags, uh(c.downloaded_hex), !!c.thumb)
    const got = r.ok ? { ok: true, plain_hex: hx(r.plain), ...(c.expect.mime ? { mime: r.mime } : {}) } : { ok: false }
    check(`dm-files read: ${c.note}`, eq(got, c.expect), got)
  }
  for (const n of v.names ?? []) { const got = F.shownName(n.name, n.mime); check(`dm-files name: ${JSON.stringify(n.name)} (${n.mime})`, got === n.shown, got) }
  const bs = F.bubbles(v.bubbles.rumors).map((b) => ({ files: b.files.map((r) => r.id), caption: b.caption?.id ?? null }))
  check('dm-files bubbles', eq(bs, v.bubbles.expect), bs)
}


// sealed.json (collective-belfry docs/sealed-vectors.json, docs/sealed.md v1): chains, wraps, the
// size ceiling and history packing, key hand-over, the link proof, shared history, ready
{
  const S = M.S
  const sv = vec('sealed.json')
  const hex = (b) => Buffer.from(b).toString('hex')
  const unhex = (h) => Uint8Array.from(Buffer.from(h, 'hex'))
  const h = (s) => Uint8Array.from(createHash('sha256').update('belfry sealed vectors/' + s).digest())
  const K = Object.fromEntries(Object.entries(sv.keys).map(([n, k]) => [n, { secret: unhex(k.secret), pubkey: k.pubkey }]))
  const chainCase = Object.fromEntries(sv.chains.map((c) => [c.name, c]))
  const chainOf = (ref) => { if (!ref) return null; const c = typeof ref === 'string' ? chainCase[ref] : ref; return c ? S.validChain(c.anchor, c.rosters) : null }
  const sum = (c) => c ? { valid_ids: c.rosters.map((r) => r.id), head: S.headOf(c).id, devices: S.headOf(c).devices, lb: S.headOf(c).lb, ready: S.isReady(c) } : { valid_ids: [], head: null, devices: [], lb: null, ready: false }
  for (const c of sv.chains) { const got = sum(S.validChain(c.anchor, c.rosters, c.pinned_head)); check(`sealed chain: ${c.name}`, eq(got, c.expected), { got, want: c.expected }) }
  const pickR = (r) => r && { pubkey: r.pubkey, created_at: r.created_at, kind: r.kind, tags: r.tags, content: r.content, id: r.id }
  for (const c of sv.wraps) {
    let got
    try {
      const { seal, rumor } = S.openSealed(unhex(c.letterbox_secret), c.wrap)
      const author = chainOf(c.author_chain ?? c.author_chain_events)
      got = { opens: true, seal_pubkey: seal.pubkey, rumor: pickR(rumor), accepted: S.accept(author, seal, rumor, c.belfry) }
    } catch { got = { opens: false } }
    const want = c.expected.opens ? { ...c.expected, rumor: pickR(c.expected.rumor) } : c.expected
    check(`sealed wrap: ${c.name}`, eq(got, want), { got, want })
  }
  const T0 = sv.fixed.T0, AUX = new Uint8Array(32)
  const fixed = (name, sealAt, wrapAt) => ({ sealAt, wrapAt, oneTime: h('once/' + name), sealNonce: h('nonce/seal/' + name), wrapNonce: h('nonce/wrap/' + name), aux: AUX })
  for (const c of sv.ceiling.cases) {
    const r = S.withId({ ...c.rumor, content: c.rumor.content.repeat.repeat(c.rumor.content.times) })
    const got = { rumor_id: r.id, rumor_json_bytes: S.rumorBytes(r), may_seal: S.fits(r) }
    const want = { rumor_id: c.rumor_id, rumor_json_bytes: c.rumor_json_bytes, may_seal: c.may_seal }
    if (c.may_seal) {
      const w = S.sealToLetterbox(K.alice_phone.secret, r, K.bob.pubkey, K.bob_lb1.pubkey, fixed(c.name, T0, T0))
      const { seal } = S.openWrap(K.bob_lb1.secret, w)
      Object.assign(got, { seal_content_chars: seal.content.length, wrap_content_chars: w.content.length, wrap_id: w.id })
      Object.assign(want, { seal_content_chars: c.seal_content_chars, wrap_content_chars: c.wrap_content_chars, wrap_id: c.wrap_id })
    } else { let threw = false; try { S.sealToLetterbox(K.alice_phone.secret, r, K.bob.pubkey, K.bob_lb1.pubkey) } catch { threw = true }; got.refused = threw; want.refused = true }
    check(`sealed ceiling: ${c.name}`, eq(got, want), { got, want })
  }
  {
    const hp = sv.ceiling.history_packing
    const rumors = []
    for (let i = 0; i < 300; i++) {
      const from = i % 2 ? 'alice' : 'bob', to = i % 2 ? 'bob' : 'alice'
      rumors.push(S.withId({ pubkey: K[from].pubkey, created_at: T0 + i, kind: 14, tags: [['p', K[to].pubkey]], content: `Message ${i}: "quoted" \\ and é` }))
      if (i === 149) rumors.push(S.withId({ pubkey: K.bob.pubkey, created_at: T0 + 150, kind: 14, tags: [['p', K.alice.pubkey]], content: 'x'.repeat(39700) }))
    }
    const { packs, leftOut } = S.packHistory(K.bob.pubkey, K.alice.pubkey, rumors, T0 + 800)
    const got = { rumors_per_8564: packs.map((p) => JSON.parse(p.content).rumors.length), json_bytes_per_8564: packs.map((p) => S.rumorBytes(p)), ids_of_8564: packs.map((p) => p.id), left_out: leftOut }
    check(`sealed history packing: ${hp.name}`, eq(got, hp.expected), { got, want: hp.expected })
  }
  if (sv.ceiling.keys_packing) {
    const kp = sv.ceiling.keys_packing
    const many = Array.from({ length: 600 }, (_, i) => { const sk = h('key/many-lb-' + i); return { lb: S.getPub(sk), secret: hex(sk) } })
    const chunks = S.keyChunks(K.alice_phone.pubkey, K.alice.pubkey, many, T0 + 600)
    const rumors = chunks.map((k) => S.keysRumor(K.alice_phone.pubkey, K.alice.pubkey, k, T0 + 600))
    const got = { keys_per_8561: chunks.map((k) => k.length), json_bytes_per_8561: rumors.map((r) => S.rumorBytes(r)), ids_of_8561: rumors.map((r) => r.id) }
    const want = { keys_per_8561: kp.expected.keys_per_8561, json_bytes_per_8561: kp.expected.json_bytes_per_8561, ids_of_8561: kp.expected.ids_of_8561 }
    check(`sealed keys packing: ${kp.name}`, eq(got, want), { got, want })
  }
  for (const c of sv.keys_handover) {
    let got
    try {
      const { seal, rumor } = S.openKeys(unhex(c.device_secret), c.wrap)
      got = { giver: seal.pubkey, rumor: pickR(rumor), keys: S.acceptKeys(c.account, [chainOf(c.chain)].filter(Boolean), seal, rumor) }
    } catch (e) { got = { error: String(e.message) } }
    const want = { ...c.expected, rumor: pickR(c.expected.rumor) }
    check(`sealed keys: ${c.name}`, eq(got, want), { got, want })
  }
  for (const c of sv.link) {
    const got = { link_for_this_device: S.linkProof(c.link_secret, c.request.pubkey), valid: S.linkOk(c.link_secret, c.request) }
    check(`sealed link: ${c.name}`, eq(got, c.expected), { got, want: c.expected })
  }
  for (const c of sv.history) {
    let got
    try {
      const { seal, rumor } = S.openSealed(unhex(c.letterbox_secret), c.wrap)
      const ok = S.accept(S.validChain(c.sharer_chain_events.anchor, c.sharer_chain_events.rosters), seal, rumor)
      got = { sharer: rumor.pubkey, seal_pubkey: seal.pubkey, added_ids: ok ? S.historyRumors(rumor, c.receiver).map((r) => r.id) : [] }
    } catch (e) { got = { error: String(e.message) } }
    check(`sealed history: ${c.name}`, eq(got, c.expected), { got, want: c.expected })
  }
  for (const c of sv.ready.cases) { const got = { may_carry_ready: S.mayCarryReady(c.sessions, c.roster_devices, sv.ready.now) }; check(`sealed ready: ${c.name}`, eq(got, c.expected), { got, want: c.expected }) }
  // follow (pins: none / moved / ignored_back / switch / fresh_start) and trust (who is answered without a prompt)
  const pinOut = (p) => p && { root: p.root, head: p.head, path: p.path, anchored: p.anchored, anchored_head: p.anchored_head, trusted: p.trusted, switch_head: p.switch_head, prev_root: p.prev_root }
  const runSteps = (c) => {
    let pin = c.pin, chainNow = null, fails = []
    c.steps.forEach((st, i) => {
      const r = S.follow(st.anchor, st.rosters, pin)
      pin = r.pin; chainNow = r.chain
      for (const d of st.allow_locally ?? []) pin = S.allowLocally(pin, d)
      if (st.expected) {
        const got = { event: r.event, pin: pinOut(pin), chain: sum(r.chain) }
        const want = { event: st.expected.event, pin: st.expected.pin, chain: st.expected.chain ?? sum(null) }
        if (!eq(got, want)) fails.push({ step: i, got, want })
      }
    })
    return { pin, chainNow, fails }
  }
  for (const c of sv.follow?.cases ?? []) { const { fails } = runSteps(c); check(`sealed follow: ${c.name}`, !fails.length, fails[0]) }
  for (const c of sv.trust?.cases ?? []) {
    const { pin, chainNow, fails } = runSteps(c)
    const asks = (c.asks ?? []).map((a) => ({ name: a.device_name, got: S.answerWithoutPrompt(chainNow, pin, a.device, a.live_chat_devices, a.request_created_at, a.added_at, a.last_handover_at), want: a.expected }))
    const wrong = asks.filter((a) => a.got !== a.want)
    check(`sealed trust: ${c.name} (${asks.length} asks)`, !fails.length && !wrong.length, fails[0] ?? wrong)
  }
  for (const c of sv.reports?.cases ?? []) {
    const r = S.openReport(c.report, sv.reports.belfry, c.key_chain)
    const got = r.state === 'opened' ? { state: 'opened', body: r.body } : { state: r.state }
    check(`sealed reports: ${c.name}`, eq(got, c.expected), { got, want: c.expected })
  }
  // confirm (§5.2 2b, kind 8563, from belfry-android 921a6c1): "It was me" accepted once for all
  if (sv.confirm) {
    const cf = sv.confirm, cc = S.validChain(cf.chain.anchor, cf.chain.rosters)
    for (const c of cf.cases) {
      const v = cc ? S.confirmOk(cc, cf.account, { pubkey: c.seal_pubkey }, c.rumor) : { ok: 'no' }
      const got = v.ok === 'ok' ? { accepted: true, device: v.device, roster: c.rumor.tags.find((t) => t[0] === 'e')[1] } : { accepted: false }
      check(`sealed confirm: ${c.name}`, eq(got, c.expected), { got, want: c.expected, v })
    }
    const w = cf.wrap
    let got
    try { const { seal, rumor } = S.openSealed(unhex(w.letterbox_secret), w.wrap); const v = S.confirmOk(cc, cf.account, seal, rumor); const ordered = Object.fromEntries(Object.keys(w.expected.rumor).map((k) => [k, rumor[k]])); got = { seal_pubkey: seal.pubkey, rumor: ordered, accepted: v.ok === 'ok' } } /* the fields in the vector's order */ catch (e) { got = String(e) }
    check(`sealed confirm wrap: ${w.name}`, eq(got, w.expected), { got, want: w.expected })
  }
  console.log(bad ? `vectors (with sealed): ${bad} failed` : 'vectors (with sealed): all ok')
}
process.exit(bad ? 1 : 0)
