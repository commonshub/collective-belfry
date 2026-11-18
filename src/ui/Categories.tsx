// Categories of channels (the owner, 2026-11-17; wire format in core/order.ts, the iPhone app's
// SPEC 19.34). In the channel list: each category's header with a + (new channel in it), and the
// belfry's admins move channels by dragging them (a mouse) or holding them, then dragging (a
// finger), within a category and across categories; "The town" stays fixed. In Belfry settings →
// Channels: the same list, where admins also rename, add, delete and reorder the categories.
import { GroupName } from './GroupName'
import type React from 'react'
import { useEffect, useRef, useState } from 'react'
import { categorize, categoryId, layoutContent, mayOrder, setLayout, shownCategories, LIFE, type Category } from '../core/order'
import { groups, isPublicChannel, type Group } from '../core/model'
import { useVersion } from '../core/store'
import { Btn, Form, Sheet } from './common'
import { setArchived } from '../core/actions'
import { channelIsEmpty, deleteChannel } from '../core/checkin'
import { isTownChannel } from './Places'
import { ArchiveIcon, ChevronDownIcon, ChevronRightIcon, ChevronUpIcon, GlobeIcon, GripIcon, MoreIcon, PencilIcon, PlusIcon, TrashIcon } from './icons'

/** The town's channels (not the Square) in their categories. */
export const townCategories = () => categorize(groups().filter((g) => isTownChannel(g.id) && g.id !== 'general'))

type Cat = Category<Group>
const clone = (cats: Cat[]) => cats.map((c) => ({ ...c, items: [...c.items] }))
/** Publish CATS as the belfry's list. */
export const saveCategories = (cats: Cat[]) =>
  // `building` left out of the list and still empty stays out (it keeps its defaults for later)
  setLayout(layoutContent(cats.filter((c) => c.listed || c.id === LIFE || c.items.length || c.named.length)))

/** CATS with channel ID moved into category TO at position POS. */
export function moved(cats: Cat[], id: string, to: string, pos: number): Cat[] {
  const next = clone(cats)
  let ch: Group | undefined
  for (const c of next) { const i = c.items.findIndex((g) => g.id === id); if (i >= 0) ch = c.items.splice(i, 1)[0] }
  const t = next.find((c) => c.id === to)
  if (!ch || !t) return cats
  t.items.splice(Math.max(0, Math.min(pos, t.items.length)), 0, ch)
  return next
}

/** A channel created from a category's + by an admin: at the end of that category. */
export async function placeNewChannel(id: string, cat: string) {
  if (!mayOrder()) return
  const cats = townCategories()
  const c = cats.find((x) => x.id === cat)
  if (!c) return
  if (cats.some((x) => x.items.some((g) => g.id === id))) { await saveCategories(moved(cats, id, cat, c.items.length)); return }
  // not on this device yet: named at the end, shown once the relay has it
  const next = clone(cats)
  for (const x of next) x.named = x.named.filter((n) => n !== id)
  const t = next.find((x) => x.id === cat)!
  t.named = [...t.items.map((g) => g.id), ...t.named.filter((n) => !t.items.some((g) => g.id === n)), id]
  await saveCategories(next)
}

// ---- dragging -----------------------------------------------------------------------------

/** Drag channels between the categories in BOX (rows: .chan-slot[data-cat][data-chan]; headers:
 *  .section-title[data-cat]). A mouse drags after 6 px; a finger holds 400 ms first (a short touch
 *  scrolls or opens the channel; a sideways swipe still closes the drawer). */
