// Photos and files dropped on an open conversation (to-do 920a27dc; Xavier: "I should be able to
// drag and drop any photo or file in the web app instead of having to click + then select 'photos
// or files'"): added to that conversation's composer exactly as its ＋ → Photos or files does.
// While files are dragged over it, the conversation says "Drop to add to your message". A drop
// anywhere else (or of text, a link) does nothing special, and never opens the file in the tab.
// Composers that take files carry data-files and listen for ADD_FILES (Chat.tsx Composer).
export const ADD_FILES = 'belfry-add-files'

const hasFiles = (e: DragEvent) => !!e.dataTransfer && [...e.dataTransfer.types].includes('Files')
/** The open conversation under the pointer: its screen (or the thread pane) holding a composer that takes files. */
function zoneOf(t: EventTarget | null): { zone: HTMLElement; composer: HTMLElement } | null {
  const zone = (t as Element | null)?.closest?.('.thread-pane, .screen') as HTMLElement | null
  const composer = zone?.querySelector<HTMLElement>('.composer[data-files]')
  return zone && composer ? { zone, composer } : null
}

let on: HTMLElement | null = null
const mark = (z: HTMLElement | null) => { if (on === z) return; on?.classList.remove('drop-on'); on = z; on?.classList.add('drop-on') }
let leaveTimer: ReturnType<typeof setTimeout> | null = null

if (typeof addEventListener === 'function') {
  addEventListener('dragover', (e) => {
    if (!hasFiles(e)) return
    e.preventDefault() // never let the browser open the file in place of the app
    const z = zoneOf(e.target)
    e.dataTransfer!.dropEffect = z ? 'copy' : 'none'
    mark(z?.zone ?? null)
    if (leaveTimer) { clearTimeout(leaveTimer); leaveTimer = null }
  })
  // leaving the window (no dragover follows): the target goes
  addEventListener('dragleave', () => { leaveTimer ??= setTimeout(() => { mark(null); leaveTimer = null }, 120) })
  addEventListener('dragend', () => mark(null))
  addEventListener('drop', (e) => {
    if (!hasFiles(e)) return
    e.preventDefault()
    mark(null)
    const z = zoneOf(e.target)
    const files = [...(e.dataTransfer?.files ?? [])]
    if (z && files.length) z.composer.dispatchEvent(new CustomEvent(ADD_FILES, { detail: files }))
  })
}
