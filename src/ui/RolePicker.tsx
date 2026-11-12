// One component wherever a role is chosen (Xavier, 2026-11-12, canvas "RolePicker"): the invite
// sheet's "Joins as", a member's profile, the belfry's settings. A radio list of roles (label,
// optional line; the default marked; Admin with the shield, the only role with powers); admins
// also edit them and add new ones. RoleForm is the edit screen (label, description, default for
// new members, and in settings the people who have it); deleting asks in the app and says who has
// the role and what happens to them.
import { useMemo, useState } from 'react'
import { displayName, profiles } from '../core/model'
import { ADMIN, MEMBER, RoleFailed, createRole, defaultRole, deleteRole, holders, memberKeys, roleById, roles, setMemberRole, updateRole, type Role } from '../core/roles'
import { useVersion } from '../core/store'
import { Avatar, BackHead, Btn, Form, Sheet } from './common'
import { CloseIcon, PencilIcon } from './icons'

const ShieldIcon = () => (
  <svg className="ico shield" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" role="img" aria-label="Has powers">
    <path d="M12 2 4 5v6c0 5 3.4 9.3 8 11 4.6-1.7 8-6 8-11V5z" />
  </svg>
)
export const PlusIcon = () => (
  <svg className="ico" width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" aria-hidden="true"><path d="M12 5v14M5 12h14" /></svg>
)

/** The roles as a radio list. ONEDIT shows a pencil per role and "Add a role" (admins). */
export function RoleList({ value, onPick, onEdit, onAdd, busy }: { value?: string; onPick: (id: string) => unknown; onEdit?: (id: string) => void; onAdd?: () => void; busy?: boolean }) {
  useVersion()
  return (
    <div className="pick-group role-list" role="radiogroup" aria-label="Role">
      {roles().map((r) => (
        <div key={r.id} className={'pick-row' + (value === r.id ? ' on' : '')}>
          <Btn className="pick-main" role="radio" aria-checked={value === r.id} disabled={busy} onClick={() => onPick(r.id)}>
            <span className="radio" aria-hidden="true" />
            <span className="t"><b>{r.label}{r.powers && <> <ShieldIcon /></>}{r.default && <span className="tag">default</span>}</b>{r.description && <small>{r.description}</small>}</span>
          </Btn>
          {onEdit && <button type="button" className="icon-btn small" aria-label={`Edit ${r.label}`} onClick={() => onEdit(r.id)}><PencilIcon /></button>}
        </div>
      ))}
      {onAdd && <button type="button" className="pick-row add" onClick={onAdd}><PlusIcon /><b>Add a role</b></button>}
    </div>
  )
}

const names = (keys: string[]) => {
  const n = keys.map((k) => displayName(k))
  return n.length <= 1 ? n.join('') : `${n.slice(0, -1).join(', ')} and ${n[n.length - 1]}`
}

/** Add or edit a role. PEOPLE: list who has it, with Add people and remove (settings). */
export function RoleForm({ id, onDone, people = false }: { id?: string; onDone: (id?: string) => void; people?: boolean }) {
  useVersion()
  const role = id ? roleById(id) : undefined
  const [label, setLabel] = useState(role?.label ?? '')
  const [description, setDescription] = useState(role?.description ?? '')
  const [isDefault, setIsDefault] = useState(!!role?.default)
  const [error, setError] = useState('')
  const [confirming, setConfirming] = useState(false)
  const [adding, setAdding] = useState(false)
  const builtin = role?.builtin ?? false
  const canDefault = id !== ADMIN
  const has = id ? holders(id) : []
  const fail = (e: unknown) => setError(e instanceof RoleFailed ? e.message : 'That did not work. Try again.')
  const save = async () => {
    setError('')
    try {
      if (!id) { const made = await createRole({ label, description, default: isDefault }); onDone(made.id) }
      else {
        const change: { label?: string; description?: string; default?: boolean } = {}
        if (label.trim() !== role?.label) change.label = label
        if (description.trim() !== role?.description) change.description = description
        if (canDefault && isDefault !== role?.default) change.default = isDefault
        if (Object.keys(change).length) await updateRole(id, change)
        onDone(id)
      }
    } catch (e) { fail(e) }
  }
  if (id && confirming) {
    const to = roleById(defaultRole() === id ? MEMBER : defaultRole())?.label ?? 'Member'
    return (
      <div className="confirm-box" role="alertdialog" aria-label={`Delete ${role?.label}`}>
        <b>Delete the {role?.label} role?</b>
        <p>{has.length === 0 ? 'Nobody has it.' : `${has.length === 1 ? '1 person has' : `${has.length} people have`} it: ${names(has)}. They keep their membership and become ${to}.`}</p>
        {error && <div className="error" role="alert">{error}</div>}
        <div className="row-btns two">
          <button type="button" className="btn ghost" onClick={() => setConfirming(false)}>Keep it</button>
          <Btn className="btn danger" onClick={async () => { setError(''); try { await deleteRole(id); onDone() } catch (e) { fail(e) } }}>Delete</Btn>
        </div>
      </div>
    )
  }
  return (
    <Form className="role-form" onSubmit={save}>
      <label className="field"><span>Label</span>
        <input className="input" value={label} maxLength={40} placeholder="Host, Volunteer, Team…" autoFocus={!id} onChange={(e) => { setLabel(e.target.value); setError('') }} /></label>
      <label className="field"><span>Description <i>· optional</i></span>
        <textarea className="input" rows={2} value={description} maxLength={200} placeholder="One line people see when they pick it" onChange={(e) => { setDescription(e.target.value); setError('') }} /></label>
      {builtin && <p className="note-box">{id === ADMIN ? 'Admin is built in: it can change settings and roles, and there is always at least one.' : 'Member is built in: everyone who is not a guest has at least this.'}</p>}
      {canDefault && (
        <label className="switch-row">
          <span><b>Default for new members</b><small>{isDefault ? 'Picked first on invites.' : `Today: ${roleById(defaultRole())?.label ?? 'Member'}.`}</small></span>
          <input type="checkbox" role="switch" checked={isDefault} onChange={(e) => setIsDefault(e.target.checked)} />
        </label>
      )}
      {people && id && (
        <div className="role-people">
          <div className="role-people-head"><span>People with this role · {has.length}</span>
            <button type="button" className="btn small ghost" onClick={() => setAdding(true)}>Add people</button></div>
          {has.map((k) => (
            <div key={k} className="invite-row">
              <Avatar pubkey={k} size={28} />
              <span className="t">{displayName(k)}</span>
              {id !== defaultRole() && <Btn className="icon-btn small" aria-label={`Remove ${displayName(k)} from ${role?.label}`} onClick={async () => { setError(''); try { await setMemberRole(k, defaultRole()) } catch (e) { fail(e) } }}><CloseIcon /></Btn>}
            </div>
          ))}
        </div>
      )}
      {error && <div className="error" role="alert">{error}</div>}
      <button className="btn" disabled={!label.trim()}>Save</button>
      {id && !builtin && <button type="button" className="link danger" onClick={() => { setError(''); setConfirming(true) }}>Delete role…</button>}
      {adding && id && <AddPeopleSheet role={id} onClose={() => setAdding(false)} />}
    </Form>
  )
}

