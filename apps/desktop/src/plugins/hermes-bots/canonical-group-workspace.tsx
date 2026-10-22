import { Button, Textarea } from '@hermes/plugin-sdk'
import { useEffect, useRef, useState } from 'react'

import { CanonicalGroupAttachments } from './canonical-group-attachments'
import { type CanonicalGroupEvent, CanonicalGroupHistory } from './canonical-group-history'
import { useCanonicalGroupLabels } from './canonical-group-labels'
import { prepareCanonicalGroupSend, readCanonicalGroupSend, retireCanonicalGroupSend } from './canonical-group-send'
import type { PreparedCanonicalGroupSend } from './canonical-group-send'
import { CanonicalGroupStatusStrip } from './canonical-group-status'
import { actCanonicalGroup, canonicalGroupRequest } from './canonical-groups'
import type { CanonicalGroupBinding, CanonicalPendingAction, CanonicalRoom, CanonicalRoomMember } from './canonical-groups'

type RoomEvent = CanonicalGroupEvent
interface Attachment { attachment_id?: string; event_id?: string; kind: string; name: string; mime: string; size?: number }
interface RoomState { room: CanonicalRoom; driver_status?: { pending_actions?: CanonicalPendingAction[] } }

const sameGroupPayload = (payload: Record<string, unknown>, text: string, attachments: Attachment[]) =>
  payload.text === text && JSON.stringify(payload.attachments ?? []) === JSON.stringify(attachments)

function CanonicalGroupRoster({ members = [], label }: { members?: CanonicalRoomMember[]; label: string }) {
  if (!members.length) {return null}

  return <ul aria-label={label} className="flex flex-wrap gap-x-2 text-xs text-(--ui-text-tertiary)">
    {members.map(member => <li key={member.member_id}>{member.display_name || member.handle || member.member_id}</li>)}
  </ul>
}

export function CanonicalGroupWorkspace({ binding, visible = true, onBack }: {
  binding: CanonicalGroupBinding; visible?: boolean; onBack?: () => void
}) {
  // Remount on identity changes: old polls and pending confirmations never cross rooms.
  return <CanonicalRoomView binding={binding} key={JSON.stringify([binding.connectionId, binding.profile, binding.roomId])} onBack={onBack} visible={visible} />
}

