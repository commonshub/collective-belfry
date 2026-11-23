# Following a channel by e-mail

`services/onboarding/belfry_onboarding/followers.py`. Anyone with a proven address can follow a
channel (a channel, a topic, a group or an event: a NIP-29 group) by e-mail. A follow is one
`belfry.follower` row per contact and channel (the register, or Odoo's `belfry_pass`).

## Frequencies

| `frequency` | When | What |
|---|---|---|
| `asap` | a few minutes after each post (`ASAP_DELAY`) | the channel's **announcements** (kind 9 tagged `t announcement` by one of its admins) |
| `weekly` (default) | Mondays, the first round after `DIGEST_HOUR` in `DIGEST_TZ` | a **summary** of the channel |
| `monthly` | the 1st of the month, same hour | the same **summary**, over the month |

Weekly is the default since 2026-11-23 (the owner: "ask whether you want a weekly or monthly
summary by email. Default to weekly"): a follow made without a frequency, the website form
without one, and a confirmation link opened without `f=` are weekly. Rows made earlier keep
their frequency.

Monthly is a full summary too (it was a digest of announcements only before): weekly and
monthly differ only in their period. Each morning is claimed once (`belfry_follow_claim_day(day,
what)`, `what` = `monthly` | `weekly`, kept apart, so a Monday that is the 1st sends both).

## A summary

- The channel's new messages of the period: kind 9 in the group, newest first, without deleted
  ones (5 by the author, 9005), the chat's cards (events, proposals, new groups), e-mail cards
  and the belfry's own "started from a printed card" line; the author's latest edit (1010)
  applied. Thread replies (1111) are not listed.
- Each: the author's **name** (kind 0; "Somebody" without a profile, never a key), the time in
  `DIGEST_TZ`, the first ~200 characters (mentions as @Name, links clickable), "Open in the
  chat" (`https://chat.<domain>/#/c/<channel>/m/<id>`).
- At most 20 (`SUMMARY_MAX`), then "and N more in #channel" linking to the channel.
- "N new people started following #channel this week/month" when some did.
- Period = since the follower's last mail (`sent_until`), at most 8 days (weekly) / 32 days
  (monthly) back. Nothing new for that follower: no mail (and `sent_until` stays).
- Who gets the whole channel: a contact with an active pass that is not a guest's (members read
  every channel that is not private) or a guest pass of that channel. Anyone else (someone who
  follows `general` from the website, a guest of another channel) gets the same mail with only
  the channel's announcements, so nothing members-only reaches outside.
- **Private groups** (`private` in the group's 39000) get no summary at all, nor does a channel
  with no metadata. `asap` announcements are unchanged.
- Footer: the frequency in bold, the other two and unsubscribe as links to `/follow?t=…&f=…`
  (a page with a button); RFC 8058 one-click `List-Unsubscribe`.

## Where a frequency is taken

- `POST /follow` (website form, `general`): `email`, optional `frequency` (`asap` | `weekly` |
  `monthly`, default weekly) rides in the confirmation link.
- `/follow?t=…&f=…` and `POST /follow/set`: `asap` | `weekly` | `monthly` | `stop`.
- `POST /api/me/follows` (NIP-98, a member): `{"channel", "frequency"?}`, frequency `asap` |
  `weekly` | `monthly` | `stop`, default `weekly`.
- `POST /api/v1/cards/<id>/claim` and `/claim-verify` (a topic card): optional `summary`:
  `weekly` (default) | `monthly`; the person who claims follows the new topic at that frequency
  with their proven address; the answer carries `"summary"` when the follow was saved. Group
  cards make no follow. Anything else: 422 `invalid`, `field: "summary"`, before anything is made
  or a code spent.
- `POST /api/v1/guest/save/verify` (a guest proving their address, e.g. a topic card's "Follow by
  e-mail": join with the card's guest link, then save): optional `summary` `weekly` | `monthly`
  follows the channel(s) of the guest's passes; the answer carries `"summary"`. When the address
  has an account and the device signs in to it (`session_key`), that account follows the
  channel the guest was reading. Not given: no follow (older clients unchanged).
