// Small line icons (24px grid, 1.75 stroke, currentColor): no emoji in the chrome.
import type { ReactNode } from 'react'

function Svg({ children, size = 22, fill = 'none' }: { children: ReactNode; size?: number; fill?: string }) {
  return (
    <svg className="ico" width={size} height={size} viewBox="0 0 24 24" fill={fill} stroke="currentColor"
      strokeWidth={1.75} strokeLinecap="round" strokeLinejoin="round" aria-hidden="true" focusable="false">
      {children}
    </svg>
  )
}

export const MenuIcon = () => <Svg><path d="M4 7h16M4 12h16M4 17h16" /></Svg>

/** Outline: not following. Filled: following (e-mails come). */
export const BellIcon = ({ on = false }: { on?: boolean }) => (
  <Svg fill={on ? 'currentColor' : 'none'}>
    <path d="M6 9a6 6 0 1 1 12 0c0 4.5 1.5 6.5 2 7H4c.5-.5 2-2.5 2-7Z" />
    <path d="M10 19.5a2 2 0 0 0 4 0" fill="none" />
  </Svg>
)

/** A dot on the envelope when something is unread. */
export const MailIcon = ({ unread = false }: { unread?: boolean }) => (
  <Svg>
    <rect x="3" y="5.5" width="18" height="13" rx="2" />
    <path d="m3.5 7 8.5 6 8.5-6" />
    {unread && <circle cx="19.5" cy="5.5" r="3" fill="var(--accent)" stroke="var(--bg)" strokeWidth={1.5} />}
  </Svg>
)

export const CalendarIcon = () => (
  <Svg><rect x="3.5" y="5" width="17" height="15" rx="2" /><path d="M3.5 10h17M8 3v4M16 3v4" /></Svg>
)

export const GearIcon = () => (
  <Svg size={18}>
    <circle cx="12" cy="12" r="3" />
    <path d="M19.4 15a1.7 1.7 0 0 0 .3 1.8l.1.1a2 2 0 1 1-2.8 2.8l-.1-.1a1.7 1.7 0 0 0-1.8-.3 1.7 1.7 0 0 0-1 1.5V21a2 2 0 1 1-4 0v-.1a1.7 1.7 0 0 0-1.1-1.5 1.7 1.7 0 0 0-1.8.3l-.1.1a2 2 0 1 1-2.8-2.8l.1-.1a1.7 1.7 0 0 0 .3-1.8 1.7 1.7 0 0 0-1.5-1H3a2 2 0 1 1 0-4h.1a1.7 1.7 0 0 0 1.5-1.1 1.7 1.7 0 0 0-.3-1.8l-.1-.1a2 2 0 1 1 2.8-2.8l.1.1a1.7 1.7 0 0 0 1.8.3H9a1.7 1.7 0 0 0 1-1.5V3a2 2 0 1 1 4 0v.1a1.7 1.7 0 0 0 1 1.5 1.7 1.7 0 0 0 1.8-.3l.1-.1a2 2 0 1 1 2.8 2.8l-.1.1a1.7 1.7 0 0 0-.3 1.8V9a1.7 1.7 0 0 0 1.5 1H21a2 2 0 1 1 0 4h-.1a1.7 1.7 0 0 0-1.5 1Z" />
  </Svg>
)

export const SearchIcon = () => <Svg><circle cx="11" cy="11" r="6.5" /><path d="m20 20-4.2-4.2" /></Svg>

/** Add a reaction: a smile with a small plus. */
export const SmilePlusIcon = () => (
  <Svg size={18}>
    <path d="M21 11.5V12a9 9 0 1 1-9-9h.5" />
    <path d="M8.5 14.5s1.3 1.5 3.5 1.5 3.5-1.5 3.5-1.5M9 9.5h.01M15 9.5h.01" />
    <path d="M19 2v6M16 5h6" />
  </Svg>
)

export const PinIcon = () => (
  <Svg size={16}><path d="M12 21s-7-6.1-7-11.5a7 7 0 0 1 14 0C19 14.9 12 21 12 21Z" /><circle cx="12" cy="9.5" r="2.5" /></Svg>
)

export const CloseIcon = () => <Svg size={18}><path d="M18 6 6 18M6 6l12 12" /></Svg>

