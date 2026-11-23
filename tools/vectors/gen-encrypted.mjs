// Writes vectors/encrypted.json (docs/encrypted.md with §16): every key, nonce and date fixed, so the
// file is the same on every run. Apps check their reading against it (tools/vectors/check.mjs for the
// web); the expected values come from src/core/enc/core.ts at the time of writing, then reviewed by hand.
//   node tools/vectors/gen-encrypted.mjs
import { build } from 'esbuild'
import { writeFileSync } from 'node:fs'
import { createHash } from 'node:crypto'
import { fileURLToPath } from 'node:url'
import path from 'node:path'
const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '../..')
const out = path.join(root, 'tools/vectors/.enc.mjs')
await build({ entryPoints: [path.join(root, 'src/core/enc/core.ts')], bundle: true, platform: 'node', format: 'esm', outfile: out, logLevel: 'error' })
const E = await import(out + '?' + Date.now())
const { getPublicKey } = await import('nostr-tools')

const h = (s) => createHash('sha256').update(s).digest()
const sk = (name) => new Uint8Array(h('belfry-vector:' + name))
const hex = (u) => Buffer.from(u).toString('hex')
const pub = (name) => getPublicKey(sk(name))
const nonce = (label) => (i) => new Uint8Array(h(`nonce:${label}:${i}`))
const aux = new Uint8Array(32)
const fixed = (label, at, extra = {}) => ({ at, sealAt: at - 100, oneTime: sk('once:' + label), nonce: nonce(label), aux, ...extra })
// signWith lives in sealedcore; reach it through a tiny re-export
const S = await (async () => { const o = path.join(root, 'tools/vectors/.sc.mjs'); await build({ stdin: { contents: "export { signWith } from './src/core/sealedcore'", resolveDir: root, loader: 'ts' }, bundle: true, platform: 'node', format: 'esm', outfile: o, logLevel: 'error' }); return import(o + '?' + Date.now()) })()
const signed = (name, t) => S.signWith(sk(name), t, aux)

const B = 'belfry', A = 'sam', C = 'lina', AG = 'agent'
const keys = { belfry: pub(B), sam: pub(A), lina: pub(C), agent: pub(AG), d1: pub('d1'), d2: pub('d2'), d3: pub('d3'), d4: pub('d4'), c1: pub('c1'), other: pub('other') }
const secrets = Object.fromEntries(['d1', 'd2', 'd3', 'd4', 'c1', 'agent', 'lina'].map((n) => [n, hex(sk(n))]))
const list = (name, account, n, prev, devices, at, readers = [], signer = B, self = false) =>
  signed(signer, { kind: 8570, created_at: at, tags: [['p', account], ['n', String(n)], ...(prev ? [['prev', prev]] : []), ...[...devices].sort().map((d) => ['device', d]), ...(self ? [['device', account, 'self']] : []), ...readers.map((r) => ['device', r, 'reader'])], content: '' })

