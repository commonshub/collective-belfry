import { useEffect, useRef, useState } from 'react'
import { fileUrl, type Dm } from '../core/dm'
import { UNREADABLE, fileLabel, sizeLabel } from '../core/dmfiles'
import { canRecord, clock, discard, elapsed, filesOf, start, stop, subscribe, type RecMeta } from '../core/recorder'
import { Btn, Sheet } from './common'

// Pictures, files and audio recordings in direct messages (canvas v104, messages/attach-*): shown
// from a decrypted blob: URL, never a link to what the storage holds (it is encrypted anyway).

function useFile(d: Dm) {
  const [s, set] = useState<{ url?: string; bad?: boolean }>({})
  useEffect(() => {
    let on = true
    set({})
    fileUrl(d).then((url) => on && set({ url }), () => on && set({ bad: true }))
    return () => { on = false }
  }, [d.rid, d.file?.x])
  return s
}

const extOf = (mime: string) => ({ 'application/pdf': 'pdf', 'audio/mp4': 'm4a', 'image/jpeg': 'jpg', 'image/png': 'png', 'text/plain': 'txt' } as Record<string, string>)[mime] ?? (mime.split('/')[1] ?? 'bin').replace(/[^a-z0-9]/g, '').slice(0, 5)

/** One picture: its own size while it loads (the dim tag), then the picture; a tap opens it. */
function Picture({ d, more = 0, tile = false }: { d: Dm; more?: number; tile?: boolean }) {
  const { url, bad } = useFile(d)
  const [w, h] = d.file?.dim ?? [4, 3]
  if (bad) return <div className="dm-pic bad">{UNREADABLE}</div>
  return (
    <a className={'dm-pic' + (tile ? ' tile' : '')} href={url} target="_blank" rel="noopener" aria-label="Picture" style={tile ? undefined : { aspectRatio: `${w} / ${h}` }}
      onClick={(e) => { if (!url) e.preventDefault() }}>
      {url ? <img src={url} alt="" /> : <span className="dm-pic-wait" aria-hidden="true" />}
      {more > 0 && <span className="dm-more">+{more}</span>}
    </a>
  )
}

/** A file: its kind and size; a tap saves it (decrypted). */
function FileCard({ d }: { d: Dm }) {
  const [busy, setBusy] = useState(false)
  const [bad, setBad] = useState(!d.file)
  const mime = d.file?.mime ?? ''
  const label = fileLabel(mime)
  if (bad) return <div className="dm-filecard bad">{UNREADABLE}</div>
  const save = async () => {
    setBusy(true)
    try {
      const url = await fileUrl(d)
      const name = d.file!.name
      Object.assign(document.createElement('a'), { href: url, download: /\.[a-z0-9]{1,5}$/i.test(name) ? name : `${name}.${extOf(mime)}` }).click()
    } catch { setBad(true) } finally { setBusy(false) }
  }
  return (
    <button type="button" className="dm-filecard" onClick={() => void save()} aria-busy={busy || undefined} disabled={busy}>
      <span className="dm-filebadge" aria-hidden="true">{extOf(mime).toUpperCase().slice(0, 4)}</span>
      <span className="t"><b>{d.file!.name}</b><small>{label === 'PDF' ? 'PDF' : extOf(mime).toUpperCase()} · {sizeLabel(d.file!.size)}{busy ? ' · opening…' : ''}</small></span>
    </button>
  )
}

/** A recording: the browser's player on the decrypted file, its length. */
function Recording({ d }: { d: Dm }) {
  const { url, bad } = useFile(d)
  if (bad) return <div className="dm-filecard bad">{UNREADABLE}</div>
  return (
    <div className="dm-audio">
      {url ? <audio controls preload="metadata" src={url} /> : <span className="dm-audio-wait">Opening the recording…</span>}
      {d.file?.duration ? <small>{clock(d.file.duration * 1000)}</small> : null}
    </div>
  )
}

