# E-mail as messages

A belfry receives mail for its members and channels, and members answer from the chat.

| Address | Goes to |
|---|---|
| `<username>@<domain>` | the member (active pass), in the chat's **Messages** |
| `<channel>@<domain>` | the channel, pod or event chat, posted as the member who sent it (members only, and the mail must pass DKIM or SPF aligned with its From). The post carries `client belfry-mail`, a `subject` tag and `mail-original` (the whole e-mail, AES-GCM on Blossom: url, key, nonce); the chat shows it as the member's message with a "via e-mail" tag and a "Full e-mail" link |
| `postmaster@`, `abuse@` | the admins' Messages |

Usernames win over channel names. Anything else is refused at `RCPT TO` (550).

## Protocol

There is no numbered NIP for e-mail. Belfry follows **Nostr Mail** ([nogringo/nostr-mail](https://github.com/nogringo/nostr-mail)), the convention Nostr mail clients use:

- an e-mail is a kind **1301** rumor whose content is the RFC 2822 message, gift-wrapped with NIP-59 like a NIP-17 DM, and published on the recipient's DM relays (kind 10050: the community relay);
- the belfry is the bridge, `_smtp@<domain>` in its nostr.json (the belfry key);
- incoming mail: a 1301 from the bridge with `mail-from` (the legacy sender), plus Belfry's `belfry-auth` tag (`pass` when DKIM or SPF aligned with From). A message over 60 kB is stored on Blossom encrypted with AES-GCM; the rumor carries `x`, `url`, `decryption-key` and `decryption-nonce` (Nostr Mail's large-MIME scheme);
- outgoing mail: the member's app wraps a 1301 with `email-id`, `mail-from` (their `username@domain`) and `rcpt-to` to the bridge, and a copy to themselves. The belfry checks that the address is the sender's and that the From header matches, removes Bcc, sends through the belfry's SMTP server (the same one Odoo uses) and answers with a kind **7679** delivery report (no `p` tag, encrypted to the sender with a throwaway key). The reports are also how a restart knows what was already sent.

Only what the sender wrote is kept in the message: the quoted history (Gmail, Outlook, Apple Mail, Thunderbird; English, French, German, Spanish, Dutch, Italian, Portuguese headers), the signature after `-- ` and phone footers ("Sent from my iPhone") are cut. The whole e-mail stays one tap away.

The chat app reads NIP-17 (kind 14), old NIP-04 (kind 4) and Nostr Mail in one Messages list.

## Running it

- `mail-in` (Compose, `belfry_onboarding/mailfront.py`) listens on port 25. It runs as Caddy's user and uses Caddy's certificate for STARTTLS. It keeps nothing: every address and message goes to the onboarding service (`/internal/mail/*`, hook token), and a sender whose mail could not be delivered tries again later (4xx).
- DNS: `MX <domain> 10 <domain>.` (the apex already points at the box). Sending needs nothing more than what the mail provider already asked for (SPF/DKIM for the sender domain).
- The firewall: install.sh opens 25/tcp. Docker publishes it anyway.
- `MAIL_IN=0` in `infra/.env` turns receiving and the bridge off.
- Limits: 15 MB per message (`MAIL_MAX_BYTES`), 20 recipients, 20 messages per sender address per 10 minutes, 60 sent mails per member per hour.

## Not yet

- No content spam filter. Mail from unknown senders reaches members marked "unverified sender" when it has no aligned DKIM or SPF. Channels already require a member's proven address.
- SPF is skipped for mail arriving over IPv6. Docker's proxy hides the sender's address there, so DKIM is what counts.
- Members can't attach files to outgoing mail yet, and the bridge doesn't send large (Blossom) outgoing mail.
- A NIP-17 DM between members is not copied to e-mail.