// ---- lists: Sam's chain ------------------------------------------------------------------------
const L1 = list('L1', keys.sam, 1, null, [keys.d1], 1000)
const L2 = list('L2', keys.sam, 2, L1.id, [keys.d1, keys.d2], 2000)
const L3 = list('L3', keys.sam, 3, L2.id, [keys.d2], 3000, [keys.agent])
const L4 = list('L4', keys.sam, 4, L3.id, [keys.d2, keys.d4], 4000, [keys.agent])
const LC1 = list('LC1', keys.lina, 1, null, [keys.c1], 1500)
const L5 = list('L5', keys.sam, 5, L4.id, [keys.d2, keys.d4, keys.d3], 5000, [keys.agent])
const LA1 = list('LA1', keys.agent, 1, null, [], 1200, [], B, true) // §17.1: an agent with its own key: its key as "self"
const L2fork = list('L2f', keys.sam, 2, L1.id, [keys.d1, keys.d3], 2050)
const bad = {
  other_signer: list('x1', keys.sam, 2, L1.id, [keys.d3], 2100, [], 'other'),
  wrong_prev_fork: list('x2', keys.sam, 2, keys.other, [keys.d3], 2200),
  duplicate_device: signed(B, { kind: 8570, created_at: 2300, tags: [['p', keys.sam], ['n', '2'], ['prev', L1.id], ['device', keys.d1], ['device', keys.d1]], content: '' }),
  too_many: list('x4', keys.sam, 2, L1.id, Array.from({ length: 21 }, (_, i) => pub('many' + i)), 2400),
  bad_reader_tag: signed(B, { kind: 8570, created_at: 2500, tags: [['p', keys.sam], ['n', '2'], ['prev', L1.id], ['device', keys.d1, 'owner']], content: '' }),
  n1_with_prev: signed(B, { kind: 8570, created_at: 900, tags: [['p', keys.sam], ['n', '1'], ['prev', keys.other], ['device', keys.d3]], content: '' }),
  content_not_empty: signed(B, { kind: 8570, created_at: 2600, tags: [['p', keys.sam], ['n', '2'], ['prev', L1.id]], content: 'x' }),
  self_with_another_device: signed(B, { kind: 8570, created_at: 1250, tags: [['p', keys.agent], ['n', '2'], ['prev', keys.other], ['device', keys.agent, 'self'], ['device', keys.d3]], content: '' }),
  twenty_and_two_readers: list('x20', keys.sam, 2, L1.id, Array.from({ length: 20 }, (_, i) => pub('twenty' + i)), 2800, [pub('r1'), pub('r2')]),
  unknown_marker_skipped: signed(B, { kind: 8570, created_at: 2900, tags: [['p', keys.sam], ['n', '2'], ['prev', L1.id], ['device', keys.d1], ['device', keys.d3, 'later-marker']], content: '' }),
  self_not_the_account: signed(B, { kind: 8570, created_at: 2700, tags: [['p', keys.sam], ['n', '2'], ['prev', L1.id], ['device', keys.d1, 'self']], content: '' }),
}
const L6gap = list('L6', keys.sam, 6, keys.other, [keys.d2], 6000)
const chainEvents = [L3, bad.other_signer, L1, bad.wrong_prev_fork, L2, LC1]
const chainView = (c) => c && { lists: c.lists.map((l) => l.id), current: E.current(c).id, devices: E.current(c).devices, readers: E.current(c).readers, gaps: c.gaps, fork: c.fork, invalidAbove: c.invalidAbove }
const listCases = {
  valid: Object.fromEntries(Object.entries({ L1, L2, L3, L4, L5, LC1, LA1, ...bad }).map(([k, e]) => { const l = E.parseList(e, keys.belfry); return [k, { event: e, valid: !!l, ...(l ? { devices: l.devices.length, readers: l.readers.length } : {}) }] })),
  chains: [
    { name: 'sam: three lists; another signer ignored; a list 2 whose prev names no list is a fork (warn), the lower id kept', account: keys.sam, events: chainEvents, expect: chainView(E.chainOf(chainEvents, keys.belfry, keys.sam)) },
    { name: 'sam: the three lists alone: no fork, no gap', account: keys.sam, events: [L3, L1, L2], expect: chainView(E.chainOf([L3, L1, L2], keys.belfry, keys.sam)) },
    { name: 'two valid lists 2: a fork', account: keys.sam, events: [L1, L2, L2fork, L3], expect: chainView(E.chainOf([L1, L2, L2fork, L3], keys.belfry, keys.sam)) },
    { name: 'list 6 without 4 and 5: current is 6 (the highest n), gaps', account: keys.sam, events: [L1, L2, L3, L6gap], expect: chainView(E.chainOf([L1, L2, L3, L6gap], keys.belfry, keys.sam)) },
    { name: 'lists 2 and 3 without 1: current is 3, gaps, reading not blocked', account: keys.sam, events: [L2, L3], expect: chainView(E.chainOf([L2, L3], keys.belfry, keys.sam)) },
    { name: 'the agent: its own key as self', account: keys.agent, events: [LA1], expect: chainView(E.chainOf([LA1], keys.belfry, keys.agent)) },
    { name: 'no list at all: null (Held)', account: keys.lina, events: [L1], expect: chainView(E.chainOf([L1], keys.belfry, keys.lina)) },
  ],
}
const c3 = E.chainOf([L1, L2, L3], keys.belfry, keys.sam)
const c5 = E.chainOf([L1, L2, L3, L4, L5], keys.belfry, keys.sam)
const changeCases = { lists: [L1, L2, L3, L4, L5], expect: E.changes(c5) }
const before = [[keys.d1, keys.d2], [keys.d2, keys.d4], [keys.d4, keys.d2], [keys.d3, keys.d4], [keys.d2, keys.d2], [keys.agent, keys.d4], [keys.d2, keys.other]].map(([a, b]) => ({ older: a, device: b, expect: E.listedBefore(c5, a, b) }))
const nul = (v) => (v === undefined ? null : v)
const signsAt = [[keys.d1, 2500], [keys.d1, 3600], [keys.d1, 3601], [keys.d2, 999999], [keys.agent, 3500], [keys.d3, 2500]].map(([d, at]) => ({ device: d, at, expect: nul(E.signsAt(c3, d, at)) }))
const cGap = E.chainOf([L1, L2, L4], keys.belfry, keys.sam) // list 3 missing: what dropped d1 isn't known
const signsAtGap = [[keys.d1, 3500], [keys.d2, 3500], [keys.d4, 4500]].map(([d, at]) => ({ device: d, at, expect: nul(E.signsAt(cGap, d, at)) }))
// the review (P3): d1 dropped by list 3 (3000), listed again by R4 (4000): not accepted for 3601–3999
const R4 = list('R4', keys.sam, 4, L3.id, [keys.d1, keys.d2], 4000, [keys.agent])
const cRe = E.chainOf([L1, L2, L3, R4], keys.belfry, keys.sam)
const signsAtRelisted = [[keys.d1, 3500], [keys.d1, 3700], [keys.d1, 4000], [keys.d1, 4500]].map(([d, at]) => ({ device: d, at, expect: nul(E.signsAt(cRe, d, at)) }))
// the review (P2.3): a device on no list known here, dated at or after the newest list: its list may be behind (wait)
// §21: d2 first listed by L2 (2000): an outer date up to 5 minutes before it counts (a slow clock)
const signsAtSlow = [[keys.d2, 1800], [keys.d2, 1700], [keys.d2, 1699], [keys.d2, 1600]].map(([d, at]) => ({ device: d, at, expect: nul(E.signsAt(c3, d, at)) }))
const groupKeySlow = [1800, 1600].map((at) => ({ maker: keys.d2, account: keys.sam, at, now: 5000, lists: [L1, L2, L3], expect: E.acceptGroupKey({ k: '1'.repeat(64), K: 'a'.repeat(64), group: 'pod-garden-7f3a', maker: keys.d2, account: keys.sam, at, set: 'b'.repeat(64) }, c3, 5000) }))
const signsAtBehind = [[keys.d4, 3500], [keys.d4, 2500], [keys.d3, 3000]].map(([d, at]) => ({ device: d, at, expect: nul(E.signsAt(c3, d, at)) }))
// the review (P2.7): a newer list that isn't valid (self beside a device) → invalidAbove; a fork is reported
const L4bad = signed(B, { kind: 8570, created_at: 4000, tags: [['p', keys.sam], ['n', '4'], ['prev', L3.id], ['device', keys.sam, 'self'], ['device', keys.d2]], content: '' })
const invalidAbove = { events: [L1, L2, L3, L4bad], expect: chainView(E.chainOf([L1, L2, L3, L4bad], keys.belfry, keys.sam)), invalid_above: E.chainOf([L1, L2, L3, L4bad], keys.belfry, keys.sam).invalidAbove }
const listedAtGap = [[keys.d1, 3500], [keys.d2, 2500], [keys.d1, 2500]].map(([d, at]) => ({ device: d, at, expect: nul(E.listedAt(cGap, d, at)) }))
const listedAt = [[keys.d1, 1500], [keys.d1, 3000], [keys.d1, 3600], [keys.d1, 3601], [keys.d2, 1999], [keys.d2, 2000], [keys.d2, 900]].map(([d, at]) => ({ device: d, at, expect: nul(E.listedAt(c3, d, at)) }))

