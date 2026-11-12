// The belfry's settings in the chat (Xavier, 2026-11-12, canvas "Settings-Phone" / "Settings-Wide";
// Discord's server settings are the model). Admins of the belfry only; opened from the
// belfry's name at the top of the channel list, and from Me. One list of sections:
//   Community: Overview, Branding, Roles · People: Members, Invite links, Requests to join ·
//   Belfry: E-mail, Apps, Version and updates.
// Roles and Branding are screens here; the others open what exists already (the console on
// belfry.<domain>, or the chat's Admin screen) rather than a second copy of it.
// Phone: the list, then a section, then a role (‹ goes up one level, never pushes).
// Laptop (≥ 900 px): the list on the left, the section on the right; roles list and the role
// being edited side by side.
import { useEffect, type ReactNode } from 'react'
import { BELFRY, NAME } from '../config'
import { isBelfryAdmin } from '../core/people'
import { ADMIN, MEMBER, holders, loadRoles, memberKeys, otherCounts, roleById, roles, type Role } from '../core/roles'
import { useVersion } from '../core/store'
import { me } from '../core/sync'
import { useBranding } from '../core/branding'
import { back, go, up, useWide } from './common'
import { BrandingScreen } from './Branding'
import { useCommunityName } from './community'
import { ChevronRightIcon, GlobeIcon, ImageIcon, LinkIcon, MailIcon, PeopleIcon, GearIcon } from './icons'
import { PlusIcon, RoleForm } from './RolePicker'

/** belfry.<domain>: the console, where the rest of the settings live today. */
const CONSOLE = BELFRY.api.replace(/\/api(\/v1)?$/, '').replace('//pass.', '//belfry.')

type Section = { id: string; label: string; icon: ReactNode; sub?: () => string; href?: string; to?: string }
const ShieldIcon = () => <svg className="ico" width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.75" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true"><path d="M12 2 4 5v6c0 5 3.4 9.3 8 11 4.6-1.7 8-6 8-11V5z" /></svg>
const InboxIcon = () => <svg className="ico" width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.75" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true"><path d="M4 13h4l2 3h4l2-3h4M5 5h14l1 8v6H4v-6z" /></svg>
const InfoIcon = () => <svg className="ico" width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.75" strokeLinecap="round" aria-hidden="true"><circle cx="12" cy="12" r="9" /><path d="M12 11v5M12 8v.01" /></svg>
const AppsIcon = () => <svg className="ico" width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.75" strokeLinejoin="round" aria-hidden="true"><rect x="4" y="4" width="7" height="7" rx="2" /><rect x="13" y="4" width="7" height="7" rx="2" /><rect x="4" y="13" width="7" height="7" rx="2" /><rect x="13" y="13" width="7" height="7" rx="2" /></svg>

const GROUPS: { title: string; items: Section[] }[] = [
  { title: 'Community', items: [
    { id: 'overview', label: 'Overview', icon: <InfoIcon />, sub: () => 'Name, description', href: '/' },
    { id: 'branding', label: 'Branding', icon: <ImageIcon />, sub: () => 'Logo, colours' },
    { id: 'roles', label: 'Roles', icon: <ShieldIcon />, sub: () => String(roles().length) },
  ] },
  { title: 'People', items: [
    { id: 'members', label: 'Members', icon: <PeopleIcon />, sub: () => (memberKeys().length ? String(memberKeys().length) : ''), href: '/members' },
    { id: 'invites', label: 'Invite links', icon: <LinkIcon />, to: 'admin/invites' },
    { id: 'requests', label: 'Requests to join', icon: <InboxIcon />, to: 'admin/requests' },
  ] },
  { title: 'Belfry', items: [
    { id: 'email', label: 'E-mail', icon: <MailIcon />, href: '/setup' },
    { id: 'apps', label: 'Apps', icon: <AppsIcon />, href: '/apps' },
    { id: 'version', label: 'Version and updates', icon: <GearIcon />, href: '/status' },
  ] },
]
const SECTIONS = GROUPS.flatMap((g) => g.items)
const BUILT = new Set(['branding', 'roles'])

/** #/settings[/<section>[/<role id>|new]] */
export function SettingsScreen({ section, id }: { section?: string; id?: string }) {
  useVersion()
  const wide = useWide()
  const community = useCommunityName(NAME)
  useEffect(() => { void loadRoles() }, [])
  if (!isBelfryAdmin(me())) {
    return (
      <div className="screen">
        <div className="topbar"><button className="icon-btn" aria-label="Back" onClick={() => back('c')}>‹</button><h1>Settings</h1></div>
        <div className="empty">Only admins of the belfry open its settings.</div>
      </div>
    )
  }
  const current = section && BUILT.has(section) ? section : undefined
  if (wide) {
    const open = current ?? 'roles'
    return (
      <div className="screen settings wide">
        <div className="topbar"><button className="icon-btn" aria-label="Back" onClick={() => back('c')}>‹</button><h1>{community} settings</h1></div>
        <div className="settings-wide">
          <nav className="settings-nav" aria-label="Settings">
            {GROUPS.map((gr) => (
              <div key={gr.title}>
                <div className="section-title">{gr.title}</div>
                {gr.items.map((it) => <NavItem key={it.id} it={it} on={it.id === open} replace />)}
              </div>
            ))}
          </nav>
          <main className="settings-panel">
            {open === 'branding' ? <BrandingScreen embedded /> : <RolesPanel id={id} wide />}
          </main>
        </div>
      </div>
    )
  }
  if (current === 'branding') return <BrandingScreen parent="settings" />
  if (current === 'roles' && id) {
    const r = id === 'new' ? undefined : roleById(id)
    return (
      <div className="screen settings">
        <div className="topbar"><button className="icon-btn" aria-label="Back" onClick={() => up('settings/roles')}>‹</button><h1>{id === 'new' ? 'New role' : r?.label ?? 'Role'}</h1></div>
        <div className="scroll pad">
          {id !== 'new' && !r ? <div className="empty">This role doesn’t exist any more.</div>
            : <RoleForm key={id} id={r?.id} people onDone={() => up('settings/roles')} />}
        </div>
      </div>
    )
  }
  if (current === 'roles') {
    return (
      <div className="screen settings">
        <div className="topbar"><button className="icon-btn" aria-label="Back" onClick={() => up('settings')}>‹</button><h1>Roles</h1>
          <button className="btn small ghost" onClick={() => go('settings/roles/new')}>Add</button></div>
        <div className="scroll pad"><RolesPanel /></div>
      </div>
    )
  }
  return <SettingsList community={community} />
}