export function useCategoryDrag(cats: Cat[], may = mayOrder()) {
  const ref = useRef<HTMLDivElement>(null)
  const [drag, setDrag] = useState<{ id: string; from: string; to: string; pos: number } | null>(null)
  const st = useRef<{ id: string; cat: string; x: number; y: number; armed: boolean; on: boolean; timer?: number; moved: boolean } | null>(null)
  const dragRef = useRef(drag); dragRef.current = drag
  const catsRef = useRef(cats); catsRef.current = cats
  const dropped = useRef(false)
  // a held finger: the list doesn't scroll while it drags (and the drawer doesn't take the move)
  useEffect(() => {
    const el = ref.current
    if (!el || !may) return
    const tm = (e: TouchEvent) => { if (st.current?.on || st.current?.armed) e.preventDefault() }
    // a blocking touchstart too: Chrome then waits for the page before it starts scrolling
    const ts = () => undefined
    el.addEventListener('touchstart', ts, { passive: false })
    el.addEventListener('touchmove', tm, { passive: false })
    return () => { el.removeEventListener('touchstart', ts); el.removeEventListener('touchmove', tm) }
  }, [may])
  const where = (y: number): { to: string; pos: number } => {
    const box = ref.current!
    const heads = [...box.querySelectorAll('.section-title[data-cat]')] as HTMLElement[]
    const head = heads.filter((h) => h.getBoundingClientRect().top <= y).at(-1) ?? heads[0]
    const to = head?.dataset.cat ?? LIFE
    const slots = [...box.querySelectorAll('.chan-slot')].filter((x) => (x as HTMLElement).dataset.cat === to && (x as HTMLElement).dataset.chan !== st.current?.id) as HTMLElement[]
    return { to, pos: slots.filter((x) => { const r = x.getBoundingClientRect(); return r.top + r.height / 2 < y }).length }
  }
  const finish = () => {
    const s = st.current, d = dragRef.current
    if (s?.timer) clearTimeout(s.timer)
    dropped.current = !!(s?.on || s?.armed)
    setTimeout(() => { dropped.current = false }, 0) // only the click that ends this drag
    st.current = null
    setDrag(null)
    if (!s?.on || !d) return
    const before = catsRef.current
    const was = before.find((c) => c.id === d.from)?.items.findIndex((g) => g.id === d.id)
    if (d.from === d.to && d.pos === was) return // dropped where it was
    void saveCategories(moved(before, d.id, d.to, d.pos))
  }
  const slot = (g: Group, cat: string, i: number) => {
    const base = { className: 'chan-slot', 'data-chan': g.id, 'data-cat': cat } as Record<string, unknown>
    if (!may) return base
    const d = drag
    const list = cats.find((c) => c.id === cat)?.items ?? []
    const rest = d ? list.filter((x) => x.id !== d.id) : []
    const iRest = rest.findIndex((x) => x.id === g.id)
    const before = !!d && d.to === cat && d.id !== g.id && d.pos === iRest
    const after = !!d && d.to === cat && d.pos === rest.length && rest.at(-1)?.id === g.id
    void i
    return {
      ...base,
      className: 'chan-slot' + (d?.id === g.id ? ' dragging' : '') + (before ? ' drop-before' : '') + (after ? ' drop-after' : ''),
      onPointerDown: (e: React.PointerEvent) => {
        if (e.pointerType === 'mouse' && e.button !== 0) return
        const s = { id: g.id, cat, x: e.clientX, y: e.clientY, armed: false, on: false, moved: false } as NonNullable<typeof st.current>
        if (e.pointerType !== 'mouse') s.timer = window.setTimeout(() => { if (st.current === s && !s.moved) { s.armed = true; navigator.vibrate?.(15); setDrag({ id: g.id, from: cat, to: cat, pos: list.findIndex((x) => x.id === g.id) }) } }, 400)
        st.current = s
      },
      onPointerMove: (e: React.PointerEvent) => {
        const s = st.current
        if (!s || s.id !== g.id) return
        const dy = e.clientY - s.y, dx = e.clientX - s.x
        if (!s.on) {
          if (e.pointerType === 'mouse' ? Math.abs(dy) < 6 : !s.armed) { if (Math.hypot(dx, dy) > 8) { s.moved = true; if (!s.armed && s.timer) clearTimeout(s.timer) } return }
          s.on = true
          try { (e.currentTarget as HTMLElement).setPointerCapture(e.pointerId) } catch { /* gone */ }
        }
        setDrag({ id: g.id, from: cat, ...where(e.clientY) })
      },
      onPointerUp: finish,
      onPointerCancel: () => { const s = st.current; if (s?.timer) clearTimeout(s.timer); st.current = null; setDrag(null) },
      // the click that ends a drag (or a held finger) doesn't open the channel
      onClickCapture: (e: React.MouseEvent) => { if (dropped.current) { e.stopPropagation(); e.preventDefault() } dropped.current = false },
      onContextMenu: (e: React.MouseEvent) => { if (st.current?.armed || st.current?.on) e.preventDefault() },
    }
  }
  /** A category's header: shows where an empty category takes the channel. */
  const into = (cat: string) => !!drag && drag.to === cat && (cats.find((c) => c.id === cat)?.items ?? []).filter((x) => x.id !== drag.id).length === 0
  return { ref, drag, slot, into, may }
}