// ---- names -------------------------------------------------------------------------------------
const kp = E.newKey(sk('conv-key-1'))
const aliases = [[kp.K, keys.sam], [kp.K, keys.lina]].map(([K, a]) => ({ K, account: a, expect: E.alias(K, a) }))
const lh = { lists: [{ account: keys.lina, id: LC1.id }, { account: keys.sam, id: L3.id }], expect: E.listsHash([{ account: keys.lina, id: LC1.id }, { account: keys.sam, id: L3.id }]) }

// ---- a people key: Sam's d2 makes it for Sam (L3) and Lina (LC1) ---------------------------------
const kr = E.keyRumor(keys.d2, kp.k, [{ account: keys.sam, list: L3.id }, { account: keys.lina, list: LC1.id }], 3100)
const envs = E.peopleEnvelopes(sk('d2'), kr, [{ account: keys.sam, devices: E.receivers(E.current(c3)) }, { account: keys.lina, devices: [keys.c1] }], fixed('env1', 3100))
const allLists = [L1, L2, L3, LC1, LA1]
const NOW = 5000
// §20: three answers: a chain, 'service' (the belfry key), or undefined (no list: Held)
const chainsOf = (events) => (a) => (a === keys.belfry ? E.SERVICE : E.chainOf(events, keys.belfry, a) ?? undefined)
const openAs = (dev, env) => { try { const o = E.openPeopleEnvelope(sk(dev), env); return o ? { rumor_id: o.rumor.id, K: o.rumor.tags.find((t) => t[0] === 'K')[1] } : null } catch (e) { return { error: true } } }
const variant = (fn) => { const r = JSON.parse(JSON.stringify(kr)); fn(r); return E.withId(r) }
const peopleKey = {
  key_rumor: kr, envelopes: envs,
  open: [['c1', 1], ['d2', 0], ['agent', 0], ['d1', 0], ['d3', 0]].map(([dev, i]) => ({ device: dev, envelope: i, expect: openAs(dev, envs[i]) })),
  accept: [
    { name: 'Lina takes it: maker d2 on the list named for Sam', receiver: keys.lina, rumor: kr },
    { name: 'K is not the public half of the secret', receiver: keys.lina, rumor: variant((r) => { r.tags[0][1] = keys.other }) },
    { name: 'the receiver is not one of its people', receiver: keys.agent, rumor: kr },
    { name: 'maker d1 is not on the list named for Sam (L3)', receiver: keys.lina, rumor: variant((r) => { r.pubkey = keys.d1 }) },
    { name: 'the named list is not loaded yet: wait', receiver: keys.lina, rumor: variant((r) => { r.tags = r.tags.map((t) => (t[0] === 'list' && t[1] === keys.sam ? ['list', keys.sam, keys.other] : t)) }) },
    { name: 'the agent (own key, listed as self) makes a key for itself and Sam: ok (§20)', receiver: keys.sam, rumor: E.keyRumor(keys.agent, E.newKey(sk('agent-key')).k, [{ account: keys.agent, list: LA1.id }, { account: keys.sam, list: L3.id }], 3150) },
    { name: 'dated more than 15 minutes after now (5000): refused', receiver: keys.lina, rumor: variant((r) => { r.created_at = NOW + 901 }) },
    { name: 'dated exactly 15 minutes after now: taken', receiver: keys.lina, rumor: variant((r) => { r.created_at = NOW + 900 }) },
  ].map((c) => ({ ...c, lists: allLists, now: NOW, expect: E.acceptKeyRumor(c.rumor, c.receiver, chainsOf(allLists), NOW) })),
}

