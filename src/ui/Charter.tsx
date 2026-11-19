// The charter (board Charter, Join-Member 1–3; iOS SPEC 19.32): #/charter, #/charter/who/<id>,
// and a new member's first minutes (#/welcome: Welcome → the charter one tick at a time → the
// first channel). Members tick for themselves; stewards write, change, reorder and remove the
// agreements and see who agreed. Guests read them (from the belfry: they can't read the group)
// and tick them when they apply (Places.tsx ApplyScreen).
import { replaceHash } from '../core/nav'
import { directory, loadDirectory } from '../core/requests'
import { generateSecretKey, getPublicKey } from 'nostr-tools'
import { useEffect, useState, useLayoutEffect, useRef } from 'react'
import { NAME } from '../config'
import { agentOwner, belfryAdminKeys, demoSetAdmins } from '../core/access'
import { addAgreement, agree, agreedAt, charter, editAgreement, isStale, listIdForCharter, loadRemoteCharter, moveAgreement, parseCharter, remoteAgreements, removeAgreement, toAgree, type Agreement } from '../core/charter'
import { useBranding } from '../core/branding'
import { isGuest, isAgent } from '../core/invite'
import { displayName, group } from '../core/model'
import { isBelfryAdmin } from '../core/people'
import { CHARTER_ID } from '../core/places'
import { addEvents, all, useVersion, type Ev } from '../core/store'
import { me } from '../core/sync'
import { Avatar, back, Btn, Form, go, Sheet, up } from './common'
import { useCommunityName } from './community'
import { CheckCircleIcon, ChevronDownIcon, ChevronRightIcon, ChevronUpIcon, DocIcon, CheckIcon, PencilIcon, PlusIcon, TrashIcon } from './icons'
import { Arms } from './Login'
import { openProfile } from './Profile'
import { amGuest } from './Places'

const fmtDate = (ts: number) => new Date(ts * 1000).toLocaleDateString([], { day: 'numeric', month: 'short' })

/** The members a count is out of: people in the Square who are not guests or agents. */
/** An agreement's explanation: up to two lines, then "more" opens the whole text (Xavier,
 *  2026-11-18; boards onboarding/welcome-charter, apply-form, home/home). Inside a row that is
 *  itself a button, "more" doesn't tick the row. MORE = false: two lines, no "more". */
export function AgreementAbout({ text, more = true }: { text: string; more?: boolean }) {
  const ref = useRef<HTMLSpanElement>(null)
  const [open, setOpen] = useState(false)
  const [cut, setCut] = useState(false)
  useLayoutEffect(() => { const el = ref.current; if (el && !open) setCut(el.scrollHeight > el.clientHeight + 2) }, [text, open])
  const toggle = (e: React.SyntheticEvent) => { e.stopPropagation(); e.preventDefault(); setOpen(!open) }
  return (
    <>
      <small ref={ref} className={'agree-about' + (open ? ' open' : '')}>{text}</small>
      {more && (cut || open) && <span className="agree-more" role="button" tabIndex={0} aria-expanded={open} onClick={toggle}
        onKeyDown={(e) => { if (e.key === 'Enter' || e.key === ' ') toggle(e) }}>{open ? 'less' : 'more'}</span>}
    </>
  )
}

/** The belfry's members (who should agree): its member directory, else the main channel's
 *  members; never guests or agents (a belfry whose #general lists nobody showed "0 of 0"). */
export function charterPeople(): string[] {
  const dirKeys = (directory() ?? []).map((m) => m.pubkey)
  const g = group('general')
  const keys = new Set([...dirKeys, ...(g?.members ?? []), ...(isGuest(me()) ? [] : [me()])])
  return [...keys].filter((pk) => pk && !isGuest(pk) && !isAgent(pk) && !agentOwner(pk))
}

export function CharterScreen({ sub, id }: { sub?: string; id?: string }) {
  if (sub === 'who' && id) return <WhoAgreed id={id} />
  return <CharterList />
}