/** A category's title in the channel list, with its + on the right when ADD. */
export function CategoryHead({ c, add, into }: { c: Cat; add?: () => void; into?: boolean }) {
  return (
    <div className={'section-title cat-head' + (into ? ' drop-into' : '')} data-cat={c.id}>
      <span className="lbl">{c.name}</span>
      {add && <button type="button" className="icon-btn cat-add" aria-label={`New channel in ${c.name}`} onClick={add}><PlusIcon /></button>}
    </div>
  )
}

// ---- Belfry settings → Channels -----------------------------------------------------------

/** The categories with their channels; admins edit them (every change is published at once). */
export function ChannelsPanel({ wide = false }: { wide?: boolean }) {
  useVersion()
  const cats = townCategories()
  const admin = mayOrder()
  const dnd = useCategoryDrag(cats, admin)
  const [menu, setMenu] = useState<{ kind: 'cat'; id: string } | { kind: 'chan'; id: string } | null>(null)
  const [adding, setAdding] = useState(false)
  const shown = shownCategories(cats, true)
  return (
    <section aria-label="Channels" className="channels-panel">
      {wide && <div className="panel-head"><h2 className="panel-title">Channels</h2><button className="btn small" onClick={() => setAdding(true)}><PlusIcon /> Add a category</button></div>}
      <p className="hint" style={{ fontSize: 14 }}>How the channel list is arranged for everyone. Hold a channel and drag it, or tap it to move it. The town always comes first.</p>
      <div className="pick-head">The town</div>
      <div className="act-group cat-fixed">
        <div className="act settings-row"><span className="t"><b>Townsquare, Members, Charter</b><small>Fixed: always at the top</small></span></div>
      </div>
      <div ref={dnd.ref} className={'chan-sections settings-cats' + (dnd.drag ? ' sorting' : '') + (admin ? ' may' : '')}>
        {shown.map((c) => (
          <div key={c.id} className="cat-block">
            <div className={'section-title cat-head' + (dnd.into(c.id) ? ' drop-into' : '')} data-cat={c.id}>
              <span className="lbl">{c.name}{c.builtin && c.id === LIFE && <span className="tag">default</span>}</span>
              {admin && <button type="button" className="icon-btn" aria-label={`${c.name}: rename, move, delete`} onClick={() => setMenu({ kind: 'cat', id: c.id })}><MoreIcon /></button>}
            </div>
            <div className="act-group cat-chans">
              {c.items.length === 0 && <div className="act settings-row empty-cat"><span className="t"><small>No channels. Drag one here, or move one from its menu.</small></span></div>}
              {c.items.map((g, i) => (
                <div key={g.id} {...dnd.slot(g, c.id, i)}>
                  <button type="button" className="act settings-row chan-row" onClick={() => admin && setMenu({ kind: 'chan', id: g.id })}>
                    <span className="hash">#</span>
                    <span className="t"><b><GroupName g={g} />{isPublicChannel(g) && <span className="pub" aria-label="public"><GlobeIcon /></span>}</b></span>
                    {admin && <GripIcon />}
                  </button>
                </div>
              ))}
            </div>
          </div>
        ))}
      </div>
      {!wide && admin && <button className="btn ghost add-cat" onClick={() => setAdding(true)}><PlusIcon /> Add a category</button>}
      {menu?.kind === 'cat' && <CategorySheet cats={cats} id={menu.id} onClose={() => setMenu(null)} />}
      {menu?.kind === 'chan' && <ChannelMoveSheet cats={cats} id={menu.id} onClose={() => setMenu(null)} />}
      {adding && <NewCategorySheet cats={cats} onClose={() => setAdding(false)} />}
    </section>
  )
}