// ---- a group key: 300 devices, split over several envelope events -------------------------------
const gk = E.newKey(sk('group-key-1'))
const many = Array.from({ length: 300 }, (_, i) => 'g' + i)
const gdevs = [keys.d2, keys.c1, ...many.map(pub)]
const gtempl = E.groupEnvelopes(sk('d2'), gk.k, 'pod-garden-7f3a', gdevs, E.listsHash([{ account: keys.sam, id: L3.id }, { account: keys.lina, id: LC1.id }]), fixed('genv', 3200))
const genvs = gtempl.map((t) => signed(A, t))
const findIn = (dev) => { for (const [i, e] of genvs.entries()) { try { const g = E.openGroupEnvelope(sk(dev), e, 'pod-garden-7f3a'); if (g) return { envelope: i, K: g.K, maker: g.maker } } catch { return { error: true } } } return null }
const gkey = E.openGroupEnvelope(sk('c1'), genvs[0], 'pod-garden-7f3a')
const groupKey = {
  group: 'pod-garden-7f3a', envelopes: genvs, max_content_bytes: 60000,
  expect_events: genvs.length, expect_each_under: genvs.every((e) => Buffer.byteLength(e.content) <= 60000),
  // §18.4: the entries' set = the hash of every device key with an entry, over all events; one event alone doesn't match it
  expect_set: gkey.set, expect_set_all_events: E.deviceSet(E.entryKeys(genvs)) === gkey.set, expect_set_first_event_only: E.deviceSet(E.entryKeys([genvs[0]])) === gkey.set,
  find: [['c1'], ['d2'], ['g299'], ['g150'], ['d4']].map(([d]) => ({ device: d, expect: findIn(d) })),
  wrong_group: { device: 'c1', group: 'pod-other', expect: (() => { try { E.openGroupEnvelope(sk('c1'), genvs[0], 'pod-other'); return 'opened' } catch { return 'error' } })() },
  accept: [[keys.d1, 3200], [keys.d1, 3600], [keys.d1, 3601], [keys.d2, 3200], [keys.d2, NOW + 901]].map(([maker, at]) => ({ maker, account: keys.sam, at, now: NOW, lists: [L1, L2, L3], expect: E.acceptGroupKey({ ...gkey, maker, at }, c3, NOW) })),
}

