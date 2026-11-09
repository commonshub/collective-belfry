// Someone's page (#/u/<npub>): reached by tapping a name or a face anywhere in the chat.
// Yours has Edit: name, about, picture, tags (suggested from every member's tags) and where you
// are (a city from the belfry's list, one you add next to the nearest city, the country only,
// or nothing). Belfry admins can make another member an admin here.
import { useEffect, useMemo, useRef, useState } from 'react'
import { nip19 } from 'nostr-tools'
import { channelIcon, displayName, groups, guestJoins, profiles } from '../core/model'
import { aboutOf, addPlace, allTags, cleanTag, countries, extras, isBelfryAdmin, placeLabel, saveExtras, saveProfile, searchPlaces, setBelfryAdmin, type Place, type Share } from '../core/people'
import { isAdmin } from '../core/access'
import { useVersion } from '../core/store'
import { me, setAvatar } from '../core/sync'
import { Avatar, back, go, Sheet } from './common'
import { Markdown } from './markdown'
import { PinIcon } from './icons'

export const profileHref = (pubkey: string) => 'u/' + nip19.npubEncode(pubkey)
export const openProfile = (pubkey: string) => go(profileHref(pubkey))

export function decodeProfileId(id?: string): string | undefined {
  if (!id) return undefined
  if (/^[0-9a-f]{64}$/.test(id)) return id
  try { const d = nip19.decode(id); return d.type === 'npub' ? (d.data as string) : undefined } catch { return undefined }
}

