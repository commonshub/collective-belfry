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
