import { useEffect, useState } from 'react'
import { nip19 } from 'nostr-tools'
import { accessInfo, accessState, createInvite, grant, inviteCode, loadAccessInfo, requestNotes, revoke, withdrawInvite, type RequestNote } from '../core/access'
import { displayName } from '../core/model'
import { all, useVersion } from '../core/store'
import { me } from '../core/sync'
import { ACCESS, NAME, PLACE } from '../config'
import { Avatar, back, go, Sheet, Btn, Form } from './common'
import { Tabs } from './ChannelInfo'

type Tab = 'requests' | 'members' | 'invites'

const date = (s?: number) => (s ? new Date(s * 1000).toLocaleDateString([], { day: 'numeric', month: 'short', year: 'numeric' }) : '')
const name = (p: string) =>
  p === accessInfo()?.service ? 'Belfry' : displayName(p).endsWith('…') ? nip19.npubEncode(p).slice(0, 16) + '…' : displayName(p)

/** #/admin, #/admin/requests/<wrap id> (the link in Belfry's DM), #/admin/members, #/admin/invites */
export function Admin({ sub, id }: { sub?: string; id?: string }) {
  useVersion()
  useEffect(() => { void loadAccessInfo() }, [])
  const tab: Tab = sub === 'members' || sub === 'invites' ? sub : 'requests'
  const { pending, members, invites } = accessState()
  return (
    <div className="screen">
      <div className="topbar">
        <button className="icon-btn" aria-label="Back" onClick={() => back('me')}>‹</button>
        <h1>Admin<small>Who is in {PLACE}</small></h1>
      </div>
      <Tabs tab={tab} setTab={(t) => go('admin/' + t)} tabs={[['requests', `Requests${pending.length ? ` (${pending.length})` : ''}`], ['members', 'Members'], ['invites', 'Invites']]} />
      <div className="scroll">
        {tab === 'requests' && <Requests focus={id} pending={pending} />}
        {tab === 'members' && <Members members={members} />}
        {tab === 'invites' && <Invites invites={invites} />}
        <p className="hint pad" style={{ fontSize: 13 }}>
          Every grant, invite and answer is a signed event that all members can see. Admins can also answer Belfry's DMs with “approve” or “decline”.
        </p>
      </div>
    </div>
  )
}

function Requests({ focus, pending }: { focus?: string; pending: ReturnType<typeof accessState>['pending'] }) {
  const [notes, setNotes] = useState<Map<string, RequestNote>>(new Map())
  const [answering, setAnswering] = useState<string | null>(focus ?? null)
  const ids = pending.map((r) => r.wrapId).join(',')
  useEffect(() => {
    void requestNotes(pending.map((r) => r.wrapId)).then(setNotes).catch(() => { /* the bunker said no */ })
  }, [ids])
  if (!pending.length)
    return <div className="empty">{focus ? 'This request was answered already.' : 'No one is waiting.'}</div>
  const r = pending.find((x) => x.wrapId === answering)
  return (
    <div>
      {pending.map((p) => {
        const n = notes.get(p.wrapId)
        return (
          <button key={p.wrapId} className="member" style={{ alignItems: 'flex-start' }} onClick={() => setAnswering(p.wrapId)}>
            <Avatar pubkey={p.pubkey} />
            <div style={{ flex: 1, minWidth: 0 }}>
              <div style={{ fontWeight: 600 }}>{n?.name ?? name(p.pubkey)}{p.role === 'admin' && <span className="badge orange" style={{ marginLeft: 6 }}>asks for admin</span>}</div>
              <div className="hint" style={{ fontSize: 14, overflowWrap: 'anywhere' }}>{n ? n.note || 'No note.' : 'Reading the note…'}</div>
              <div className="hint" style={{ fontSize: 12 }}>{date(p.at)}</div>
            </div>
          </button>
        )
      })}
      {r && <Answer key={r.wrapId} wrapId={r.wrapId} pubkey={r.pubkey} note={notes.get(r.wrapId)} onClose={() => setAnswering(null)} />}
    </div>
  )
}