/** Members who don't have ROLE yet, by name: tap to give it. */
function AddPeopleSheet({ role, onClose }: { role: string; onClose: () => void }) {
  useVersion()
  const [q, setQ] = useState('')
  const [error, setError] = useState('')
  const r = roleById(role)
  const words = q.trim().toLowerCase()
  const list = useMemo(() => memberKeys()
    .filter((k) => !holders(role).includes(k))
    .filter((k) => !words || displayName(k).toLowerCase().split(/\s+/).some((w) => w.startsWith(words)))
    .sort((a, b) => displayName(a).localeCompare(displayName(b)))
    .slice(0, 30), [words, role, profiles().size]) // eslint-disable-line react-hooks/exhaustive-deps
  return (
    <Sheet title={`Add people to ${r?.label ?? role}`} onClose={onClose}>
      <label className="field"><span>Members</span>
        <input className="input" placeholder="Type a name" value={q} autoCapitalize="none" autoCorrect="off" onChange={(e) => setQ(e.target.value)} /></label>
      {list.map((k) => (
        <div key={k} className="invite-row">
          <Avatar pubkey={k} size={28} />
          <span className="t">{displayName(k)}<small>{roleById(holdersOf(k))?.label ?? ''}</small></span>
          <Btn className="btn small" onClick={async () => { setError(''); try { await setMemberRole(k, role) } catch (e) { setError(e instanceof RoleFailed ? e.message : 'That did not work. Try again.') } }}>Add</Btn>
        </div>
      ))}
      {list.length === 0 && <p className="hint">{words ? 'No member by that name.' : 'Everyone has this role.'}</p>}
      {error && <div className="error" role="alert">{error}</div>}
    </Sheet>
  )
}
const holdersOf = (k: string) => roles().find((r) => holders(r.id).includes(k))?.id

/** A sheet to pick someone's role (a profile): the list, and for admins Edit / Add in place. */
export function RolePickerSheet({ title, value, onPick, onClose }: { title: string; value?: string; onPick: (id: string) => Promise<void>; onClose: () => void }) {
  const [editing, setEditing] = useState<string | null>(null) // '' = a new role
  const [error, setError] = useState('')
  if (editing !== null) {
    return (
      <Sheet title={editing ? 'Edit role' : 'New role'} hideTitle onClose={onClose}>
        <BackHead title={editing ? 'Edit role' : 'New role'} onBack={() => setEditing(null)} />
        <RoleForm id={editing || undefined} onDone={() => setEditing(null)} />
      </Sheet>
    )
  }
  return (
    <Sheet title={title} onClose={onClose}>
      <RoleList value={value} onPick={async (id) => { if (id === value) return onClose(); setError(''); try { await onPick(id); onClose() } catch (e) { setError(e instanceof RoleFailed ? e.message : 'That did not work. Try again.') } }}
        onEdit={(id) => setEditing(id)} onAdd={() => setEditing('')} />
      {error && <div className="error" role="alert" style={{ marginTop: 10 }}>{error}</div>}
    </Sheet>
  )
}

export type { Role }
