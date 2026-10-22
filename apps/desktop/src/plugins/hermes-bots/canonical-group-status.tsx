import { useEffect, useState } from 'react'

import type { CanonicalGroupEvent } from './canonical-group-history'
import type { CanonicalRoomMember } from './canonical-groups'

export const STALL_AFTER_MS = 5 * 60_000

const SETTLE_KINDS = new Set(['room.activity', 'turn.cancelled', 'room.stop_requested', 'room.disbanded'])

export interface RoomActivity {
  /** A user message is newer than the last settle/stop event, so replies are still expected. */
  waiting: boolean
  /** Newest event of any kind, ms epoch (0 when the log is empty). */
  lastEventAt: number
  /** Last message time per member id, ms epoch. */
  lastSpoke: Map<string, number>
}

const ms = (event?: CanonicalGroupEvent) => (typeof event?.created_at === 'number' ? event.created_at * 1000 : 0)

export function deriveRoomActivity(events: CanonicalGroupEvent[]): RoomActivity {
  const lastSpoke = new Map<string, number>()
  let lastUser = -1
  let lastSettle = -1

  events.forEach((event, index) => {
    if (event.kind === 'message.user') {lastUser = index}
    else if (SETTLE_KINDS.has(event.kind)) {lastSettle = index}
    else if (event.kind === 'message.member' && event.actor?.id) {lastSpoke.set(event.actor.id, ms(event))}
  })

  return { lastEventAt: ms(events.at(-1)), lastSpoke, waiting: lastUser > lastSettle }
}

export function ageLabel(thenMs: number, nowMs: number): string {
  const mins = Math.max(0, Math.floor((nowMs - thenMs) / 60_000))

  if (mins < 1) {return 'just now'}

  if (mins < 60) {return `${mins} min ago`}
  const hours = Math.floor(mins / 60)

  return hours < 24 ? `${hours} hr ago` : `${Math.floor(hours / 24)} d ago`
}

/** Re-render on an interval so ages and the stall flag stay current. */
export function useNow(intervalMs = 30_000): number {
  const [now, setNow] = useState(() => Date.now())

   
  useEffect(() => {
    const timer = setInterval(() => setNow(Date.now()), intervalMs)

    return () => clearInterval(timer)
  }, [intervalMs])

  return now
}

export function CanonicalGroupStatusStrip({ events, members = [], onJump }: {
  events: CanonicalGroupEvent[]; members?: CanonicalRoomMember[]; onJump?: (memberId: string) => void
}) {
  const now = useNow()
  const activity = deriveRoomActivity(events)
  const quietFor = activity.lastEventAt ? now - activity.lastEventAt : 0
  const stalled = activity.waiting && quietFor > STALL_AFTER_MS

  return <div className="flex flex-wrap items-center gap-x-3 gap-y-0.5 text-[0.65rem] text-(--ui-text-tertiary)" data-slot="group-status-strip">
    {activity.waiting
      ? <span className={stalled ? 'font-medium text-(--ui-warning,orange)' : ''} role="status">
        {stalled ? `Possibly stalled: no activity for ${Math.floor(quietFor / 60_000)} min` : 'Waiting on replies'}
      </span>
      : <span>Idle{activity.lastEventAt ? ` · last activity ${ageLabel(activity.lastEventAt, now)}` : ''}</span>}
    {members.map(member => {
      const spoke = activity.lastSpoke.get(member.member_id)

      return <button className="hover:text-foreground" key={member.member_id} onClick={() => onJump?.(member.member_id)} type="button">
        {member.display_name || member.handle || member.member_id}: {spoke ? ageLabel(spoke, now) : 'not yet'}
      </button>
    })}
  </div>
}
