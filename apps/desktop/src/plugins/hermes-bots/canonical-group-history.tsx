import * as sdk from '@hermes/plugin-sdk'
import { Fragment } from 'react'

import { type CanonicalGroupAttachment, CanonicalGroupAttachments } from './canonical-group-attachments'
import { useCanonicalGroupLabels } from './canonical-group-labels'
import type { CanonicalGroupBinding, CanonicalRoomMember } from './canonical-groups'

const MessageTextContent = typeof sdk === 'undefined' ? undefined : sdk.MessageTextContent
const Streamdown = typeof sdk === 'undefined' ? undefined : sdk.Streamdown

export interface CanonicalGroupEvent {
  seq: number
  event_id?: string
  room_id?: string
  kind: string
  payload: { text?: string; content?: string; attachments?: CanonicalGroupAttachment[]; member_id?: string; error?: string; reason?: string; reason_code?: string; status?: string }
  actor?: { kind?: string; id?: string; display_name?: string }
  created_at?: number
}

interface CanonicalGroupHistoryProps {
  binding: CanonicalGroupBinding
  events: CanonicalGroupEvent[]
  members?: CanonicalRoomMember[]
  disabled?: boolean
  /** Insert an @mention for this handle into the composer. */
  onReply?: (handle: string) => void
}

const GROUP_WINDOW_S = 5 * 60
const fmtDay = new Intl.DateTimeFormat(undefined, { day: 'numeric', month: 'short', weekday: 'short' })

function eventAuthor(event: CanonicalGroupEvent, roster: Map<string, CanonicalRoomMember>, isMessage: boolean) {
  const memberId = isMessage
    ? event.actor?.kind === 'member' ? event.actor.id : undefined
    : event.payload.member_id

  const member = roster.get(memberId ?? '')

  // The log's label preserves who spoke even if the roster has since changed.
  return (isMessage ? event.actor?.display_name : undefined) || member?.display_name || member?.handle ||
    (isMessage ? event.actor?.id : memberId)
}

const fmtClock = new Intl.DateTimeFormat(undefined, { hour: 'numeric', minute: '2-digit' })
const fmtDayClock = new Intl.DateTimeFormat(undefined, { day: 'numeric', hour: 'numeric', minute: '2-digit', month: 'short' })
const fmtFull = new Intl.DateTimeFormat(undefined, { dateStyle: 'medium', timeStyle: 'medium' })

// Show the real send time (clock today, day + clock otherwise); the relative age
// lives in the tooltip so a stale-looking "1 sec ago" can never stand in for the time.
function EventTime({ timestamp }: { timestamp?: number }) {
  if (typeof timestamp !== 'number') {return null}
  const at = new Date(timestamp * 1000)

  if (!Number.isFinite(at.getTime())) {return null}
  const sameDay = at.toDateString() === new Date().toDateString()

  return <time className="text-[0.625rem] text-(--ui-text-quaternary)" dateTime={at.toISOString()}
    title={`${fmtFull.format(at)} · ${sdk.relativeTime(at.getTime())}`}>{(sameDay ? fmtClock : fmtDayClock).format(at)}</time>
}

function EventBody({ event, notice }: { event: CanonicalGroupEvent; notice?: string }) {
  if (notice) {
    const detail = event.payload.error || event.payload.reason || event.payload.reason_code

    return <p className="text-xs text-(--ui-text-secondary)" data-selectable-text="true">
      <strong>{notice}</strong>{detail && <>: {detail}</>}
    </p>
  }

  const text = event.payload.text || event.payload.content || ''

  return <div className="min-w-0 text-xs text-(--ui-text-secondary) [&_p]:mb-1 [&_p:last-child]:mb-0 [&_ul]:mb-1 [&_ul]:list-disc [&_ul]:pl-4 [&_ol]:mb-1 [&_ol]:list-decimal [&_ol]:pl-4 [&_pre]:whitespace-pre-wrap [&_pre]:wrap-anywhere" data-selectable-text="true">
    {/* The shared renderer (layout and large-message safeguards). media={false}: room text
        may come from another machine, so its MEDIA: paths never resolve against this gateway;
        room files keep their own download path below. */}
    {MessageTextContent ? <MessageTextContent media={false} text={text} />
      : Streamdown ? <Streamdown>{text}</Streamdown> : text}
  </div>
}

export function CanonicalGroupHistory({ binding, events, members = [], disabled = false, onReply }: CanonicalGroupHistoryProps) {
  const labels = useCanonicalGroupLabels()
  const roster = new Map(members.map(member => [member.member_id, member]))

  const notices = new Map([
    ['turn.failed', labels.turnFailed],
    ['turn.cancelled', labels.turnCancelled],
    ['turn.deferred', labels.turnDeferred],
    ['member.unavailable', labels.memberUnavailable],
    ['room.stop_requested', labels.roomStopped],
    ['room.disbanded', labels.roomDisbanded],
    ['authority.lost', labels.authorityLost]
  ])

  let prevDay = ''
  let prev: { author?: string; at: number } | null = null

  return <>{events.map(event => {
    const notice = event.kind === 'room.activity' && event.payload.status === 'bounded'
      ? labels.workLimitReached : notices.get(event.kind)

    const isMessage = event.kind === 'message' || event.kind === 'message.user' || event.kind === 'message.member'

    if (!notice && !isMessage) {return null}
    const author = eventAuthor(event, roster, isMessage)
    const at = typeof event.created_at === 'number' ? event.created_at : 0
    const day = at ? new Date(at * 1000).toDateString() : ''
    const newDay = !!day && day !== prevDay
    const grouped = !newDay && isMessage && !!prev && prev.author === author && at - prev.at < GROUP_WINDOW_S
    prevDay = day || prevDay
    prev = isMessage ? { at, author } : null
    const text = String(event.payload.text || event.payload.content || '')
    const handle = event.actor?.kind === 'member' ? roster.get(event.actor.id ?? '')?.handle : undefined

    return <Fragment key={event.seq}>
      {newDay && <div className="px-2 pt-2 text-center text-[0.625rem] text-(--ui-text-quaternary)" role="separator">{fmtDay.format(new Date(at * 1000))}</div>}
      <div className={`group grid grid-cols-[minmax(0,1fr)] gap-1 px-2 ${grouped ? 'py-0.5' : 'py-1.5'}`}
        data-author-id={event.actor?.id} data-seq={event.seq}>
        <div className="flex items-center gap-2">
          {author && !grouped && <strong className="text-[0.7rem] font-semibold text-(--ui-accent)">{author}</strong>}
          <EventTime timestamp={event.created_at} />
          {isMessage && text && <span className="ml-auto flex gap-2 text-[0.625rem] text-(--ui-text-quaternary) opacity-0 group-hover:opacity-100 focus-within:opacity-100">
            {handle && onReply && <button className="hover:text-foreground" onClick={() => onReply(handle)} type="button">Reply</button>}
            <button className="hover:text-foreground" onClick={() => void navigator.clipboard?.writeText(text)} type="button">Copy</button>
          </span>}
        </div>
        <EventBody event={event} notice={notice} />
        {!!event.payload.attachments?.length && <CanonicalGroupAttachments
          attachments={event.payload.attachments.map(attachment => ({ ...attachment, event_id: event.event_id }))}
          binding={binding} disabled={disabled || !event.event_id || event.room_id !== binding.roomId} readOnly />}
      </div>
    </Fragment>
  })}</>
}