function CharterList() {
  useVersion()
  const guest = amGuest()
  const steward = isBelfryAdmin(me())
  const [editing, setEditing] = useState(false)
  const [writing, setWriting] = useState<Agreement | 'new' | null>(null)
  useEffect(() => { if (guest) void loadRemoteCharter(); else if (!directory()) void loadDirectory() }, [guest])
  const rules = charter()
  const people = charterPeople()
  const back_ = () => back('home')
  if (guest) {
    const list = remoteAgreements() ?? rules.map((r) => ({ id: r.id, title: r.title, about: r.about, order: r.order }))
    return (
      <div className="screen">
        <div className="topbar"><button className="icon-btn" aria-label="Back" onClick={back_}>‹</button><h1>Charter</h1></div>
        <div className="scroll pad">
          {list.length === 0 ? <div className="empty">No agreements here.</div> : <>
            <p className="hint">How we do things here. You tick them when you apply to become a member.</p>
            <div className="charter-list">{list.map((r) => <div key={r.id} className="rule ro"><DocIcon /><span className="t"><b>{r.title}</b>{r.about && <AgreementAbout text={r.about} />}</span></div>)}</div>
          </>}
        </div>
      </div>
    )
  }
  if (!rules.length) return (
    <div className="screen">
      <div className="topbar"><button className="icon-btn" aria-label="Back" onClick={back_}>‹</button><h1>Charter</h1></div>
      <div className="scroll pad charter-empty">
        <span className="calm-mark"><DocIcon /></span>
        <h2>No agreements yet</h2>
        <p className="hint">A charter is a short list of agreements. Everyone who joins ticks each one; members see new ones on Home.</p>
        {steward ? <button className="btn" onClick={() => setWriting('new')}>Write the first agreement</button> : <p className="hint">The stewards haven't written any.</p>}
      </div>
      {writing && <AgreementSheet r={writing === 'new' ? undefined : writing} onClose={() => setWriting(null)} />}
    </div>
  )
  const at = agreedAt()
  const waiting = toAgree()
  const fresh = waiting.length
  return (
    <div className="screen">
      <div className="topbar">
        <button className="icon-btn" aria-label="Back" onClick={back_}>‹</button>
        <h1>Charter</h1>
        {steward && <button className="icon-btn" aria-label="New agreement" title="New agreement" onClick={() => setWriting('new')}><PlusIcon /></button>}
        {steward && <button className="icon-btn" aria-label={editing ? 'Done' : 'Edit'} title={editing ? 'Done' : 'Edit'} aria-pressed={editing} onClick={() => setEditing(!editing)}>{editing ? <CheckIcon /> : <PencilIcon />}</button>}
      </div>
      <div className="scroll pad">
        {!steward || !editing ? (
          <div className="card row charter-head">
            <span className="row-ico"><DocIcon /></span>
            <span className="t"><b>Our charter</b><small>{at ? `You agreed · ${fmtDate(at)}` : fresh === rules.length ? `${rules.length} ${rules.length === 1 ? 'agreement' : 'agreements'} waiting for your tick` : `${fresh} waiting for your tick`}</small></span>
          </div>
        ) : null}
        <div className="charter-list">
          {rules.map((r, i) => {
            const mine = r.agreed.has(me())
            const stale = isStale(r)
            const n = people.filter((pk) => r.agreed.has(pk)).length
            const count = `${n} of ${people.length} ${steward ? 'agreed' : 'members'}`
            if (steward && editing) return (
              <div key={r.id} className="rule edit">
                <button className="t" onClick={() => setWriting(r)} aria-label={`Change “${r.title}”`}><b>{r.title}</b>{r.about && <AgreementAbout text={r.about} />}<small className="faint">{count}</small></button>
                <span className="moves">
                  <Btn className="icon-btn" aria-label={`Move “${r.title}” up`} disabled={i === 0} onClick={() => moveAgreement(r, -1)}><ChevronUpIcon /></Btn>
                  <Btn className="icon-btn" aria-label={`Move “${r.title}” down`} disabled={i === rules.length - 1} onClick={() => moveAgreement(r, 1)}><ChevronDownIcon /></Btn>
                </span>
              </div>
            )
            return (
              <div key={r.id} className={'rule' + (mine ? ' on' : '') + (!mine ? ' fresh' : '')}>
                <Btn className="tick" role="checkbox" aria-checked={mine} aria-label={`I agree: ${r.title}`} onClick={() => agree(r, !mine)}><CheckCircleIcon on={mine} /></Btn>
                <span className="t">
                  <b>{r.title}{!mine && <span className="badge new-badge">{stale ? 'CHANGED' : 'NEW'}</span>}</b>
                  {r.about && <AgreementAbout text={r.about} />}
                  {steward
                    ? <button className="link count" onClick={() => go('charter/who/' + r.id)}>{count}{!mine || r.addedAt > Date.now() / 1000 - 14 * 86400 ? ` · added ${fmtDate(r.addedAt)}` : ''} ›</button>
                    : <small className="faint">{!mine ? `Added ${fmtDate(r.textAt)} · ` : ''}{count}</small>}
                </span>
              </div>
            )
          })}
        </div>
        {steward && <p className="hint">{editing ? 'Tap a rule to change or remove it; the arrows move it. ' : ''}A count shows who has and hasn't agreed.</p>}
      </div>
      {writing && <AgreementSheet r={writing === 'new' ? undefined : writing} onClose={() => setWriting(null)} />}
    </div>
  )
}

/** A steward writes an agreement or changes one (changing the words asks everyone again). */
function AgreementSheet({ r, onClose }: { r?: Agreement; onClose: () => void }) {
  const [title, setTitle] = useState(r?.title ?? '')
  const [about, setAbout] = useState(r?.about ?? '')
  const [removing, setRemoving] = useState(false)
  const [error, setError] = useState('')
  const changed = !!r && (title.trim() !== r.title || about.trim() !== r.about)
  return (
    <Sheet title={r ? 'Change the agreement' : 'New agreement'} onClose={onClose}>
      <Form onSubmit={async (e) => {
        e.preventDefault()
        if (!title.trim()) return
        setError('')
        try { if (r) { if (changed) await editAgreement(r, title, about) } else await addAgreement(title, about); onClose() } catch (x) { setError(x instanceof Error ? x.message : String(x)) }
      }}>
        <label className="field"><span>Agreement</span><input className="input" autoFocus maxLength={300} placeholder="e.g. We look after each other" value={title} onChange={(e) => setTitle(e.target.value)} /></label>
        <label className="field"><span>One line to explain it (optional)</span><input className="input" maxLength={200} value={about} onChange={(e) => setAbout(e.target.value)} /></label>
        {changed && <p className="hint">Changing the words asks everyone to agree to this one again.</p>}
        {error && <div className="error" role="alert">{error}</div>}
        <button className="btn" disabled={!title.trim() || (!!r && !changed)}>{r ? 'Save' : 'Add agreement'}</button>
      </Form>
      {r && (removing
        ? <div className="row" style={{ gap: 8, marginTop: 12 }}>
            <Btn className="btn danger" onClick={async () => { await removeAgreement(r); onClose() }}>Remove “{r.title.slice(0, 40)}”</Btn>
            <button className="btn ghost" onClick={() => setRemoving(false)}>Keep</button>
          </div>
        : <button type="button" className="pick-row danger remove-row" style={{ marginTop: 12 }} onClick={() => setRemoving(true)}><TrashIcon /><span>Remove this agreement</span></button>)}
    </Sheet>
  )
}

/** #/charter/who/<id>: who has and hasn't agreed to one rule (stewards). No reminder button:
 *  Home already asks them. */
function WhoAgreed({ id }: { id: string }) {
  useVersion()
  const [tab, setTab] = useState<'yes' | 'no'>('yes')
  const r = charter().find((x) => x.id === id)
  const people = charterPeople()
  const yes = people.filter((pk) => r?.agreed.has(pk))
  const no = people.filter((pk) => !r?.agreed.has(pk))
  const list = (tab === 'yes' ? yes : no).sort((a, b) => displayName(a).localeCompare(displayName(b)))
  return (
    <div className="screen">
      <div className="topbar"><button className="icon-btn" aria-label="Back" onClick={() => up('charter')}>‹</button><h1>{r?.title ?? 'Agreement'}</h1></div>
      <div className="seg" role="tablist">
        <button role="tab" aria-selected={tab === 'yes'} className={tab === 'yes' ? 'on' : ''} onClick={() => setTab('yes')}>Agreed · {yes.length}</button>
        <button role="tab" aria-selected={tab === 'no'} className={tab === 'no' ? 'on' : ''} onClick={() => setTab('no')}>Not yet · {no.length}</button>
      </div>
      <div className="scroll">
        {!r ? <div className="empty">This agreement isn't there any more.</div> : list.length === 0 ? <div className="empty">Nobody.</div> : (
          <div className="home-card">{list.map((pk) => (
            <button key={pk} className="home-row" onClick={() => openProfile(pk)}>
              <Avatar pubkey={pk} size={36} />
              <span className="t"><b>{displayName(pk)}</b>{tab === 'no' && r.stale.has(pk) && <small>Agreed before the words changed</small>}</span>
              <ChevronRightIcon />
            </button>
          ))}</div>
        )}
        <p className="hint pad">Members who haven't agreed see the rule on their Home. Guests aren't counted until they apply.</p>
      </div>
    </div>
  )
}

// ---- a new member's first minutes (board Join-Member 1–3) ---------------------------------

const NEW_KEY = 'belfry-welcome'
const WAS_GUEST = 'belfry-was-guest'
/** This device just made an account (a link, an event) or saw its guest become a member: the
 *  welcome is due once, after the first sign-in, for that key. */
export function markNewAccount() { try { localStorage.setItem(NEW_KEY, 'new') } catch { /* private mode */ } }
export function welcomeDue(): boolean {
  let v = ''
  try {
    v = localStorage.getItem(NEW_KEY) ?? ''
    // a guest on this device: remembered; once they are a member, they get the welcome
    if (amGuest()) { localStorage.setItem(WAS_GUEST, me()); return false }
    if (localStorage.getItem(WAS_GUEST) === me() && v !== 'done:' + me()) { localStorage.removeItem(WAS_GUEST); localStorage.setItem(NEW_KEY, 'new'); v = 'new' }
  } catch { return false }
  if (v !== 'new' || agentOwner(me()) || isBelfryAdmin(me())) return false
  // no charter: nothing to tick, the step is skipped (and so is the welcome: name and photo are on the profile)
  if (!charter().length) return false
  // ticked before (on another device): not new here
  const ticked = all(me()).some((e) => e.kind === 2101 && e.pubkey === me() && e.tags.some((t) => t[0] === 'h' && t[1] === CHARTER_ID) && e.tags.some((t) => t[0] === 't' && t[1] === listIdForCharter()))
  return !ticked
}
const doneWelcome = () => { try { localStorage.setItem(NEW_KEY, 'done:' + me()) } catch { /* private mode */ } }

export function WelcomeFlow() {
  useVersion()
  const community = useCommunityName(NAME)
  const brand = useBranding()
  const [step, setStep] = useState<'hello' | 'charter'>('hello')
  const rules = charter()
  const [ticked, setTicked] = useState<Set<string>>(() => new Set(rules.filter((r) => r.agreed.has(me())).map((r) => r.id)))
  const [error, setError] = useState('')
  const inviter = welcomeInviter()
  // no profile step (board onboarding/welcome-profile, Xavier 19 Nov: "the sooner they get to the
  // chat the better"): the belfry's first channel, ready to write. The name is the one given when
  // joining, else the address's name part (displayName); the photo the initial.
  const finish = () => { doneWelcome(); replaceHash('#/c/general') }
  if (step === 'hello') return (
    <div className="screen welcome">
      <div className="scroll pad center">
        <span className="calm-mark big">{brand.logo ? <img src={brand.logo} alt="" /> : <Arms size={48} />}</span>
        <h1>Welcome to {community}</h1>
        {brand.description && <p className="hint">{brand.description}</p>}
        {inviter && <p className="welcome-by"><Avatar pubkey={inviter} size={24} /> {displayName(inviter)} let you in</p>}
        {rules.length > 0 && <p className="hint small">Next: {community}’s charter, {rules.length === 1 ? 'one short agreement' : `${rules.length} short agreements`}.</p>}
      </div>
      <div className="pad"><button className="btn" onClick={() => (rules.length ? setStep('charter') : finish())}>Continue</button></div>
    </div>
  )
  if (step === 'charter') {
    const all_ = rules.every((r) => ticked.has(r.id))
    return (
      <div className="screen welcome">
        <div className="topbar"><h1 style={{ textAlign: 'center' }}>Our charter</h1></div>
        <div className="scroll pad">
          <p className="hint">{rules.length === 1 ? 'One short agreement.' : `${rules.length} short agreements.`} Tick each one you agree to.</p>
          <div className="charter-list">
            {rules.map((r) => (
              <button key={r.id} className={'rule' + (ticked.has(r.id) ? ' on' : '')} role="checkbox" aria-checked={ticked.has(r.id)}
                onClick={() => setTicked((s) => { const n = new Set(s); if (n.has(r.id)) n.delete(r.id); else n.add(r.id); return n })}>
                <CheckCircleIcon on={ticked.has(r.id)} />
                <span className="t"><b>{r.title}</b>{r.about && <AgreementAbout text={r.about} />}</span>
              </button>
            ))}
          </div>
          {error && <div className="error" role="alert">{error}</div>}
        </div>
        <div className="pad">
          <Btn className="btn" disabled={!all_} onClick={async () => {
            setError('')
            try { for (const r of rules) if (!r.agreed.has(me())) await agree(r, true); finish() } catch (x) { setError(x instanceof Error ? x.message : String(x)) }
          }}>{all_ ? 'Continue' : `${ticked.size} of ${rules.length} agreed`}</Btn>
        </div>
      </div>
    )
  }
  return null
}

/** Who let me in: the member whose 9000 put me in the Square (its `invited-by`), when known. */
function welcomeInviter(): string | undefined {
  const e = all(me()).filter((x) => x.kind === 9000 && x.tags.some((t) => t[0] === 'p' && t[1] === me()) && x.tags.some((t) => t[0] === 'invited-by')).sort((a, b) => b.created_at - a.created_at)[0]
  return e?.tags.find((t) => t[0] === 'invited-by')?.[1]
}

// ---- demo: a charter written by a steward (window.demoCharter) -----------------------------

/** Demo only: N agreements by a steward (me when STEWARD), some agreed by others; FRESH ones I
 *  haven't ticked. Returns the ids. Also checks the parser against the iOS vector
 *  (window.__charterVector(json) → the derived list). */
export function demoCharter(opts: { steward?: boolean; rules?: number; agreedByMe?: number; others?: number; welcome?: boolean } = {}) {
  const { steward = false, rules = 6, agreedByMe = rules - 1, others = 3, welcome = false } = opts
  const hex = (n: number) => [...crypto.getRandomValues(new Uint8Array(n))].map((b) => b.toString(16).padStart(2, '0')).join('')
  const now = Math.floor(Date.now() / 1000)
  const author = steward ? me() : getPublicKey(generateSecretKey())
  const titles: [string, string][] = [['We look after each other', 'Kind words, good faith; speak up when someone isn’t.'], ['What’s said here stays here', 'Ask before sharing a message or photo outside.'],
    ['Shared things come back', 'Return tools within a week, clean and working.'], ['Quiet hours after 22:00', 'In the hall and the garden, every day.'],
    ['Big changes are proposed first', 'Proposed and voted on, not just done.'], ['Leave it better than you found it', 'Tidy the hall and the kitchen after use.']]
  const t = [['t', listIdForCharter()], ['h', CHARTER_ID], ['each', '1']]
  const evs: Ev[] = [
    { id: hex(32), pubkey: author, sig: hex(64), kind: 9007, created_at: now - 9000, tags: [['h', CHARTER_ID]], content: '' },
    { id: hex(32), pubkey: author, sig: hex(64), kind: 9002, created_at: now - 9000, tags: [['h', CHARTER_ID], ['name', 'Charter'], ['open']], content: '' },
    { id: hex(32), pubkey: hex(32), sig: hex(64), kind: 39000, created_at: now - 8990, tags: [['d', CHARTER_ID], ['name', 'Charter']], content: '' },
  ]
  // seeding again replaces the charter: every agreement there is removed by a steward first
  for (const r of charter()) evs.push({ id: hex(32), pubkey: r.author, sig: hex(64), kind: 2101, created_at: now, tags: [['e', r.id], ['action', 'delete'], ...t], content: '' })
  const ids: string[] = []
  const folks = Array.from({ length: others }, () => getPublicKey(generateSecretKey()))
  folks.forEach((pk, i) => evs.push(
    { id: hex(32), pubkey: pk, sig: hex(64), kind: 0, created_at: now - 9000, tags: [], content: JSON.stringify({ name: ['Chloé', 'Lou', 'Hana', 'Ben', 'Dev'][i % 5] }) },
    { id: hex(32), pubkey: hex(32), sig: hex(64), kind: 9000, created_at: now, tags: [['h', 'general'], ['p', pk, 'member']], content: '' }))
  for (let i = 0; i < rules; i++) {
    const [title, about] = titles[i % titles.length]
    const id = hex(32)
    ids.push(id)
    const at = i === rules - 1 && agreedByMe < rules ? now - 3600 : now - 8000 + i
    evs.push({ id, pubkey: author, sig: hex(64), kind: 2100, created_at: at, tags: [...t, ['about', about], ['order', String(i)]], content: title })
    if (i < agreedByMe) evs.push({ id: hex(32), pubkey: me(), sig: hex(64), kind: 2101, created_at: now - 7000 + i, tags: [['e', id], ['action', 'done'], ...t], content: '' })
    folks.forEach((pk, k) => { if (k < others - (i === rules - 1 ? others - 1 : 0)) evs.push({ id: hex(32), pubkey: pk, sig: hex(64), kind: 2101, created_at: now - 3000 + i, tags: [['e', id], ['action', 'done'], ...t], content: '' }) })
  }
  addEvents(evs)
  demoAdmins([author])
  if (welcome) markNewAccount()
  return ids
}
function demoAdmins(keys: string[]) { demoSetAdmins([...new Set([...belfryAdminKeys(), ...keys])]) }

// browser tests: the parser against vectors/charter.json of the iOS app
if (typeof window !== 'undefined') (window as unknown as { __charterVector: (v: { admins: string[]; events: Ev[] }) => unknown }).__charterVector = (v) =>
  parseCharter(v.events, (pk) => v.admins.includes(pk)).map((r) => ({ id: r.id, title: r.title, about: r.about, order: r.order === Number.MAX_SAFE_INTEGER ? null : r.order, textAt: r.textAt, agreed: Object.fromEntries(r.agreed), stale: [...r.stale] }))