export const MoreIcon = () => <Svg size={18}><circle cx="5" cy="12" r="1.2" fill="currentColor" /><circle cx="12" cy="12" r="1.2" fill="currentColor" /><circle cx="19" cy="12" r="1.2" fill="currentColor" /></Svg>
export const ReplyIcon = () => <Svg><path d="M9 17 4 12l5-5" /><path d="M20 18v-2a4 4 0 0 0-4-4H4" /></Svg>
export const ThreadIcon = () => <Svg><path d="M4 5h16v10H9l-5 4z" /><path d="M8 9h8M8 12h5" /></Svg>
export const AtIcon = () => <Svg><circle cx="12" cy="12" r="4" /><path d="M16 8v5a3 3 0 0 0 6 0v-1a10 10 0 1 0-4 8" /></Svg>
export const CopyIcon = () => <Svg><rect x="9" y="9" width="12" height="12" rx="2" /><path d="M5 15H4a1 1 0 0 1-1-1V4a1 1 0 0 1 1-1h10a1 1 0 0 1 1 1v1" /></Svg>
export const LinkIcon = () => <Svg><path d="M10 13a5 5 0 0 0 7.5.5l3-3a5 5 0 0 0-7-7l-1.7 1.7" /><path d="M14 11a5 5 0 0 0-7.5-.5l-3 3a5 5 0 0 0 7 7l1.7-1.7" /></Svg>
export const GlobeIcon = () => <Svg><circle cx="12" cy="12" r="9" /><path d="M3 12h18M12 3a14 14 0 0 1 0 18M12 3a14 14 0 0 0 0 18" /></Svg>
export const PencilIcon = () => <Svg><path d="M4 20h4L19 9l-4-4L4 16z" /><path d="M13.5 6.5l4 4" /></Svg>
export const TrashIcon = () => <Svg><path d="M4 7h16M10 11v6M14 11v6M6 7l1 13h10l1-13M9 7V4h6v3" /></Svg>
export const ImageIcon = () => <Svg><rect x="3" y="4" width="18" height="16" rx="2.5" /><circle cx="8.5" cy="9.5" r="1.5" /><path d="m4 18 5-5 4 4 2.5-2.5L20 19" /></Svg>
export const MegaphoneIcon = () => <Svg><path d="M4 10v4h3l8 4V6l-8 4H4zM18 9.5a3.5 3.5 0 0 1 0 5M8 14.5l1 4.5h2.5l-1-3.5" /></Svg>
export const DocIcon = () => <Svg><path d="M7 3h7l4 4v14H7zM14 3v4h4M10 12h5M10 16h5" /></Svg>
export const HexIcon = () => <Svg><path d="M12 3l7.8 4.5v9L12 21l-7.8-4.5v-9z" /></Svg>

export const TodoIcon = () => <Svg><rect x="3" y="3" width="18" height="18" rx="4" /><path d="m8 12 3 3 5-6" /></Svg>
export const PeopleIcon = () => <Svg><circle cx="9" cy="8" r="4" /><path d="M2 21a7 7 0 0 1 14 0M17 4a4 4 0 0 1 0 8M22 21a6 6 0 0 0-4-5.6" /></Svg>
export const ChevronDownIcon = () => <Svg><path d="m6 9 6 6 6-6" /></Svg>
export const ChevronRightIcon = () => <Svg><path d="m9 6 6 6-6 6" /></Svg>
export const ChevronUpIcon = () => <Svg><path d="m6 15 6-6 6 6" /></Svg>

/** A pod's mark: a regular polygon with one side per member (3 at least; from 13 on, a circle). */
export function PodShape({ members, size = 20 }: { members: number; size?: number }) {
  const n = Math.max(3, members)
  const label = `${members} ${members === 1 ? 'member' : 'members'}`
  const common = { className: 'ico pod-shape', width: size, height: size, viewBox: '0 0 24 24', fill: 'none', stroke: 'currentColor', strokeWidth: 2, strokeLinejoin: 'round' as const, role: 'img', 'aria-label': label }
  if (n > 12) return <svg {...common}><circle cx="12" cy="12" r="9" /></svg>
  const pts = Array.from({ length: n }, (_, k) => {
    const a = -Math.PI / 2 + (2 * Math.PI * k) / n
    return `${(12 + 9.5 * Math.cos(a)).toFixed(2)},${((n === 3 ? 13.5 : 12) + 9.5 * Math.sin(a)).toFixed(2)}`
  }).join(' ')
  return <svg {...common}><polygon points={pts} /></svg>
}
export const PushPinIcon = () => <Svg><path d="M12 17v5M9 3h6l-1 6 3 3v2H7v-2l3-3z" /></Svg>