/** A category's menu: rename, move up / down, delete (its channels go back to Community life). */
function CategorySheet({ cats, id, onClose }: { cats: Cat[]; id: string; onClose: () => void }) {
  const c = cats.find((x) => x.id === id)
  const [name, setName] = useState(c?.name ?? '')
  const [confirm, setConfirm] = useState(false)
  if (!c) return null
  const i = cats.findIndex((x) => x.id === id)
  const swap = (j: number) => { const next = clone(cats); [next[i], next[j]] = [next[j], next[i]]; return saveCategories(next) }
  const remove = () => {
    const next = clone(cats).filter((x) => x.id !== id)
    const life = next.find((x) => x.id === LIFE)!
    life.items.push(...c.items)
    life.named = [...(life.named ?? []), ...c.named.filter((n) => !c.items.some((g) => g.id === n))]
    return saveCategories(next)
  }
  return (
    <Sheet title={c.name} onClose={onClose}>
      {confirm ? (
        <div className="confirm-delete">
          <p>Delete “{c.name}”? {c.items.length ? `Its ${c.items.length === 1 ? 'channel moves' : `${c.items.length} channels move`} to ${cats.find((x) => x.id === LIFE)?.name}.` : 'It has no channels.'} Nothing else changes.</p>
          <div className="act-group">
            <Btn className="act danger" onClick={async () => { await remove(); onClose() }}><TrashIcon /><span>Delete the category</span></Btn>
            <button type="button" className="act" onClick={() => setConfirm(false)}><span>Keep it</span></button>
          </div>
        </div>
      ) : <>
        <Form className="cat-rename" onSubmit={async () => {
          const n = name.trim().slice(0, 60)
          if (!n || n === c.name) { onClose(); return }
          const next = clone(cats); next[i].name = n
          await saveCategories(next); onClose()
        }}>
          <label className="field"><span>Name</span>
            <input className="input" aria-label="Category name" maxLength={60} value={name} onChange={(e) => setName(e.target.value)} /></label>
          <button className="btn" disabled={!name.trim()}><PencilIcon /> Rename</button>
        </Form>
        <div className="act-group">
          {i > 0 && <Btn className="act" onClick={async () => { await swap(i - 1); onClose() }}><ChevronUpIcon /><span>Move up</span></Btn>}
          {i < cats.length - 1 && <Btn className="act" onClick={async () => { await swap(i + 1); onClose() }}><ChevronDownIcon /><span>Move down</span></Btn>}
        </div>
        {c.id !== LIFE && (
          <div className="act-group">
            <button type="button" className="act danger" onClick={() => setConfirm(true)}><TrashIcon /><span>Delete the category<small>Its channels move to {cats.find((x) => x.id === LIFE)?.name}</small></span></button>
          </div>
        )}
      </>}
    </Sheet>
  )
}