function SettingsList({ community }: { community: string }) {
  const brand = useBranding()
  return (
    <div className="screen settings">
      <div className="topbar"><button className="icon-btn" aria-label="Back" onClick={() => back('c')}>‹</button><h1>{community} settings</h1></div>
      <div className="scroll pad">
        <div className="settings-head">
          {brand.logo ? <img className="brand-logo big" src={brand.logo} alt="" /> : <span className="settings-mark" aria-hidden="true">{community.slice(0, 1).toUpperCase()}</span>}
          <span><b>{community}</b><small>{BELFRY.domain}{memberKeys().length ? ` · ${memberKeys().length} members` : ''}{otherCounts()?.guest ? ` · ${otherCounts()!.guest} guests` : ''}</small></span>
        </div>
        {GROUPS.map((gr) => (
          <div key={gr.title}>
            <div className="pick-head">{gr.title}</div>
            <nav className="act-group settings-group" aria-label={`${gr.title} settings`}>
              {gr.items.map((it) => <NavItem key={it.id} it={it} />)}
            </nav>
          </div>
        ))}
        <p className="hint" style={{ fontSize: 13 }}>Rows marked <GlobeIcon /> open on {CONSOLE.replace(/^https:\/\//, '')}, the belfry’s console.</p>
      </div>
    </div>
  )
}

function NavItem({ it, on = false, replace = false }: { it: Section; on?: boolean; replace?: boolean }) {
  const sub = it.sub?.() ?? ''
  if (it.href) {
    return (
      <a className="act settings-row" href={CONSOLE + it.href} target="_blank" rel="noreferrer">
        {it.icon}<span className="t">{it.label}</span>{sub && <span className="v">{sub}</span>}<GlobeIcon />
      </a>
    )
  }
  return (
    <button type="button" className={'act settings-row' + (on ? ' on' : '')} aria-current={on ? 'page' : undefined}
      onClick={() => { const path = it.to ?? 'settings/' + it.id; if (replace && !it.to) location.replace('#/' + path); else go(path) }}>
      {it.icon}<span className="t">{it.label}</span>{sub && <span className="v">{sub}</span>}<ChevronRightIcon />
    </button>
  )
}

/** Members' roles, with how many have each; guests and agents apart, fixed. */
function RolesPanel({ id, wide = false }: { id?: string; wide?: boolean }) {
  useVersion()
  const all = roles()
  const order: Role[] = [...all.filter((r) => r.id === ADMIN), ...all.filter((r) => r.id !== ADMIN && r.id !== MEMBER), ...all.filter((r) => r.id === MEMBER)]
  const others = otherCounts()
  const open = (rid: string) => (wide ? location.replace('#/settings/roles/' + rid) : go('settings/roles/' + rid))
  const count = (rid: string) => holders(rid).length
  const editing = wide && id ? (id === 'new' ? 'new' : roleById(id)?.id) : undefined
  const list = (
    <section aria-label="Roles" className="roles-panel">
      {wide && <div className="panel-head"><h2 className="panel-title">Roles</h2><button className="btn small" onClick={() => open('new')}><PlusIcon /> Add a role</button></div>}
      <p className="hint" style={{ fontSize: 14 }}>Every member has one role. A role is a name and a line people see when they pick it; Admin is the one that can change settings and roles.</p>
      <div className="act-group role-rows">
        {order.map((r) => (
          <button key={r.id} type="button" className={'act settings-row' + (editing === r.id ? ' on' : '')} onClick={() => open(r.id)}>
            <span className="t"><b>{r.label}{r.default && <span className="tag">default</span>}</b>{r.description && <small>{r.description}</small>}</span>
            <span className="v">{count(r.id)}{wide ? (count(r.id) === 1 ? ' person' : ' people') : ''}</span><ChevronRightIcon />
          </button>
        ))}
      </div>
      <div className="pick-head">Not members</div>
      <div className="act-group role-rows fixed">
        <div className="act settings-row"><span className="t"><b>Guest</b><small>One channel, for a day, a week or a month. Chosen on each invite.</small></span><span className="v">{others?.guest ?? '—'}</span></div>
        <div className="act settings-row"><span className="t"><b>Agent</b><small>A member’s own AI agent.</small></span><span className="v">{others?.agent ?? '—'}</span></div>
      </div>
    </section>
  )
  if (!wide) return list
  return (
    <div className="settings-split">
      {list}
      {editing && (
        <section className="role-edit" aria-label={editing === 'new' ? 'New role' : `Edit ${roleById(editing)?.label}`}>
          <h2 className="panel-title">{editing === 'new' ? 'New role' : roleById(editing)?.label}</h2>
          <RoleForm key={editing} id={editing === 'new' ? undefined : editing} people onDone={(rid) => location.replace('#/settings/roles' + (rid ? '/' + rid : ''))} />
        </section>
      )}
    </div>
  )
}

export { SECTIONS }