/** The files of one bubble (pictures in a grid of up to four, "+N" on the last), then the others. */
export function FilesBody({ files }: { files: Dm[] }) {
  const pics = files.filter((d) => d.file?.mime.startsWith('image/'))
  const rest = files.filter((d) => !pics.includes(d))
  return (
    <div className="dm-files-body">
      {pics.length === 1 && <Picture d={pics[0]} />}
      {pics.length > 1 && (
        <div className={'dm-grid n' + Math.min(4, pics.length)} role="group" aria-label={`${pics.length} pictures`}>
          {pics.slice(0, 4).map((d, i) => <Picture key={d.rid} d={d} tile more={i === 3 ? pics.length - 4 : 0} />)}
        </div>
      )}
      {rest.map((d) => (d.file?.mime.startsWith('audio/') ? <Recording key={d.rid} d={d} /> : <FileCard key={d.rid} d={d} />))}
    </div>
  )
}

/** messages/attach-plus: the + of a conversation. Photos (several at once), File, Audio recording. */
export function AttachSheet({ onFiles, onRecord, onClose }: { onFiles: (f: File[]) => void; onRecord: () => void; onClose: () => void }) {
  const photos = useRef<HTMLInputElement>(null)
  const any = useRef<HTMLInputElement>(null)
  const take = (e: React.ChangeEvent<HTMLInputElement>) => { const f = Array.from(e.target.files ?? []); e.target.value = ''; if (f.length) { onFiles(f); onClose() } }
  return (
    <Sheet title="Add to the conversation" hideTitle onClose={onClose}>
      <input ref={photos} type="file" accept="image/*" multiple hidden onChange={take} />
      <input ref={any} type="file" hidden onChange={take} />
      <div className="act-group">
        <button className="act" onClick={() => photos.current?.click()}><span>Photos<small>Pictures from this device</small></span></button>
        <button className="act" onClick={() => any.current?.click()}><span>File<small>A document or a video, from this device</small></span></button>
        {canRecord() && <button className="act" onClick={() => { onClose(); onRecord() }}><span>Audio recording<small>Record here; kept on this device until sent</small></span></button>}
      </div>
    </Sheet>
  )
}

/** An audio recording for a conversation: Record, Stop, then Send or Discard. */
export function DmRecordSheet({ conv, onSend, onClose }: { conv: string; onSend: (f: File, seconds: number) => Promise<void>; onClose: () => void }) {
  const [meta, setMeta] = useState<RecMeta | null>(null)
  const [done, setDone] = useState<RecMeta | null>(null)
  const [error, setError] = useState('')
  const [, tick] = useState(0)
  useEffect(() => subscribe(() => tick((n) => n + 1)), [])
  const close = async () => { if (meta && !done) { const m = await stop(); if (m) await discard(m.id) } else if (done) await discard(done.id); onClose() }
  return (
    <Sheet title="Audio recording" onClose={() => void close()}>
      {error && <p className="error">{error}</p>}
      {!meta && !done && <Btn className="btn" onClick={async () => { setError(''); try { setMeta(await start('dm:' + conv)) } catch (e) { setError(e instanceof Error ? e.message : String(e)) } }}>Record</Btn>}
      {meta && !done && (
        <>
          <p className="rec-clock" role="timer">{clock(elapsed())}</p>
          <Btn className="btn" onClick={async () => { const m = await stop(); setDone(m) }}>Stop</Btn>
        </>
      )}
      {done && (
        <>
          <p className="hint">{clock(done.ms)} recorded. It stays on this device until it is sent.</p>
          <div className="row-btns">
            <Btn className="btn ghost" onClick={async () => { await discard(done.id); onClose() }}>Discard</Btn>
            <Btn className="btn" onClick={async () => {
              setError('')
              try {
                const [f] = await filesOf(done)
                if (!f) throw new Error('The recording is empty.')
                await onSend(f, done.ms / 1000)
                await discard(done.id)
                onClose()
              } catch (e) { setError(e instanceof Error ? e.message : String(e)) }
            }}>Send</Btn>
          </div>
        </>
      )}
    </Sheet>
  )
}