function CanonicalRoomView({ binding: initialBinding, visible, onBack }: {
  binding: CanonicalGroupBinding; visible: boolean; onBack?: () => void
}) {
  const [binding] = useState(() => ({ ...initialBinding }))
  const labels = useCanonicalGroupLabels()
  const [state, setState] = useState<RoomState | null>(null)
  const [events, setEvents] = useState<RoomEvent[]>([])
  const eventsRef = useRef<RoomEvent[]>([])
  const [query, setQuery] = useState('')
  const [unseen, setUnseen] = useState(0)
  const seenSeq = useRef(0)
  const [error, setError] = useState('')
  const [readError, setReadError] = useState('')
  const [draft, setDraft] = useState('')
  const [attachments, setAttachments] = useState<Attachment[]>([])
  const [uploading, setUploading] = useState(false)
  const [restored, setRestored] = useState(false)
  const [pending, setPending] = useState<PreparedCanonicalGroupSend | null>(null)
  const [busy, setBusy] = useState(false)
  const busyRef = useRef(false)
  const alive = useRef(true)
  const [discard, setDiscard] = useState<CanonicalPendingAction | null>(null)
  const revision = useRef(0)
  // This window's typed draft while it sends the room's one pending intent another window prepared.
  const heldDraft = useRef<{ text: string; attachments: Attachment[] } | null>(null)

  // eslint-disable-next-line no-restricted-syntax -- journal hydration and mounted lifetime, not a reactive store mirror
  useEffect(() => {
    alive.current = true
    let cancelled = false
    void readCanonicalGroupSend(binding).then(entry => {
      if (cancelled) {return}

      if (entry) {
        setPending(entry)
        setDraft(String(entry.params.payload.text ?? ''))
        setAttachments((entry.params.payload.attachments as Attachment[] | undefined) ?? [])
      }

      setRestored(true)
    }).catch(e => { if (!cancelled) {setError(e instanceof Error ? e.message : String(e))} })

    return () => { cancelled = true; alive.current = false; revision.current++ }
  }, [binding])

  // Keep the log pinned to the newest message unless the user scrolled up to read history.
  const logRef = useRef<HTMLDivElement | null>(null)
  const stickToBottom = useRef(true)

  const [atBottom, setAtBottom] = useState(true)
  const [limit, setLimit] = useState(300)

  // The room can sit inside a host scroll area that, not this log, does the scrolling.
  // Scroll whichever of the two actually overflows.
  const scroller = (): HTMLElement | null => {
    const el = logRef.current

    if (!el) {return null}

    return el.scrollHeight > el.clientHeight + 1
      ? el
      : (el.closest('[data-slot="scroll-area-viewport"]') as HTMLElement | null) ?? el
  }

  const scrollToEnd = () => {
    const el = scroller()

    if (el) {el.scrollTop = el.scrollHeight}
  }

  const onScrolled = () => {
    const el = scroller()

    if (!el) {return}
    const near = el.scrollHeight - el.scrollTop - el.clientHeight < 80
    stickToBottom.current = near
    setAtBottom(near)

    if (near) {setUnseen(0)}
  }

  useEffect(() => {
    const viewport = logRef.current?.closest('[data-slot="scroll-area-viewport"]')

    if (!viewport) {return}
    viewport.addEventListener('scroll', onScrolled, { passive: true })

    return () => viewport.removeEventListener('scroll', onScrolled)
    // eslint-disable-next-line react-hooks/exhaustive-deps -- onScrolled only reads refs and stable setters
  }, [])

   
  const lastSeq = events.at(-1)?.seq
  const pendingCount = state?.driver_status?.pending_actions?.length

  // eslint-disable-next-line no-restricted-syntax -- DOM scroll anchoring plus a new-message counter derived from the scroll position
  useEffect(() => {
    if (stickToBottom.current) {
      scrollToEnd()
      setUnseen(0)
    } else if (seenSeq.current) {
      setUnseen(n => n + events.filter(e => e.seq > seenSeq.current && e.kind.startsWith('message')).length)
    }

    seenSeq.current = lastSeq ?? 0
    // eslint-disable-next-line react-hooks/exhaustive-deps -- events is read for the delta only
  }, [lastSeq, pendingCount])

  // eslint-disable-next-line no-restricted-syntax -- re-anchor when the tab becomes visible again
  useEffect(() => {
    if (visible) {stickToBottom.current = true; scrollToEnd()}
    // eslint-disable-next-line react-hooks/exhaustive-deps -- scrollToEnd only reads refs
  }, [visible])

   
  useEffect(() => {
    const content = logRef.current?.firstElementChild

    if (!content || typeof ResizeObserver === 'undefined') {return}
    const observer = new ResizeObserver(() => { if (stickToBottom.current) {scrollToEnd()} })
    observer.observe(content)

    return () => observer.disconnect()
    // eslint-disable-next-line react-hooks/exhaustive-deps -- scrollToEnd only reads refs
  }, [])

  const refresh = async () => {
    const version = ++revision.current
    const snapshot = await canonicalGroupRequest<RoomState>(binding, 'groups.state', { room_id: binding.roomId })
    // Incremental: only fetch what is newer than the last event we already hold.
    const log: RoomEvent[] = []
    let cursor = eventsRef.current.at(-1)?.seq ?? 0

    for (;;) {
      const page = await canonicalGroupRequest<{ events: RoomEvent[]; has_more?: boolean; next_seq?: number }>(binding, 'groups.log', { room_id: binding.roomId, since_seq: cursor, limit: 100 })
      log.push(...page.events)

      if (!page.has_more) {break}
      const next = page.events.at(-1)?.seq

      if (!next || next <= cursor) {throw new Error(labels.invalidLogCursor)}
      cursor = next
    }

    if (alive.current && version === revision.current) {
      setState(snapshot)

      if (log.length) {
        eventsRef.current = [...eventsRef.current, ...log]
        setEvents(eventsRef.current)
      }

      setReadError('')
    }
  }

  useEffect(() => {
    if (!visible) {return}
    let cancelled = false
    let timer: ReturnType<typeof setTimeout>

    const poll = async () => {
      try { await refresh() } catch (e) { if (!cancelled) {setReadError(String(e instanceof Error ? e.message : e))} }

      if (!cancelled) {timer = setTimeout(() => void poll(), 2000)}
    }

    void poll()

    return () => { cancelled = true; revision.current++; clearTimeout(timer) }
    // The keyed parent freezes the authority binding for this lifetime.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [visible])

  const mutate = async (operation: () => Promise<unknown>) => {
    if (busyRef.current) {return}
    busyRef.current = true
    setBusy(true)
    setError('')

    try { await operation();

 if (alive.current) {await refresh()} }
    catch (e) { if (alive.current) {setError(e instanceof Error ? e.message : String(e))} }
    finally {
      busyRef.current = false

      if (alive.current) {setBusy(false)}
    }
  }

  const send = () => {
    if (!restored || busyRef.current || uploading || !state?.driver_status || (!pending && !draft.trim() && !attachments.length)) {return}
    void mutate(async () => {
      const exact = pending ?? await prepareCanonicalGroupSend(binding, { text: draft, attachments })

      if (!alive.current) {return}

      // Another window won the room's slot: send its intent first, keep this window's words.
      if (!pending && !sameGroupPayload(exact.params.payload, draft, attachments)) {
        heldDraft.current = { text: draft, attachments }
      }

      setPending(exact)
      setDraft(String(exact.params.payload.text ?? ''))
      setAttachments((exact.params.payload.attachments as Attachment[] | undefined) ?? [])
      await canonicalGroupRequest(exact.binding, 'groups.send', exact.params)
      await retireCanonicalGroupSend(exact.binding, exact.params.event_id)

      if (alive.current) {
        const held = heldDraft.current
        heldDraft.current = null
        setPending(null)
        setDraft(held?.text ?? '')
        setAttachments(held?.attachments ?? [])
      }
    })
  }

  const act = (action: CanonicalPendingAction, choice?: 'once' | 'deny') =>
    mutate(() => actCanonicalGroup(binding, action, choice))

  const needle = query.trim().toLowerCase()

  const matched = needle
    ? events.filter(e => String(e.payload.text || e.payload.content || '').toLowerCase().includes(needle))
    : events

  // Only mount the recent tail; hundreds of markdown messages make load and scroll crawl.
  const shown = needle ? matched.slice(-500) : matched.slice(-limit)
  const hidden = matched.length - shown.length

  const jumpToMember = (memberId: string) => {
    const nodes = logRef.current?.querySelectorAll(`[data-author-id="${CSS.escape(memberId)}"]`)
    const node = nodes?.[nodes.length - 1]

    if (node) {
      stickToBottom.current = false
      node.scrollIntoView({ block: 'center' })
    }
  }

  const exportTranscript = () => {
    const roster = new Map((state?.room.members ?? []).map(m => [m.member_id, m.display_name || m.handle || m.member_id]))

    const lines = events.filter(e => e.kind.startsWith('message')).map(e => {
      const who = e.actor?.kind === 'member' ? roster.get(e.actor.id ?? '') ?? e.actor.id : e.actor?.display_name ?? 'You'
      const when = typeof e.created_at === 'number' ? new Date(e.created_at * 1000).toLocaleString() : ''

      return `**${who}** — ${when}\n\n${e.payload.text || e.payload.content || ''}\n`
    })

    const url = URL.createObjectURL(new Blob([`# ${state?.room.name ?? 'Group'}\n\n${lines.join('\n---\n\n')}`], { type: 'text/markdown' }))
    const a = document.createElement('a')
    a.href = url
    a.download = `${(state?.room.name ?? 'group').replace(/[^\w-]+/g, '_')}-transcript.md`
    a.click()
    URL.revokeObjectURL(url)
  }

  const replyTo = (handle: string) => setDraft(text => (text.includes(`@${handle} `) ? text : `@${handle} ${text}`))

  const composerLocked = !restored || busy || !!pending

  return <section className="flex h-full min-h-0 flex-col gap-3 p-3">
    <header className="flex items-center gap-2">
      {onBack && <Button onClick={onBack}>{labels.back}</Button>}
      <div className="min-w-0">
        <h2 className="text-sm font-medium">{state?.room.name || labels.loadingGroup}</h2>
        <CanonicalGroupRoster label={labels.members} members={state?.room.members} />
      </div>
      <input aria-label="Search messages" className="ml-auto min-w-0 max-w-40 rounded border border-(--border) bg-transparent px-2 py-0.5 text-xs" onChange={e => setQuery(e.target.value)} placeholder="Search" type="search" value={query} />
      <Button disabled={!events.length} onClick={exportTranscript}>Export</Button>
      <Button disabled={busy || !state?.driver_status} onClick={() => void mutate(() => canonicalGroupRequest(binding, 'groups.stop', { room_id: binding.roomId, cancel_id: crypto.randomUUID() }))}>{labels.stop}</Button>
    </header>
    <CanonicalGroupStatusStrip events={events} members={state?.room.members} onJump={jumpToMember} />
    {readError && <div role="alert">{readError}<Button onClick={() => void refresh().catch(e => setReadError(String(e)))}>{labels.refresh}</Button></div>}
    {error && <div role="alert">{error}</div>}
    {state && !state.driver_status && <p>{labels.driverUnavailable}</p>}
    <div className="min-h-0 flex-1 overflow-auto [scrollbar-gutter:stable] [&::-webkit-scrollbar-thumb]:bg-[color-mix(in_srgb,var(--dt-scrollbar-thumb)_35%,transparent)]" onScroll={onScrolled} ref={logRef} role="log">
      <div>
        {hidden > 0 && <button className="mx-auto my-1 block text-xs text-(--ui-accent)" onClick={() => setLimit(n => n + 300)} type="button">Show {Math.min(hidden, 300)} earlier ({hidden} hidden)</button>}
        <CanonicalGroupHistory binding={binding} disabled={!visible} events={shown} members={state?.room.members} onReply={replyTo} />
        {!atBottom && <button className="sticky bottom-2 left-1/2 z-10 mx-auto block w-fit rounded-full bg-(--ui-accent) px-3 py-1 text-xs text-white shadow" onClick={() => { stickToBottom.current = true; setAtBottom(true); scrollToEnd(); setUnseen(0) }} type="button">{unseen > 0 ? `${unseen} new message${unseen === 1 ? '' : 's'} ↓` : 'Latest ↓'}</button>}
      </div>
    </div>
    {(state?.driver_status?.pending_actions || []).map(action => <div className="flex items-center gap-2" key={`${action.kind}:${action.task_id}:${action.execution_generation}`}>
      <span>{action.member_id}</span>
      {action.kind === 'discard' && <Button disabled={busy} onClick={() => setDiscard({ ...action })}>{labels.discard}</Button>}
      {action.kind === 'retry' && <Button disabled={busy} onClick={() => void act({ ...action })}>{labels.retry}</Button>}
      {action.kind === 'approval' && <><Button disabled={busy} onClick={() => void act({ ...action }, 'once')}>{labels.allowOnce}</Button><Button disabled={busy} onClick={() => void act({ ...action }, 'deny')}>{labels.deny}</Button></>}
    </div>)}
    {discard && <div aria-label={labels.discardUnknown} role="alertdialog">
      <p>{labels.discardWarning}</p>
      <Button disabled={busy} onClick={() => { const exact = discard; setDiscard(null); void act(exact) }}>{labels.confirmDiscard}</Button>
      <Button onClick={() => setDiscard(null)}>{labels.cancel}</Button>
    </div>}
    {pending && <p role="status">{labels.restoredPendingSend}</p>}
    <form className="flex gap-2" onSubmit={event => { event.preventDefault(); send() }}>
      <CanonicalGroupAttachments attachments={attachments} binding={binding} disabled={composerLocked} onChange={setAttachments} onUploadingChange={setUploading} />
      <Textarea aria-label={labels.groupMessage} className="min-w-0 flex-1" disabled={composerLocked} onChange={e => setDraft(e.target.value)} value={draft} />
      <Button disabled={!restored || busy || uploading || (!pending && !draft.trim() && !attachments.length) || !state?.driver_status} type="submit">{pending ? labels.retry : labels.send}</Button>
    </form>
  </section>
}
