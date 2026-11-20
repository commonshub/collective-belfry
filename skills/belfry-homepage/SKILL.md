---
name: belfry-homepage
description: Set what a Belfry shows at its main address (https://<domain>/) - Belfry's join page, the community's existing website through a proxy, or a static site published as an nsite (NIP-5A) on the belfry's own relay and Blossom server. Use when someone asks to put their community's website, homepage or landing page on their belfry, to update it, or to move it from another host.
---

# The homepage of a Belfry

> **Agents:** this skill changes. Fetch it again once a day (`https://belfry.<domain>/skills/homepage.md`)
> and follow the newest text; don't work from an older copy.

The apex of a belfry (`https://<domain>/`) is the community's front door. One setting,
`HOMEPAGE` in `infra/.env`, decides what it shows:

| `HOMEPAGE` | The apex shows | Use when |
| --- | --- | --- |
| `nsite` *(new installs)* | the website the belfry publishes under its own key; until one is published, a placeholder that explains how to publish it | the community's site lives on its own infrastructure (the default) |
| *(empty)* | Belfry's start page: what the community is, how to join | older belfries; set it explicitly for a join page on the apex |
| `https://site.example` | that website, through a proxy; visitors stay on `https://<domain>` | the site lives elsewhere (Vercel, Netlify, WordPress, GitHub Pages) and keeps being edited there |
| `nsite:<npub>` or `nsite:<npub>/<name>` | the root site (kind 15128) or named site (kind 35128, `d` = name) of another key | a member or a designer publishes the site with their own key and nsite tool |

Whatever it is set to, the apex keeps:
- Belfry's discovery files (`/.well-known/belfry.json`, `/.well-known/nostr.json`);
- the short names (`/chat`, `/calendar`, `/apps`, …), which redirect to the apps.

Belfry's own pages that used to answer on the apex (`/me`, `/apply`, `/claim/…`) redirect to
`https://pass.<domain>`, and `https://pass.<domain>/` shows the join page. Link to it from the homepage
("Join", "Member area").

The homepage runs on the apex's origin, so only point it at a site the community controls.

## Set it (on the server)

```bash
printf 'BELFRY_ENV_HOMEPAGE=%s\n' 'https://site.example' > /tmp/homepage.env && belfry config /tmp/homepage.env
```

`belfry config` writes the setting and restarts what needs it. Without the `belfry` command:
set `HOMEPAGE=` in `/opt/belfry/infra/.env`, then
`docker compose -f infra/docker-compose.yml -f infra/docker-compose.prod.yml --profile app up -d onboarding`.
Check: `curl -sI https://<domain>/` and, for nsites,
`docker compose … exec -T onboarding python -m belfry_onboarding homepage-status`.

## A proxied site

- Give the full `https://` URL of the site's home. Paths and query strings follow, so
  `https://<domain>/about` shows `https://site.example/about`.
- Redirects to the site's own host become relative, so visitors don't leave the belfry's address.
- The site's own Content-Security-Policy is kept. Without one, only framing is restricted.
- If the host rejects unknown `Host` headers or redirects to its canonical domain, the page
  loops or errors. Use the host's own deployment URL (for example `https://project.vercel.app`),
  not a custom domain that redirects.

## Who publishes, who approves

- **Admins** (the stewards group) publish directly from the server (below), or approve a member's
  version with the button under its message in #website (or at `https://belfry.<domain>/website`).
- **Members and their agents** propose a version: the agent publishes the site as an nsite with
  **its own key** on this belfry, then proposes it. An admin approves it, and the belfry copies it as its own
  root site (NIP-5A copy, with `a`/`A` tags naming the member's site). It also records a snapshot
  (kind 5128) saying who proposed and who approved; `/changelog` shows it.

### A member's agent proposes a version

**You use your own identity, never the member's.** An agent keeps its own Nostr key (make one
once, store it privately, reuse it; never ask a person for their key, a sign-in code or a
`bunker://` link). If you are not in this belfry yet, join first with that key:

- the member gives you an **invite link for an agent** (in the chat: their profile → "Add an
  agent"). Open `https://belfry.<domain>/invite/<token>.md` and follow it: one signed request
  and you are in, as that member's agent; or
- with no link, ask to join: `https://belfry.<domain>/agents.md` (an admin lets you in).

Everything below is signed with your key; people see it is the agent of that member. Then
decide **what you are changing**:

- **Only a part of the site** (the usual case: you look after `/ios/screenshots/`, a page, a
  folder of downloads): make a **scoped** proposal. Prefer it whenever you own only a part of
  the site. Publishing it changes only that path; nothing else on the website is touched, even
  if your copy of the rest is old.
- **The whole site** (a redesign, a new generator): an unscoped proposal replaces every file.
  Start from the **current live site**, never from an old copy, or you undo other people's
  changes. Admins judge these more carefully.

One proposal per responsibility: don't bundle unrelated paths (screenshots and a pricing page)
in one proposal; an admin may want one and not the other.

**A path** is a folder, starting and ending with `/` (`/ios/screenshots/`), or one file
(`/android/index.html`; a file name has an extension). Letters, digits, `-`, `_` and `.`; no
empty, `.`, `..` or hidden (`.git`) parts; at most 200 characters, at most 10 paths. No path
(or `/`) means the whole site. A proposal with an invalid path is **not** published (the belfry
says so under it); it never falls back to the whole site.

1. Build the files to a folder (`dist/`), **at their real paths**:
   - scoped: only that subtree, for example `dist/ios/screenshots/*.png` for `/ios/screenshots/`.
     It needs no `index.html` of its own; files outside the path are ignored.
   - whole site: everything, with `index.html` at its root. To start from the live site, read
     its manifest (the belfry's root site: kind 15128 by the belfry's key, `nostr` in
     `https://<domain>/.well-known/belfry.json`, on `wss://relay.<domain>`) and download each
     `path` from `https://blossom.<domain>/<sha256>`; or ask the admin for the source.
2. Publish it as a named site `website` with ngit 3, logged in with your own key (keep it out
   of the command line and the shell history: read it from a file only you can read):
   ```bash
   ngit account login --nsec "$(cat ~/.secrets/agent.nsec)" --alias agent
   ngit nsite publish dist --id website --title "<Community> website" \
     --blossom-server https://blossom.<domain> --relay wss://relay.<domain> --repo-relay-only --defaults
   ```
   This uploads the files to the belfry's Blossom server (allowed for keys with an active pass) and
   publishes the manifest (kind 35128, `d=website`) to the belfry's members-only relay. For
   several parts of the site, use one site name each (`--id ios-shots`, at most 13 characters)
   so the previews don't overwrite each other.
3. Propose it in the **#website** channel (the groups relay, `relays.groups` in
   `https://<domain>/.well-known/belfry.json`, NIP-42 AUTH), signed by your key, a message
   with a button an admin taps to publish it:
   ```
   kind 9
   tags  ["h", "website"]
         ["a", "35128:<pubkey hex that published the site>:website"]
         ["path", "/ios/screenshots/"]        ← one per path; none for the whole site
         ["button", "publish", "Approve and publish", "primary", "belfry:website-publish"]
         ["imeta", "url https://blossom.<domain>/<sha256>.png", "m image/png", "alt /ios/ on a phone, light"]  ← one per screenshot
   content  "Website: /ios/screenshots/ — new onboarding screens." then what changed,
            the screenshot URLs, the preview link https://preview.<domain>/p/<npub>@website,
            and that an admin can approve it with the button under this message
   ```
   **Always attach screenshots of what changed**: each changed page on a phone (about 390 px
   wide) and on a laptop (about 1280 px), in light and dark if the page has both. Take them from
   the preview link, upload them to `https://blossom.<domain>` (BUD-02), and add one `imeta` tag
   per picture; put the same URLs in the text for apps that don't show `imeta`. Do the same for
   every new version of a proposal. An admin approves what they can see.
   Start the text with the path it is for ("Website: /ios/screenshots/ — …", or "Website: the
   whole site — …"): the path decides what an admin is approving. The text must make sense
   without the button (older apps show only the text). When an admin taps it, the belfry checks
   they are an admin and publishes that version (only the message's paths when it has `path`
   tags), then answers under the message ("Published by <name>." or "Published by <name>: only
   /ios/screenshots/."); a "Website updated" line with what changed follows in #website. The
   button and its answers are in `docs/actions.md` of the Belfry repository. If there is no
   #website channel yet, post with `["h","general"]` instead and say it's for the website.

   **Or as a proposal** (what the chat's "Propose as the website" makes; it is discussed, and
   goes live when a belfry admin marks it Passed): a NIP-23 kind 30023 on `wss://relay.<domain>`
   with `["d","proposal-<x>"]`, `["title","Website: /ios/screenshots/ — new onboarding
   screens"]`, `["t","proposal"]`, `["t","website"]`, `["website","nsite:<npub>/website"]` (or
   the preview link), `["h","prop-<x>"]` (its own chat, a group you create) and the same
   `["path", …]` tags; its card in #website is a kind 9 with `["proposal","prop-<x>"]` and
   `["a","30023:<pubkey>:proposal-<x>"]`. Keep the `path` tags on every new version.
4. **What happens next.** For every version of a proposal (30023), the belfry posts in the
   proposal's chat what publishing it would change, from the two manifests (a button message
   gets no such note, so its text must say what changes): "This changes only /ios/screenshots/: 9 files added, 3 changed, 1 removed. The rest
   of the website stays as it is." or "This replaces the whole website: 4 changed, 2 added,
   0 removed, in / and /android/." Read it: if it says more than you meant, fix the version
   before an admin approves. A scoped publish is a **scoped replace**: inside the path, the
   website gets exactly your files (a live file there that your version lacks is removed);
   outside it, nothing changes. It needs a live website already (publish a whole site first).
5. Tell the member that an admin needs to approve it.

(The older route still works for whole sites: a kind-1 note on `wss://relay.<domain>` with the
tags `["t","proposal"]`, `["t","website"]`, `["p","<the belfry's pubkey>"]`,
`["subject","New website: <what changed>"]` and the same `a` tag; it appears at
`https://belfry.<domain>/website`, where an admin approves it. It has no paths.)

### An admin publishes a part from the API

`POST https://belfry.<domain>/api/v1/website/publish` (NIP-98 by an admin's key) with
`{"ref": "nsite:<npub>/website", "paths": ["/ios/screenshots/"]}`; without `paths`, the whole
site. Answers `{"ok": true, "files", "paths", "added", "changed", "removed", "manifest",
"uploaded"}`, 422 `invalid_paths` or `no_site`, 409 `not_published` with the reason.

## A static site as an nsite (an admin publishes it)

The belfry uploads every file to its Blossom server, `https://blossom.<domain>`, and signs a
NIP-5A root site manifest (kind 15128) with its own key on its community relay. The site needs
an `index.html` at its root, and optionally a `404.html`. Dotfiles are skipped.

**One page for every address under a folder.** A file named `_.html` in a folder answers the
addresses one step under that folder that have no file of their own: with `/villagers/_.html`,
`/villagers/kim-cook` (and `/villagers/kim-cook/`) shows that page with status 200, and the
page's script reads the name from `location.pathname` (people's profiles:
`GET /api/v1/villagers/<slug>`, docs/website-accounts.md). A file that exists wins; deeper
addresses (`/villagers/kim/photos`), names ending in a file extension the site uses (`.png`)
and the root (`/_.html`) are not covered. It works on the apex and in previews.

1. Build the site to a folder (`dist/`, `public/`, `_site/`).
2. Publish from the server:
   ```bash
   tar -C dist -c . | docker compose -f infra/docker-compose.yml -f infra/docker-compose.prod.yml \
     --profile app exec -T onboarding python -m belfry_onboarding homepage-publish --title "Example Commons"
   ```
   It prints the number of files, how many were new, the manifest's id and the npub that signed
   it. From a laptop, stream the tar over SSH: `tar -C dist -c . | ssh root@<host> 'cd /opt/belfry && docker compose … exec -T onboarding …'`.
3. New installs already have `HOMEPAGE=nsite`; older ones set it once (above). Later publishes need
   nothing else: the apex picks up a new manifest within a minute. With the `belfry` command:
   `BELFRY_BY="Ana via Claude" belfry homepage publish dist --title "Example Commons"`.

## An nsite published with another key

Any NIP-5A tool works, for example [nsyte](https://github.com/sandwichfarm/nsyte):
- Upload the blobs to `https://blossom.<domain>`. That needs a key that is in the belfry (a
  member's own key, or their agent's own key).
- Publish the manifest to `wss://relay.<domain>` (NIP-42 AUTH as a member).
- Set `HOMEPAGE=nsite:<npub>` (or `nsite:<npub>/<name>` for a named site).

If the manifest is on other relays instead, list them in `HOMEPAGE_RELAYS` (space separated).
Every manifest is checked against its signature. Every file is checked against its sha256 and
fetched first from the belfry's Blossom server, then from the manifest's `server` tags and the
author's kind 10063 list.

## Paths a homepage can't use

The belfry's own pages take these paths on the apex:

- `/.well-known/belfry.json`, `/.well-known/nostr.json`
- `/chat`, `/groups`, `/calendar`, `/programme`, `/vote`, `/people`, `/market`, `/photos`,
  `/wallet`, `/apps`, `/shop` (the short names)
- the onboarding pages: `/me`, `/apply`, `/claim/…`, `/check/…`, `/door`, `/feedback`,
  `/help`, `/privacy`, `/terms`, `/refunds`, `/rules`, `/brand/…`, `/signin/…`, `/welcome/…`,
  `/proposals`, `/changelog`, `/monitor`, `/drop/…`, `/invite/…`, `/api/…` and every other path
  the onboarding service answers

These redirect to `pass.<domain>`. Four stay on the apex: `/follow` (the e-mail form's target),
`/events` and `/events/…` (the community's events: their public pages, `events.py`), `/theme/…`
and, when a repository is set, `/git`. A website with its own `/events` page loses it to the
belfry's: link to `https://<domain>/events` instead. Every app installed with `belfry app install`
also takes `/<name>/` (the chat: `/chat` redirects to `chat.<domain>`).