/** A channel's place: up, down, or into another category (at its end). */
function ChannelMoveSheet({ cats, id, onClose }: { cats: Cat[]; id: string; onClose: () => void }) {
  const [ending, setEnding] = useState(false)
  const empty = useEmpty(id)
  const from = cats.find((c) => c.items.some((g) => g.id === id))
  const g = from?.items.find((x) => x.id === id)
  if (!from || !g) return null
  const i = from.items.indexOf(g)
  const go = async (to: string, pos: number) => { await saveCategories(moved(cats, id, to, pos)); onClose() }
  if (ending) return <EndChannelSheet g={g} onClose={onClose} onBack={() => setEnding(false)} />
  return (
    <Sheet title={`# ${g.name}`} onClose={onClose}>
      <div className="act-group">
        {i > 0 && <Btn className="act" onClick={() => go(from.id, i - 1)}><ChevronUpIcon /><span>Move up</span></Btn>}
        {i < from.items.length - 1 && <Btn className="act" onClick={() => go(from.id, i + 1)}><ChevronDownIcon /><span>Move down</span></Btn>}
      </div>
      <div className="pick-head">Move to</div>
      <div className="act-group">
        {cats.filter((c) => c.id !== from.id).map((c) => (
          <Btn key={c.id} className="act" onClick={() => go(c.id, c.items.length)}><ChevronRightIcon /><span>{c.name}</span></Btn>
        ))}
      </div>
      <div className="act-group">
        {/* an empty channel can be deleted; one with messages is archived (Xavier, 2026-11-17) */}
        <button type="button" className="act danger end-chan" disabled={empty === undefined} onClick={() => setEnding(true)}>
          {empty === false ? <ArchiveIcon /> : <TrashIcon />}<span>{empty === undefined ? 'Delete or archive…' : empty ? 'Delete the channel' : 'Archive the channel'}
            <small>{empty === undefined ? 'Checking for messages' : empty ? 'Nobody wrote in it yet' : 'It has messages: they stay readable, nobody can post'}</small></span>
        </button>
      </div>
    </Sheet>
  )
}

/** Whether channel ID has no messages: undefined while asking, null when the relay can't say. */
function useEmpty(id: string): boolean | null | undefined {
  const [empty, setEmpty] = useState<boolean | null | undefined>(undefined)
  useEffect(() => { let on = true; void channelIsEmpty(id).then((v) => { if (on) setEmpty(v) }); return () => { on = false } }, [id])
  return empty
}

/** Delete an empty channel or archive one with messages, after saying what happens. */
function EndChannelSheet({ g, onClose, onBack }: { g: Group; onClose: () => void; onBack: () => void }) {
  const empty = useEmpty(g.id)
  const [error, setError] = useState('')
  const del = empty === true
  const run = async () => {
    setError('')
    try {
      if (del) await deleteChannel(g.id)
      else await setArchived(g.id, true)
      onClose()
    } catch (e) {
      const m = e instanceof Error ? e.message : ''
      if (/archive it instead/.test(m)) { setError(m); return } // someone wrote meanwhile: archive is offered now
      setError(m || 'That did not work. Try again.')
    }
  }
  return (
    <Sheet title={del ? `Delete #${g.name}?` : `Archive #${g.name}?`} onClose={onClose}>
      <p className="end-what">{del
        ? 'Nobody wrote in it yet. It goes for everyone, with its settings. This can’t be undone.'
        : 'People wrote in it. It leaves the channel list and becomes read only; its messages stay. Unarchive it any time from its page.'}</p>
      {empty === null && <p className="hint">The channel could not be checked for messages, so it is archived (nothing is lost).</p>}
      {error && <p className="error" role="alert">{error}</p>}
      <div className="act-group">
        <Btn className="act danger" onClick={run}>{del ? <TrashIcon /> : <ArchiveIcon />}<span>{del ? 'Delete the channel' : 'Archive the channel'}</span></Btn>
        <button type="button" className="act" onClick={onBack}><span>Keep it</span></button>
      </div>
    </Sheet>
  )
}

function NewCategorySheet({ cats, onClose }: { cats: Cat[]; onClose: () => void }) {
  const [name, setName] = useState('')
  return (
    <Sheet title="New category" onClose={onClose}>
      <Form onSubmit={async () => {
        const n = name.trim().slice(0, 60)
        if (!n) return
        const next = clone(cats)
        next.push({ id: categoryId(n), name: n, builtin: false, items: [], named: [], listed: true })
        await saveCategories(next); onClose()
      }}>
        <label className="field"><span>Name</span>
          <input className="input" autoFocus aria-label="Category name" maxLength={60} placeholder="e.g. Working groups" value={name} onChange={(e) => setName(e.target.value)} /></label>
        <p className="hint">It goes at the end of the list. Drag channels into it, or use + on its title to start one.</p>
        <button className="btn" disabled={!name.trim()}>Add the category</button>
      </Form>
    </Sheet>
  )
}