// ---- messages ----------------------------------------------------------------------------------
const rumor = (author, at, tags, content, kind = 14) => E.withId({ kind, pubkey: author, created_at: at, tags, content })
const r1 = rumor(keys.sam, 3300, [['p', keys.lina]], 'Bread on Saturday?')
const msgs = E.peopleMessage(sk('d2'), r1, kp.K, [keys.sam, keys.lina], keys.sam, fixed('m1', 3300))
const keyPeople = E.peopleId(kr.tags.filter((t) => t[0] === 'p').map((t) => t[1]))
const openMsg = (m, k = kp.k) => { try { const o = E.openPeopleMessage(k, m); return { seal_signer: o.seal.pubkey, rumor_id: o.rumor.id } } catch { return { error: true } } }
const judge = (signer, r, label, outerAt = r.created_at) => { const m = E.peopleMessage(sk(signer), r, kp.K, [keys.lina], keys.sam, fixed(label, outerAt, { sealAt: outerAt }))[0]; const o = E.openPeopleMessage(kp.k, m); return { message: m, expect: E.acceptPeopleMessage(m, o.seal, o.rumor, keyPeople, chainsOf(allLists)(r.pubkey)) } }
const peopleMsgs = {
  K: kp.K, k: kp.k, key_people: keyPeople, rumor: r1, messages: msgs,
  expect_tags: msgs.map((m) => m.tags),
  open: msgs.map((m) => ({ expect: openMsg(m) })),
  open_wrong_key: openMsg(msgs[1], E.newKey(sk('nope')).k),
  accept: [
    { name: 'd2 for Sam: ok', ...judge('d2', r1, 'a1') },
    { name: 'd1 dropped at 3000, writing at 3500 (grace): ok', ...judge('d1', rumor(keys.sam, 3500, [['p', keys.lina]], 'late'), 'a2') },
    { name: 'd1 writing at 3700, after the grace: no', ...judge('d1', rumor(keys.sam, 3700, [['p', keys.lina]], 'too late'), 'a3') },
    { name: 'the agent, a reader, signing as Sam: no', ...judge('agent', rumor(keys.sam, 3300, [['p', keys.lina]], 'as Sam'), 'a4') },
    { name: 'd1: rumor dated 3500 but the outer event 3700 (after the grace): judged by the outer date (§18.2): no', ...judge('d1', rumor(keys.sam, 3500, [['p', keys.lina]], 'backdated'), 'a8', 3700) },
    { name: 'a rumor newer than its outer event by more than 5 minutes: no', ...judge('d2', rumor(keys.sam, 3601, [['p', keys.lina]], 'from the future'), 'a9', 3300) },
    { name: 'a rumor newer than its outer event by exactly 5 minutes: ok', ...judge('d2', rumor(keys.sam, 3600, [['p', keys.lina]], 'just in'), 'a10', 3300) },
    { name: 'a rumor for other people than the key’s: no', ...judge('d2', rumor(keys.sam, 3300, [['p', keys.agent]], 'elsewhere'), 'a5') },
    { name: 'the agent (own key, listed as self) writes as itself: ok', message: E.peopleMessage(sk('agent'), rumor(keys.agent, 3300, [['p', keys.sam]], 'hi'), kp.K, [keys.sam], keys.agent, fixed('a6', 3300))[0],
      expect: (() => { const m = E.peopleMessage(sk('agent'), rumor(keys.agent, 3300, [['p', keys.sam]], 'hi'), kp.K, [keys.sam], keys.agent, fixed('a6', 3300))[0]; const o = E.openPeopleMessage(kp.k, m); return E.acceptPeopleMessage(m, o.seal, o.rumor, E.peopleId([keys.agent, keys.sam]), chainsOf(allLists)(keys.agent)) })(), key_people: E.peopleId([keys.agent, keys.sam]) },
    { name: 'Lina has no list known; her account key signs the seal: never accepted, kept (wait) (§20)', message: (() => E.peopleMessage(sk('lina'), rumor(keys.lina, 3300, [['p', keys.sam]], 'as my identity'), kp.K, [keys.sam], keys.lina, fixed('a11', 3300))[0])(), key_people: keyPeople, lists_without_lina: true,
      expect: (() => { const m = E.peopleMessage(sk('lina'), rumor(keys.lina, 3300, [['p', keys.sam]], 'as my identity'), kp.K, [keys.sam], keys.lina, fixed('a11', 3300))[0]; const o = E.openPeopleMessage(kp.k, m); return E.acceptPeopleMessage(m, o.seal, o.rumor, keyPeople, chainsOf([L1, L2, L3])(keys.lina)) })() },
    { name: 'the same once Lina’s list is here (her key is not on it): no', message: (() => E.peopleMessage(sk('lina'), rumor(keys.lina, 3300, [['p', keys.sam]], 'as my identity'), kp.K, [keys.sam], keys.lina, fixed('a11', 3300))[0])(), key_people: keyPeople,
      expect: (() => { const m = E.peopleMessage(sk('lina'), rumor(keys.lina, 3300, [['p', keys.sam]], 'as my identity'), kp.K, [keys.sam], keys.lina, fixed('a11', 3300))[0]; const o = E.openPeopleMessage(kp.k, m); return E.acceptPeopleMessage(m, o.seal, o.rumor, keyPeople, chainsOf(allLists)(keys.lina)) })() },
    { name: 'the belfry key (a service key, its own device) signs as itself: ok', message: (() => E.peopleMessage(sk(B), rumor(keys.belfry, 3300, [['p', keys.sam]], 'mail'), kp.K, [keys.sam], keys.belfry, fixed('a12', 3300))[0])(), key_people: E.peopleId([keys.belfry, keys.sam]),
      expect: (() => { const m = E.peopleMessage(sk(B), rumor(keys.belfry, 3300, [['p', keys.sam]], 'mail'), kp.K, [keys.sam], keys.belfry, fixed('a12', 3300))[0]; const o = E.openPeopleMessage(kp.k, m); return E.acceptPeopleMessage(m, o.seal, o.rumor, E.peopleId([keys.belfry, keys.sam]), chainsOf(allLists)(keys.belfry)) })() },
    { name: 'an author with no list known: wait (Held, never the account key)', message: E.peopleMessage(sk('other'), rumor(keys.other, 3300, [['p', keys.sam]], 'hi'), kp.K, [keys.sam], keys.other, fixed('a7', 3300))[0],
      expect: (() => { const m = E.peopleMessage(sk('other'), rumor(keys.other, 3300, [['p', keys.sam]], 'hi'), kp.K, [keys.sam], keys.other, fixed('a7', 3300))[0]; const o = E.openPeopleMessage(kp.k, m); return E.acceptPeopleMessage(m, o.seal, o.rumor, E.peopleId([keys.other, keys.sam]), chainsOf(allLists)(keys.other)) })(), key_people: E.peopleId([keys.other, keys.sam]) },
  ],
}
const gr = rumor(keys.sam, 3400, [['h', 'pod-garden-7f3a']], 'Seeds are in', 9)
const gm = signed(A, E.groupMessage(sk('d2'), gr, gk.K, 'pod-garden-7f3a', fixed('gm', 3400), [keys.lina]))
const gmByLina = signed(C, E.groupMessage(sk('d2'), gr, gk.K, 'pod-garden-7f3a', fixed('gm2', 3400)))
const grOther = rumor(keys.sam, 3400, [['h', 'pod-other']], 'wrong room', 9)
const gmOther = signed(A, E.groupMessage(sk('d2'), grOther, gk.K, 'pod-garden-7f3a', fixed('gm3', 3400)))
const gjudge = (m) => { const o = E.openGroupMessage(gk.k, m); return E.acceptGroupMessage(m, o.seal, o.rumor, 'pod-garden-7f3a', c3) }
const groupMsgs = { K: gk.K, k: gk.k, group: 'pod-garden-7f3a', rumor_id: gr.id, cases: [
  { name: 'sent by Sam’s account, sealed by d2, mentioning Lina outside (§17.14): ok', message: gm, expect: gjudge(gm), expect_tags: gm.tags },
  { name: 'outer event by Lina for a rumor by Sam: no', message: gmByLina, expect: gjudge(gmByLina) },
  { name: 'rumor for another group: no', message: gmOther, expect: gjudge(gmOther) },
] }

