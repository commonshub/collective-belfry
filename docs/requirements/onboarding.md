# Onboarding: signing in, applying, links, guests

Prefix `ONB`. How people get in. Contracts: docs/membership.md, docs/guests.md, docs/roles.md,
docs/website-accounts.md, docs/setup.md, docs/agents.md. Canvas: onboarding, inviting.

## Signing in

- **ONB-1** [all] No passwords. A person signs in with their e-mail address (or name@domain /
  username@domain) and a 6-digit code mailed to it. Codes expire, allow a few tries, and only the
  newest works; the app shows "N tries left". → docs/website-accounts.md §1
- **ONB-2** [all] The device makes its own key first; the code binds that key as a session of
  the account. One client key = one identity: a key already linked to another account is refused
  (`key_taken`), never silently moved. (owner, 2026-11-10)
- **ONB-3** [all] Signing out ends the session at the belfry too (the session is revoked, not
  only forgotten locally) and clears the device's own data; the next sign-in makes a new key.
- **ONB-4** [apps] "Almost in" / "Apply to join" is shown only on a definite refusal of
  membership confirmed by `/whois`, never on a timeout, a rate refusal or for a device that was
  ever in. (lead, 2026-11-19 and 2026-11-23, after the blinking join screen)
- **ONB-5** [apps] The start screen offers "Log in" and "Apply to join"; under the apply form,
  "I'm already a member · Log in". (owner, 2026-11-20)
- **ONB-6** [all] The phone apps sign in natively (name@domain → 6 digits); the browser stays as
  "instead". → docs/website-accounts.md, POST /api/v1/session

## Applying and member links

- **ONB-10** [all] Applying happens inside the chat: a message + the charter's agreements → e-mail
  → 6-digit code → "Your request is pending". Nothing is stored before the code is right.
  (owner, 2026-11-19: the old flow "was a nightmare of so many redirections … so many emails")
- **ONB-11** [all] Approved: the open tab turns into the chat by itself; otherwise ONE welcome
  mail with chat.<domain> written out and a "Chat with the community" button. Never more mails
  than needed, never a detour through another host or an old-looking page. (owner, 2026-11-19)
- **ONB-12** [all] The pending screen shows the name and @username, the person's own e-mail
  address, their message, and: once approved, you are logged in in this browser; elsewhere, enter
  your e-mail to receive a new code. Nothing about keeping the tab open. (owner, 2026-11-20)
- **ONB-13** [all] A member link asks only the e-mail address and the code, then the name and
  username screen. (owner, 2026-11-19) → docs/roles.md
- **ONB-14** [all] A belfry's membership settings decide who invites members (admins | members)
  and how requests are accepted (by stewards | by N vouches). Apps read `may_invite_members`
  first; a person who may not make member links gets a guest link and the line "Only stewards
  invite new members here". (lead, 2026-11-19) → docs/membership.md
- **ONB-15** [all] An accepted applicant is a member, never an admin. Making someone a steward is
  an explicit act with a confirmation. (lead, 2026-11-22)
- **ONB-16** [all] When someone joins the belfry, Townsquare shows one line "‹name› joined ‹the
  belfry›" so everyone sees who entered the room. (owner, 2026-11-22) → docs/one-shape.md

## Names and usernames

- **ONB-20** [all] Apply and member-link sign-up ask the name and "Pick a username:", default =
  the first name (taken → first-last, then -2), with a live check and one-tap suggestions; a
  reserved name says "That name is kept for the belfry". (owner, 2026-11-20)
- **ONB-21** [all] The username is the person's address everywhere: their NIP-05 name, their
  mailbox (username@domain) and how they log in on the phone apps. Explain it with the field:
  log in with it, mentions, direct messages and e-mails. (owner, 2026-11-20)
- **ONB-22** [all] A chosen username is never replaced by an automatic one; guests keep guest-N
  until they become members. → services usernames.py
- **ONB-23** [all] The welcome mail says: log in with username@domain on the Android or iPhone
  app, with a 6-digit code to this e-mail address, and links to belfry.sh/android and
  belfry.sh/iphone. (owner, 2026-11-20)
- **ONB-24** [all] No profile step in the first run: the first run ends in the chat.
  (owner, 2026-11-19, canvas v42)

## Link your phone

- **ONB-30** [apps] Accounts → "Link your phone" (Link a phone or tablet) shows a QR of
  `https://chat.<domain>/login/<username>` (no secret in it) plus links to the Android and iPhone
  apps; the phone that scans it shows "Log in as username@domain" and asks for the code mailed
  to the person. (owner, 2026-11-20)

## Guest links and guest mode

- **ONB-40** [all] A guest link opens the channel at once in guest mode (read only on the device;
  the composer says "Guest mode" with a Join button). Joining = name + e-mail + code.
  → docs/guests.md
- **ONB-41** [all] Reading guests are invisible: no join line, not in member lists, counts,
  "active" numbers or mention pickers until they join.
- **ONB-42** [all] An invite defaults to one person for one day (options up to 50 people; 1 week,
  1 month). (owner, 2026-11-09) A guest's own access lasts 30 days after joining (to confirm:
  asked of the owner, never answered).
- **ONB-43** [all] The right code for an address that already has an account signs that account
  in on this device (the guest link's channel is added to it); no second account. (owner's
  canvas comment, 2026-11-19)
- **ONB-44** [all] Opening a one-person link spends it. Said plainly to the owner; changing it
  (spending only at the code) is an open offer. (to confirm)
- **ONB-45** [all] From a channel the invite sheet offers "Someone who is here already" first,
  then "Or invite an external person"; from Members: "Invite to ‹community›". An external invite
  starts with the role (Member default | Steward for admins only), for one person and 24 hours.
  (owner, 2026-11-18 / 2026-11-19, canvas inviting/*)
- **ONB-46** [all] Joining is idempotent per signing key: the same device tapping again gets the
  same guest, never a new account. (lead, 2026-11-09)
- **ONB-47** [all] Guests stay out of encrypted conversations for now. → encryption.md ENC-60

## Members-only links while signed out

- **ONB-50** [apps] A signed-out link to a members-only event leads to sign-in at once, then
  opens the event; the link is kept for a week across the sign-in. (owner, 2026-11-23)

## First run of a belfry

- **ONB-60** [server] Setting up a new belfry needs no code: first come, first served;
  `belfry setup-reset` undoes a wrong claim. "Don't reopen this unless something changes."
  (owner, 2026-11-13) → docs/setup.md
- **ONB-61** [server] Until a website is published the homepage shows the community's logo,
  name, description and "Open the chat"; with no admin yet, "Set up this belfry".

## Agents

- **ONB-70** [all] An agent joins with its own key, through `/agents.md` or an agent invite link;
  an admin approves it as the admin's own agent. "There should be no bunker involved for an
  agent." (owner, 2026-11-18) → docs/agents.md
- **ONB-71** [all] Any member adds their own agent from their own profile ("Add an agent"); a
  member's agents fold under the member's row ("+ N agents"). (owner, 2026-11-16, 2026-11-21)
- **ONB-72** [all] An agent can't invite, be an admin or have agents of its own.

## Website accounts

- **ONB-80** [all] On the community's own website everyone logs in as a website session (code
  asked up front, no confirmation links); the website never gets a signer session (no
  `bunker_url`). (owner via the osv agent, 2026-11-20) → docs/website-accounts.md
