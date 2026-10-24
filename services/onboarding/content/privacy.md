# Privacy notice

> Draft for legal review. Everything marked [TO FILL] must be completed before this notice is used.

This notice explains what personal data Open Source Village collects when you get and use a pass,
why, where it is kept, for how long, and what rights you have.

## Who is responsible

The controller is [TO FILL: legal name of the organisation running Belfry], [TO FILL: address].
Contact for privacy questions: [TO FILL: e-mail address]. [TO FILL: data protection officer, if one
is appointed.]

"Belfry" is the name of the system that runs passes, sign-in and the community apps of Open Source
Village.

## What we collect and why

**Buying a pass.** On the invite page you enter your name, e-mail address and, if you buy as a
company, the company name and VAT number. We create an invoice and a pass record from them, with
the pass type, dates, price and payment status, and we note which steward invited you. We need
this to sell you the pass and to keep the accounts we are required to keep (contract and legal
obligation).

**Payment.** Card payments are processed by Stripe. Stripe receives your card details directly; we
receive the payment status and a payment reference, never your card number. Stripe's own privacy
policy applies to the payment: https://stripe.com/privacy.

**Your account.** When your pass is paid we create:

- a login account (Authentik) with your name, e-mail address and member number (for example
  `OSV-0042`). There is no password: you sign in with a link we e-mail you, or with your pass;
- a Nostr identity: a key pair that lets you use the community apps, and a name like
  `osv-0042@<domain>`. We hold the secret key for you, encrypted. It is derived from a master
  secret kept under the organisers' hardware keys, so that we can recover your key if our servers
  fail. You can take your identity into your own custody at any time from your account page;
  after that we no longer hold a key for you;
- a public profile on the community relay with your display name and that name;
- village tokens (they have no money value) and a link to receive them.

**Your pass in a wallet.** If you add your pass to an EUDI wallet, the pass contains your pass
type and dates, and, shown only when you choose to, your name, member number and Nostr public
key. At the door only the pass type and its validity are checked, never your name. A public
revocation list shows for each pass number whether the pass is still valid; it contains no names.

**The pass on your phone.** The QR code at `/me` contains a pass number and a short-lived code. A
scan shows the pass type and dates, never your name.

**The community apps.** Chat, calendar, directory, marketplace, photos and your tokens work on a
Nostr relay that only people with a valid pass can read and write.

- What you post there (messages, calendar entries, offers, photos, comments, likes, your profile)
  can be seen by other participants. Private chat messages and private calendar entries are
  encrypted so that other participants and the relay cannot read them. While we hold your key,
  our signer decrypts them when you open them in an app; we do not read them. If you want to
  rule that out, take your key into your own custody.
- Token payments between participants (who sent how many tokens to whom) are visible to other
  participants.
- The directory lists you only if you opt in, and only with the profile you give it.
- Photos and files you upload are stored on our file server and can be opened by anyone who has
  their link.
- Your Nostr name (`osv-0042@<domain>`) can be looked up publicly and shows your public key.

**Votes.** Polls are anonymous: your device creates a voting key, and a vote is a cryptographic
proof that it comes from one of the passes, not from which one. We store which passes set up
voting, not who voted how. Our servers do see network addresses and times of requests (see
"Logs").

**Logs.** Our servers record technical logs: times, addresses and actions of requests, sign-ins
and errors. We use them to keep the services running and secure (legitimate interest).

**E-mails.** We send you the activation e-mail, sign-in links and mails about your pass. No
newsletters.

**Cookies and local storage.** We set only the cookies needed to keep you signed in. The apps
store your session, your wallet and your voting key in your browser. There are no tracking
cookies, no analytics and no advertising.

## Where your data is processed

| Service | Provider | Location |
| --- | --- | --- |
| Servers (all of the above) | Hetzner Online GmbH | Germany |
| Outgoing e-mail | OVHcloud | [TO FILL: OVH data centre country, EU] |
| Card payments | Stripe | EU and USA (Stripe's safeguards apply) |
| Backups | [TO FILL: provider of the off-site backup storage] | [TO FILL] |

[TO FILL: data processing agreements with Hetzner, OVHcloud and the backup provider.]

## How long we keep it

| Data | Kept |
| --- | --- |
| Invoices and payment records | as long as accounting law requires: [TO FILL: period, e.g. 10 years] |
| Your account, pass record and Nostr identity | until [TO FILL: e.g. 12 months after the event], then deleted on request or by us |
| Posts, offers, calendar entries, profiles on the relay | until you delete them or until [TO FILL: relay retention]; they stay stored after your pass ends, but people without a valid pass cannot read them |
| Uploaded photos and files | up to one year after they were last opened (video and audio: three months) |
| Sign-in and security logs | [TO FILL: e.g. up to one year (Authentik's event log)] |
| Server and app logs | [TO FILL: e.g. 14 days] |
| Backups | encrypted, [TO FILL: 30] days |

## Your rights

You can ask for access to your data, correction, deletion, restriction of processing, a copy in a
portable format, and you can object to processing based on legitimate interest. Write to
[TO FILL: privacy contact]. Some data we must keep for accounting even if you ask us to delete it.

Note that things you published to other participants may have been copied by them, and that we
cannot recall what you sent to other Nostr relays yourself.

You can complain to a data protection authority, in particular [TO FILL: the competent supervisory
authority].

Last updated: [TO FILL: date]