// ---- packs: d2 shares with the new device d4 (list 4) ----------------------------------------------
const c4 = c5
const v1 = E.newKey(sk('letterbox-1'))
const packKeys = [{ kind: 'people', rumor: kr }, { kind: 'group', k: gk.k, K: gk.K, group: 'pod-garden-7f3a' }, { kind: 'v1', secret: v1.k, lb: v1.K }]
const p1 = E.packs(sk('d2'), keys.sam, keys.d4, packKeys, fixed('p1', 4100))
const badKey = { kind: 'group', k: gk.k, K: keys.other, group: 'pod-garden-7f3a' }
const p2 = E.packs(sk('d2'), keys.sam, keys.d4, [...packKeys, badKey], fixed('p2', 4100))
const p3 = E.packs(sk('c1'), keys.sam, keys.d4, packKeys, fixed('p3', 4100))
const manyV1 = Array.from({ length: 400 }, (_, i) => { const x = E.newKey(sk('lb' + i)); return { kind: 'v1', secret: x.k, lb: x.K } })
const p4 = E.packs(sk('d2'), keys.sam, keys.d4, manyV1, fixed('p4', 4100))
const p5 = E.packs(sk('d3'), keys.sam, keys.d4, packKeys, fixed('p5', 5100))
const p6 = E.packs(sk('agent'), keys.sam, keys.d4, packKeys, fixed('p6', 5100))
const packView = (p) => { const o = E.openPack(sk('d4'), keys.sam, p, c4); return o && { from: o.from, keys: o.keys.map((k) => k.kind === 'people' ? { kind: 'people', rumor_id: k.rumor.id } : k.kind === 'group' ? { kind: 'group', K: k.K, group: k.group } : { kind: 'v1', lb: k.lb }) } }
const packCases = { lists: [L1, L2, L3, L4, L5], cases: [
  { name: 'from d2 to d4: a people key, a group key, a version-1 secret', pack: p1[0], expect: packView(p1[0]) },
  { name: 'a key whose K is not its secret’s is left out', pack: p2[0], expect: packView(p2[0]) },
  { name: 'from a device of another account: refused', pack: p3[0], expect: packView(p3[0]) },
  { name: 'from d3, listed after d4 (§17.6): refused', pack: p5[0], expect: packView(p5[0]) },
  { name: 'from the agent, a reader: refused', pack: p6[0], expect: packView(p6[0]) },
], split: { keys: 400, expect_packs: p4.length, expect_total_keys: p4.reduce((n, p) => n + (packView(p)?.keys.length ?? 0), 0) } }