function Answer({ wrapId, pubkey, note, onClose }: { wrapId: string; pubkey: string; note?: RequestNote; onClose: () => void }) {
  const [role, setRole] = useState('member')
  const [days, setDays] = useState('')
  const [error, setError] = useState('')
  const act = async (f: () => Promise<void>) => {
    try { await f(); onClose() } catch (e) { setError(e instanceof Error ? e.message : String(e)) }
  }
  return (
    <Sheet title={note?.name ?? name(pubkey)} onClose={onClose}>
      <div className="hint" style={{ fontFamily: 'ui-monospace, monospace', fontSize: 12, overflowWrap: 'anywhere', marginBottom: 8 }}>{nip19.npubEncode(pubkey)}</div>
      {note?.note && <div className="card" style={{ whiteSpace: 'pre-wrap' }}>{note.note}</div>}
      <RoleDays role={role} setRole={setRole} days={days} setDays={setDays} />
      {error && <div className="error">{error}</div>}
      <button className="btn" onClick={() => act(() => grant(pubkey, role, Number(days) || undefined, wrapId))}>Let them in</button>
      <div style={{ height: 10 }} />
      <button className="btn ghost" onClick={() => act(() => revoke(pubkey, wrapId))}>Decline</button>
    </Sheet>
  )
}

function RoleDays({ role, setRole, days, setDays }: { role: string; setRole: (r: string) => void; days: string; setDays: (d: string) => void }) {
  return (
    <>
      <label className="field"><span>Role</span>
        <input className="input" list="roles" autoCapitalize="off" value={role} onChange={(e) => setRole(e.target.value)} />
        <datalist id="roles">{['member', 'volunteer', 'speaker', 'steward', 'admin'].map((r) => <option key={r} value={r} />)}</datalist>
      </label>
      <label className="field"><span>Valid for (days, empty = no end)</span>
        <input className="input" type="number" min={1} inputMode="numeric" value={days} onChange={(e) => setDays(e.target.value)} /></label>
    </>
  )
}

function Members({ members }: { members: ReturnType<typeof accessState>['members'] }) {
  const [adding, setAdding] = useState(false)
  return (
    <div>
      <div className="pad" style={{ paddingBottom: 0 }}><button className="btn ghost" onClick={() => setAdding(true)}>＋ Let someone in by npub</button></div>
      {members.length === 0 && <div className="empty">No one was let in by an admin yet. Pass holders are members anyway.</div>}
      {members.map((g) => (
        <div key={g.pubkey} className="member">
          <Avatar pubkey={g.pubkey} />
          <div style={{ flex: 1, minWidth: 0 }}>
            <div style={{ fontWeight: 600 }}>{name(g.pubkey)} <span className={'badge' + (g.role === 'admin' ? ' lime' : '')}>{g.role}</span></div>
            <div className="hint" style={{ fontSize: 12 }}>by {g.by === me() ? 'you' : name(g.by)} · {date(g.at)}{g.until ? ` · until ${date(g.until)}` : ''}</div>
          </div>
          {g.pubkey !== me() && <button className="link" onClick={() => { if (confirm(`End ${name(g.pubkey)}'s access?`)) void revoke(g.pubkey) }}>Remove</button>}
        </div>
      ))}
      {adding && <AddMember onClose={() => setAdding(false)} />}
    </div>
  )
}

function AddMember({ onClose }: { onClose: () => void }) {
  const [npub, setNpub] = useState('')
  const [role, setRole] = useState('member')
  const [days, setDays] = useState('')
  const [error, setError] = useState('')
  return (
    <Sheet title="Let someone in" onClose={onClose}>
      <Form onSubmit={async (e) => {
        e.preventDefault()
        try {
          const d = nip19.decode(npub.trim())
          if (d.type !== 'npub') throw new Error('That is not an npub.')
          await grant(d.data, role, Number(days) || undefined)
          onClose()
        } catch (err) { setError(err instanceof Error ? err.message : String(err)) }
      }}>
        <label className="field"><span>Their npub</span>
          <input className="input" autoCapitalize="off" spellCheck={false} placeholder="npub1…" value={npub} onChange={(e) => setNpub(e.target.value)} /></label>
        <RoleDays role={role} setRole={setRole} days={days} setDays={setDays} />
        {error && <div className="error">{error}</div>}
        <button className="btn" disabled={!npub.trim()}>Let them in</button>
      </Form>
    </Sheet>
  )
}