export function ProfileView({ id }: { id?: string }) {
  useVersion()
  const pk = decodeProfileId(id)
  const [editing, setEditing] = useState(false)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  if (!pk) return <div className="screen"><div className="topbar"><button className="icon-btn" aria-label="Back" onClick={() => back('c')}>‹</button><h1>Profile</h1></div><div className="empty">This link doesn't point to a person.</div></div>
  const mine = pk === me()
  const p = profiles().get(pk)
  const x = extras().get(pk)
  const about = aboutOf(pk)
  const guest = groups().flatMap((g) => guestJoins(g.id)).find((j) => j.pubkey === pk)
  const shared = groups().filter((g) => g.members.has(pk) && !g.isProposal)
  const admin = isBelfryAdmin(pk)
  const toggleAdmin = async () => {
    const name = displayName(pk)
    if (!confirm(admin ? `Remove ${name} as an admin of the belfry?` : `Make ${name} an admin of the belfry? Admins manage members, every channel and the belfry's settings.`)) return
    setBusy(true); setError('')
    try { await setBelfryAdmin(pk, !admin) } catch (e) { setError(e instanceof Error ? e.message : String(e)) } finally { setBusy(false) }
  }
  return (
    <div className="screen">
      <div className="topbar">
        <button className="icon-btn" aria-label="Back" onClick={() => back('c')}>‹</button>
        <h1>{mine ? 'You' : displayName(pk)}</h1>
        {mine && <button className="btn small ghost" onClick={() => setEditing(true)}>Edit</button>}
      </div>
      <div className="scroll pad profile">
        <div className="profile-head">
          <Avatar pubkey={pk} size={88} />
          <div style={{ minWidth: 0 }}>
            <div className="profile-name">{p?.name || displayName(pk)}</div>
            {p?.nip05 && <div className="hint" style={{ overflowWrap: 'anywhere' }}>{p.nip05}</div>}
            <div className="profile-badges">
              {admin && <span className="chip">admin</span>}
              {guest && <span className="chip">{guest.agent ? 'agent' : guest.unsaved ? 'guest · not saved' : 'guest'}</span>}
              {guest?.by && <span className="hint" style={{ fontSize: 13 }}>invited by <button className="link" onClick={() => openProfile(guest.by!)}>{displayName(guest.by)}</button></span>}
            </div>
          </div>
        </div>
        {x?.place && <div className="profile-place"><PinIcon /> {placeLabel(x.place)}</div>}
        {about ? <Markdown text={about} className="profile-about" /> : mine && <p className="hint">Say a few words about yourself: tap Edit.</p>}
        {x && x.tags.length > 0 && <div className="profile-tags">{x.tags.map((t) => <span key={t} className="chip">#{t}</span>)}</div>}
        {!mine && !guest && <button className="btn" style={{ marginTop: 8 }} onClick={() => go('dm/' + encodeURIComponent('n:' + pk))}>Message</button>}
        {shared.length > 0 && (
          <div className="card" style={{ marginTop: 16 }}>
            <div className="hint" style={{ marginBottom: 6 }}>{mine ? 'Your channels' : 'In'}</div>
            {shared.map((g) => <button key={g.id} className="link profile-chan" onClick={() => go('c/' + encodeURIComponent(g.id))}>{channelIcon(g)} {g.name}</button>)}
          </div>
        )}
        {isAdmin() && !mine && (
          <div className="card">
            <label className="switch-row" style={{ margin: 0 }}>
              <span><b>Admin of the belfry</b><small>{guest ? 'Guests and agents cannot be admins.' : 'Manages members, every channel and the belfry\'s settings.'}</small></span>
              <input type="checkbox" role="switch" checked={admin} disabled={busy || !!guest} onChange={() => void toggleAdmin()} />
            </label>
            {error && <div className="error" style={{ marginTop: 8 }}>{error}</div>}
          </div>
        )}
      </div>
      {editing && <EditProfile onClose={() => setEditing(false)} />}
    </div>
  )
}

function EditProfile({ onClose }: { onClose: () => void }) {
  const pk = me()
  const x = extras().get(pk)
  const [name, setName] = useState(profiles().get(pk)?.name ?? '')
  const [about, setAbout] = useState(aboutOf(pk))
  const [tags, setTags] = useState<string[]>(x?.tags ?? [])
  const [share, setShare] = useState<Share>(x?.share ?? 'city')
  const [place, setPlace] = useState<Place | undefined>(x?.place)
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState('')
  const [pic, setPic] = useState('')
  return (
    <Sheet title="Edit your profile" onClose={onClose}>
      <form onSubmit={async (e) => {
        e.preventDefault()
        setSaving(true); setError('')
        try {
          const p0 = profiles().get(pk)
          if (name.trim() !== (p0?.name ?? '') || about.trim() !== aboutOf(pk)) await saveProfile({ name, about })
          await saveExtras({ tags, share, place })
          onClose()
        } catch (er) { setError(er instanceof Error ? er.message : String(er)) } finally { setSaving(false) }
      }}>
        <div className="row" style={{ gap: 12, marginBottom: 14 }}>
          <Avatar pubkey={pk} size={56} />
          <label className="btn small ghost" style={{ cursor: 'pointer' }}>{pic || 'Change picture'}
            <input type="file" accept="image/*" hidden onChange={async (e) => {
              const f = e.target.files?.[0]; e.target.value = ''
              if (!f) return
              setPic('Uploading…')
              try { await setAvatar(f); setPic('') } catch { setPic('Try again') }
            }} />
          </label>
        </div>
        <label className="field"><span>Name</span><input className="input" value={name} maxLength={100} onChange={(e) => setName(e.target.value)} /></label>
        <label className="field"><span>About you · Markdown works</span><textarea className="input" rows={4} value={about} maxLength={2000} onChange={(e) => setAbout(e.target.value)} /></label>
        <TagInput tags={tags} setTags={setTags} />
        <PlaceField share={share} setShare={setShare} place={place} setPlace={setPlace} />
        {error && <div className="error">{error}</div>}
        <button className="btn" disabled={saving}>{saving ? 'Saving…' : 'Save'}</button>
      </form>
    </Sheet>
  )
}

// ---- tags, with suggestions from everyone's -----------------------------------------------------

function TagInput({ tags, setTags }: { tags: string[]; setTags: (t: string[]) => void }) {
  const [q, setQ] = useState('')
  const [hi, setHi] = useState(0)
  const known = useMemo(() => allTags(), [])
  const c = cleanTag(q)
  const sugg = known.filter((k) => !tags.includes(k.tag) && (!c || k.tag.includes(c))).slice(0, 6)
  const add = (t: string) => { const v = cleanTag(t); if (v && !tags.includes(v) && tags.length < 30) setTags([...tags, v]); setQ(''); setHi(0) }
  const options = [...sugg.map((s) => s.tag), ...(c && !sugg.some((s) => s.tag === c) && !tags.includes(c) ? [c] : [])]
  return (
    <div className="field">
      <span id="tags-label">Tags · what you do, care about, can help with</span>
      <div className="tag-box">
        {tags.map((t) => (
          <span key={t} className="chip on">#{t}<button type="button" aria-label={`Remove ${t}`} onClick={() => setTags(tags.filter((x) => x !== t))}>×</button></span>
        ))}
        <input className="tag-input" aria-labelledby="tags-label" value={q} placeholder={tags.length ? '' : 'e.g. facilitation'}
          role="combobox" aria-expanded={!!q && options.length > 0} aria-controls="tag-options"
          onChange={(e) => { setQ(e.target.value); setHi(0) }}
          onKeyDown={(e) => {
            if (e.key === 'Enter' || e.key === ',') { if (q.trim() || options[hi]) { e.preventDefault(); add(options[hi] ?? q) } }
            else if (e.key === 'ArrowDown') { e.preventDefault(); setHi(Math.min(options.length - 1, hi + 1)) }
            else if (e.key === 'ArrowUp') { e.preventDefault(); setHi(Math.max(0, hi - 1)) }
            else if (e.key === 'Backspace' && !q && tags.length) setTags(tags.slice(0, -1))
          }} />
      </div>
      {(q || (!tags.length && sugg.length > 0)) && options.length > 0 && (
        <div className="suggest" id="tag-options" role="listbox">
          {options.map((t, i) => {
            const n = known.find((k) => k.tag === t)?.count
            return (
              <button type="button" role="option" aria-selected={i === hi} key={t} className={i === hi ? 'on' : ''} onClick={() => add(t)}>
                #{t}<small>{n ? `${n} ${n === 1 ? 'member' : 'members'}` : 'new tag'}</small>
              </button>
            )
          })}
        </div>
      )}
    </div>
  )
}

// ---- where you are -------------------------------------------------------------------------

function PlaceField({ share, setShare, place, setPlace }: { share: Share; setShare: (s: Share) => void; place?: Place; setPlace: (p?: Place) => void }) {
  const [picking, setPicking] = useState(false)
  const [country, setCountry] = useState(place?.country ?? '')
  return (
    <fieldset className="field place-field">
      <legend>Where you are</legend>
      <div className="seg" role="radiogroup" aria-label="What to share">
        {([['city', 'City'], ['country', 'Country only'], ['none', 'Nothing']] as [Share, string][]).map(([k, label]) => (
          <button type="button" role="radio" aria-checked={share === k} key={k} className={share === k ? 'on' : ''} onClick={() => setShare(k)}>{label}</button>
        ))}
      </div>
      {share === 'city' && (place?.name
        ? <div className="row" style={{ gap: 8, marginTop: 10 }}><PinIcon /><span style={{ flex: 1 }}>{placeLabel(place)}{place.near ? <small className="hint"> · on the map at {place.near}</small> : null}</span><button type="button" className="btn small ghost" onClick={() => setPicking(true)}>Change</button></div>
        : picking ? null : <button type="button" className="btn ghost" style={{ marginTop: 10 }} onClick={() => setPicking(true)}>Choose your city</button>)}
      {share === 'city' && picking && <CityPicker onPick={(p) => { setPlace(p); setCountry(p.country); setPicking(false) }} onCancel={() => setPicking(false)} />}
      {share === 'country' && (
        <select className="input" style={{ marginTop: 10 }} aria-label="Country" value={country || place?.country || ''}
          onChange={(e) => { setCountry(e.target.value); setPlace(e.target.value ? { country: e.target.value } : undefined) }}>
          <option value="">Choose a country</option>
          {countries().map((c) => <option key={c.code} value={c.code}>{c.name}</option>)}
        </select>
      )}
      <small className="hint" style={{ display: 'block', marginTop: 8 }}>
        {share === 'city' ? 'Members see your city, and you on the map of a channel\'s members (at the city, never your exact spot). Guests and the public don\'t.'
          : share === 'country' ? 'Members see your country only. You are not on the map.' : 'Nobody sees where you are.'}
      </small>
    </fieldset>
  )
}

/** Search the belfry's list; not there? Add it, then pick the nearest listed city for the map. */
function CityPicker({ onPick, onCancel }: { onPick: (p: Place) => void; onCancel: () => void }) {
  const [step, setStep] = useState<'search' | 'new'>('search')
  const [newName, setNewName] = useState('')
  const [newCountry, setNewCountry] = useState('')
  const [error, setError] = useState('')
  if (step === 'search')
    return (
      <PlaceSearch label="City" autoFocus onPick={onPick} onCancel={onCancel}
        extra={(q) => q.trim().length >= 2 ? { label: `Add “${q.trim()}”: it's not in the list`, run: () => { setNewName(q.trim()); setStep('new') } } : undefined} />
    )
  return (
    <div className="new-place">
      <label className="field"><span>Name of the place</span><input className="input" value={newName} maxLength={80} onChange={(e) => setNewName(e.target.value)} /></label>
      <label className="field"><span>Country</span>
        <select className="input" value={newCountry} onChange={(e) => setNewCountry(e.target.value)}>
          <option value="">Choose a country</option>
          {countries().map((c) => <option key={c.code} value={c.code}>{c.name}</option>)}
        </select>
      </label>
      <PlaceSearch label="Nearest city in the list · used for the map" onCancel={() => setStep('search')}
        onPick={async (near) => {
          if (!newName.trim() || !newCountry) { setError('Give the place a name and a country first.'); return }
          setError('')
          try { onPick(await addPlace(newName.trim(), newCountry, near)) } catch (e) { setError(e instanceof Error ? e.message : String(e)) }
        }} />
      {error && <div className="error">{error}</div>}
    </div>
  )
}

function PlaceSearch({ label, onPick, onCancel, extra, autoFocus }: {
  label: string; onPick: (p: Place) => void; onCancel: () => void; autoFocus?: boolean
  extra?: (q: string) => { label: string; run: () => void } | undefined
}) {
  const [q, setQ] = useState('')
  const [list, setList] = useState<Place[]>([])
  const [attr, setAttr] = useState('')
  const [state, setState] = useState<'idle' | 'busy' | 'error'>('idle')
  const timer = useRef<ReturnType<typeof setTimeout>>()
  useEffect(() => {
    clearTimeout(timer.current)
    if (q.trim().length < 2) { setList([]); setState('idle'); return }
    const ctl = new AbortController()
    timer.current = setTimeout(async () => {
      setState('busy')
      try { const r = await searchPlaces(q, ctl.signal); setList(r.places); setAttr(r.attribution ?? ''); setState('idle') }
      catch (e) { if (!ctl.signal.aborted) setState('error') }
    }, 200)
    return () => { clearTimeout(timer.current); ctl.abort() }
  }, [q])
  const more = extra?.(q)
  return (
    <div className="place-search">
      <label className="field" style={{ marginBottom: 6 }}><span>{label}</span>
        <input className="input" value={q} autoFocus={autoFocus} placeholder="Start typing a city" onChange={(e) => setQ(e.target.value)} autoComplete="off"
          onKeyDown={(e) => { if (e.key === 'Enter') { e.preventDefault(); if (list[0]) onPick(list[0]) } }} />
      </label>
      <div className="suggest" role="listbox" aria-label="Places">
        {list.map((p) => (
          <button type="button" role="option" aria-selected={false} key={p.id} onClick={() => onPick(p)}>
            {p.name}<small>{[p.region, countriesName(p.country)].filter(Boolean).join(', ')}{p.near ? ` · near ${p.near}` : ''}</small>
          </button>
        ))}
        {state === 'busy' && list.length === 0 && <div className="hint" style={{ padding: 8 }}>Looking…</div>}
        {state === 'error' && <div className="error" style={{ padding: 8 }}>The list of places is not available right now.</div>}
        {more && state !== 'busy' && <button type="button" className="add-place" onClick={more.run}>{more.label}</button>}
      </div>
      <div className="row" style={{ justifyContent: 'space-between', marginTop: 6 }}>
        <small className="hint">{attr}</small>
        <button type="button" className="link" onClick={onCancel}>Cancel</button>
      </div>
    </div>
  )
}

const countriesName = (code: string) => countries().find((c) => c.code === code)?.name ?? code