// ---- picking a key -------------------------------------------------------------------------------
const lines = E.peopleLists([{ account: keys.sam, id: L3.id }, { account: keys.lina, id: LC1.id }])
const conv1 = E.peopleId([keys.sam, keys.lina])
const held = [
  { K: 'a'.repeat(64), k: '1'.repeat(64), conv: conv1, mine: true, lists: lines, at: 1000 },
  { K: 'b'.repeat(64), k: '2'.repeat(64), conv: conv1, mine: true, lists: lines, at: 2000 },
  { K: 'c'.repeat(64), k: '3'.repeat(64), conv: conv1, mine: true, lists: 'older lists', at: 3000 },
  { K: 'd'.repeat(64), k: '4'.repeat(64), conv: conv1, mine: false, lists: lines, at: 2400 }, // Lina's: opened, never sent with (§17.8)
  { K: 'e'.repeat(64), k: '5'.repeat(64), conv: 'g:pod-garden-7f3a', mine: false, group: true, lists: 'set-now', at: 2600 }, // another member's group key with the right set: adopted (§18.7)
  { K: 'f'.repeat(64), k: '6'.repeat(64), conv: 'g:pod-garden-7f3a', mine: false, group: true, service: true, lists: 'set-now', at: 2700 }, // the groups key's: never
  { K: '9'.repeat(64), k: '7'.repeat(64), conv: 'g:pod-garden-7f3a', mine: true, group: true, lists: 'set-before', at: 2800 }, // mine, but another set
]
const pick = [
  { name: 'the newest for exactly these lists', now: 2500, expect: E.pickKey(held, conv1, lines, 2500)?.K ?? null },
  { name: 'older than 30 days: make a new one', now: 2000 + 30 * 86400, expect: E.pickKey(held, conv1, lines, 2000 + 30 * 86400)?.K ?? null },
  { name: 'lists changed: make a new one', now: 2500, lists: 'new lists', expect: E.pickKey(held, conv1, 'new lists', 2500)?.K ?? null },
  { name: 'group: another member’s key whose entry set is this device set (the groups key’s newer one never)', now: 3000, conv: 'g:pod-garden-7f3a', lists: 'set-now', expect: E.pickKey(held, 'g:pod-garden-7f3a', 'set-now', 3000)?.K ?? null },
  { name: 'group: no key for this set: make one', now: 3000, conv: 'g:pod-garden-7f3a', lists: 'set-other', expect: E.pickKey(held, 'g:pod-garden-7f3a', 'set-other', 3000)?.K ?? null },
]

