// A to-do's title as it is typed (board todos/new-todo-limit, 2026-11-19; = Android ab87302): a
// to-do holds 300 characters and is never cut silently. From 250 "n of 300" under the field on the
// right; past 300 the extra is tinted red (a layer behind the field, as the composer's marks), Add
// is off (todoOver), and the line under it says by how much. The field grows with the text.
import { useLayoutEffect, useRef } from 'react'

export const TODO_MAX = 300
export const TODO_NEAR = 250
/** The count the limit is on: the text trimmed (Android References: jsTrim().length). */
export const todoCount = (t: string) => t.trim().length
export const todoOver = (t: string) => Math.max(0, todoCount(t) - TODO_MAX)

/** Opens New to-do with TEXT in the channel on screen (Turn into a to-do with a long message). */
export const NEW_TODO_EVENT = 'belfry-new-todo'
export const openNewTodo = (text: string, from?: string) => dispatchEvent(new CustomEvent(NEW_TODO_EVENT, { detail: { text, from } }))

export function TodoField({ value, onChange, placeholder, autoFocus, label }: { value: string; onChange: (v: string) => void; placeholder?: string; autoFocus?: boolean; label?: string }) {
  const ta = useRef<HTMLTextAreaElement>(null)
  const marks = useRef<HTMLDivElement>(null)
  useLayoutEffect(() => {
    const el = ta.current
    if (!el) return
    el.style.height = 'auto'
    el.style.height = el.scrollHeight + 'px'
  }, [value])
  const n = todoCount(value)
  const over = n - TODO_MAX
  // where the extra starts in the text as typed: after the leading spaces and 300 characters
  const lead = value.length - value.trimStart().length
  const cut = over > 0 ? lead + TODO_MAX : value.length
  return (
    <div className={'todo-field' + (over > 0 ? ' over' : '')}>
      <div className="todo-input">
        <div className="todo-marks" ref={marks} aria-hidden="true">{value.slice(0, cut)}{over > 0 && <mark>{value.slice(cut)}</mark>}{'​'}</div>
        <textarea ref={ta} rows={1} className="input" aria-label={label} autoFocus={autoFocus} placeholder={placeholder} value={value}
          aria-invalid={over > 0 || undefined} aria-describedby={undefined}
          onChange={(e) => onChange(e.target.value)}
          onScroll={(e) => { if (marks.current) marks.current.scrollTop = e.currentTarget.scrollTop }}
          onKeyDown={(e) => { if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); e.currentTarget.form?.requestSubmit() } }} />
      </div>
      {over > 0
        ? <p className="todo-limit error" role="alert">{over === 1 ? '1 character' : `${over} characters`} too long. A to-do holds {TODO_MAX}; put the rest in a comment on it.</p>
        : n >= TODO_NEAR && <p className="todo-limit count" aria-live="polite">{n} of {TODO_MAX}</p>}
    </div>
  )
}