function Invites({ invites }: { invites: ReturnType<typeof accessState>['invites'] }) {
  const [making, setMaking] = useState(false)
  const [codes, setCodes] = useState<Record<string, string>>({})
  useEffect(() => {
    void (async () => {
      const out: Record<string, string> = {}
      for (const i of invites) {
        const e = all(me()).find((x) => x.id === i.id && x.kind === ACCESS.invite)
        const c = e && (await inviteCode(e))
        if (c) out[i.id] = c
      }
      setCodes(out)
    })()
  }, [invites.map((i) => i.id).join(',')])
  const now = Date.now() / 1000
  return (
    <div>
      <div className="pad" style={{ paddingBottom: 0 }}><button className="btn" onClick={() => setMaking(true)}>＋ New invite code</button></div>
      {invites.length === 0 && <div className="empty">No invite codes yet.</div>}
      {invites.map((i) => {
        const used = i.max !== undefined && i.uses >= i.max
        const expired = i.expires !== undefined && i.expires < now
        return (
          <div key={i.id} className="member">
            <div style={{ flex: 1, minWidth: 0 }}>
              <div style={{ fontWeight: 600, fontFamily: 'ui-monospace, monospace' }}>{codes[i.id] ?? 'code kept on the device that made it'}</div>
              <div className="hint" style={{ fontSize: 12 }}>
                {i.role}{i.days ? ` · ${i.days} days` : ''} · used {i.uses}{i.max ? `/${i.max}` : ''} · by {i.by === me() ? 'you' : name(i.by)}
                {used ? ' · used up' : expired ? ' · expired' : ''}
              </div>
            </div>
            {codes[i.id] && <button className="link" onClick={() => void share(codes[i.id])}>Share</button>}
            {!used && <button className="link" style={{ marginLeft: 10 }} onClick={() => { if (confirm('Withdraw this invite code?')) void withdrawInvite(i.id) }}>Withdraw</button>}
          </div>
        )
      })}
      {making && <NewInvite onClose={() => setMaking(false)} />}
    </div>
  )
}

const inviteLink = (code: string) => `${location.origin}${location.pathname}?invite=${encodeURIComponent(code)}`

async function share(code: string) {
  const text = `Join ${NAME} with this invite code: ${code}\n${inviteLink(code)}`
  try {
    if (navigator.share) return await navigator.share({ text })
    await navigator.clipboard.writeText(text)
    alert('Copied.')
  } catch { /* cancelled */ }
}

function NewInvite({ onClose }: { onClose: () => void }) {
  const [role, setRole] = useState('member')
  const [days, setDays] = useState('')
  const [max, setMax] = useState('1')
  const [code, setCode] = useState('')
  const [error, setError] = useState('')
  if (code)
    return (
      <Sheet title="Invite code" onClose={onClose}>
        <div className="card" style={{ textAlign: 'center', fontFamily: 'ui-monospace, monospace', fontSize: 24, letterSpacing: '0.05em' }}>{code}</div>
        <p className="hint">Only a fingerprint of the code is published. Pass the code on yourself: whoever has it gets in as {role}.</p>
        <Btn className="btn" onClick={() => share(code)}>Share</Btn>
      </Sheet>
    )
  return (
    <Sheet title="New invite code" onClose={onClose}>
      <Form onSubmit={async (e) => {
        e.preventDefault()
        try { setCode(await createInvite(role, Number(days) || undefined, Number(max) || undefined)) } catch (err) { setError(err instanceof Error ? err.message : String(err)) }
      }}>
        <RoleDays role={role} setRole={setRole} days={days} setDays={setDays} />
        <label className="field"><span>How many people can use it (empty = no limit)</span>
          <input className="input" type="number" min={1} inputMode="numeric" value={max} onChange={(e) => setMax(e.target.value)} /></label>
        {error && <div className="error">{error}</div>}
        <button className="btn">Make the code</button>
      </Form>
    </Sheet>
  )
}