// ---- asks and answers ----------------------------------------------------------------------------
const ask4 = E.ask(sk('d4'), keys.sam, 4050, aux)
const askC = E.ask(sk('c1'), keys.sam, 4050, aux)
const ans1 = E.answer(sk('d2'), keys.sam, { ask: ask4.id }, 'shared', 4200, aux)
const ans2 = E.answer(sk('d2'), keys.sam, { device: keys.d4 }, 'removed', 4200, aux)
const ans3 = E.answer(sk('d4'), keys.sam, { device: keys.d2 }, 'removed', 4200, aux)
const ans5 = E.answer(sk('d1'), keys.sam, { device: keys.d4 }, 'shared', 4200, aux) // d1 was removed by list 3
const ans6 = E.answer(sk('d2'), keys.sam, { ask: ask4.id }, 'shared', 4200, aux)
const ans4 = E.answer(sk('agent'), keys.sam, { device: keys.d4 }, 'shared', 4200, aux)
const asks = { lists: [L1, L2, L3, L4, L5], cases: [
  { name: 'd4 asks', event: ask4, expect: E.readAsk(ask4, keys.sam, c4) },
  { name: 'a device not on Sam’s list asks: ignored', event: askC, expect: E.readAsk(askC, keys.sam, c4) },
  { name: 'd2 answers the ask of d4: shared', event: ans1, about: keys.d4, expect: E.readAnswer(ans1, keys.sam, c4, keys.d4) },
  { name: 'd2 answers the card of a device that never asked: removed', event: ans2, expect: E.readAnswer(ans2, keys.sam, c4) },
  { name: 'd4 answers about d2, an older device (§17.6): ignored', event: ans3, expect: E.readAnswer(ans3, keys.sam, c4) },
  { name: 'the agent, a reader, answers: ignored', event: ans4, expect: E.readAnswer(ans4, keys.sam, c4) },
  { name: 'd1, older but since removed, answers: ignored (§20)', event: ans5, expect: E.readAnswer(ans5, keys.sam, c4) },
  { name: 'an answer to an ask without knowing the asking device: ignored', event: ans6, expect: E.readAnswer(ans6, keys.sam, c4) },
] }

// §17.12, §17.2, §13, and which people a rumor belongs to
const book = new Map()
const decl = [[kp.K, E.peopleId([keys.sam, keys.lina])], [kp.K, E.peopleId([keys.sam, keys.agent])], [gk.K, 'g:pod-garden-7f3a']].map(([K, conv]) => ({ K, conv, expect: E.declare(book, K, conv) }))
const peopleCases = [
  { name: 'a message', rumor: r1 },
  { name: 'a reaction in it (p of the people, e of the message)', rumor: rumor(keys.lina, 3500, [['e', r1.id], ['p', keys.sam], ['k', '14']], '👍', 7) },
  { name: 'a deletion of my message', rumor: rumor(keys.sam, 3600, [['e', r1.id], ['p', keys.lina]], '', 5) },
  { name: 'Notes to myself', rumor: rumor(keys.sam, 3600, [['p', keys.sam]], 'note') },
  { name: 'an e-mail out: the belfry key is one more person of that message', rumor: rumor(keys.sam, 3600, [['p', keys.belfry], ['subject', 'Hi']], 'raw mail', 1301) },
].map((c) => ({ ...c, expect: E.peopleOf(c.rumor) }))
const clear = [[undefined, 3000], [3000, 2999], [3000, 3000], [3000, 3001]].map(([first, at]) => ({ first_key_at: first ?? null, at, expect: E.clearAfterKey(first, at) }))
const limits = { people: [[100, false], [101, false], [101, true], [3, true]].map(([n, ever]) => ({ devices: n, ever_encrypted: ever, expect: E.peopleLimit(n, ever) })), group: [1000, 1001].map((n) => ({ devices: n, expect: E.groupLimit(n) })) }

const doc = {
  about: 'docs/encrypted.md (collective-belfry, with §16–§21): device lists 8570, keys 8571, messages 8572, packs 8573, asks 8574, answers 8575. Every secret below is a test key (sha256 of "belfry-vector:<name>").',
  keys, secrets, belfry: keys.belfry,
  lists: listCases, changes: changeCases, listed_before: { lists: [L1, L2, L3, L4, L5], cases: before }, signs_at: { lists: [L1, L2, L3], cases: signsAt }, listed_at: { lists: [L1, L2, L3], cases: listedAt },
  alias: aliases, lists_hash: lh,
  people_key: peopleKey, group_key: groupKey, people_messages: peopleMsgs, group_messages: groupMsgs,
  packs: packCases, declare: decl, people_of: peopleCases, clear_after_key: clear, limits,
  signs_at_gap: { lists: [L1, L2, L4], cases: signsAtGap }, signs_at_relisted: { lists: [L1, L2, L3, R4], cases: signsAtRelisted }, signs_at_behind: { lists: [L1, L2, L3], cases: signsAtBehind }, signs_at_slow_clock: { lists: [L1, L2, L3], cases: signsAtSlow }, group_key_slow_clock: groupKeySlow, invalid_above: invalidAbove, listed_at_gap: { lists: [L1, L2, L4], cases: listedAtGap }, pick_key: { held, conv: conv1, lists: lines, cases: pick }, asks,
}
writeFileSync(path.join(root, 'vectors/encrypted.json'), JSON.stringify(doc, null, 1) + '\n')
console.log('vectors/encrypted.json', createHash('sha256').update(JSON.stringify(doc, null, 1) + '\n').digest('hex'))
