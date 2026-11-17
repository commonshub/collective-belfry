---
name: belfry-homepage
description: Set what a Belfry shows at its main address (https://<domain>/) - Belfry's join page, the community's existing website through a proxy, or a static site published as an nsite (NIP-5A) on the belfry's own relay and Blossom server. Use when someone asks to put their community's website, homepage or landing page on their belfry, to update it, or to move it from another host.
---

# The homepage of a Belfry

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
- **Members** propose a version: their agent publishes the site as an nsite with the member's own
  key on this belfry, then proposes it. An admin approves it, and the belfry copies it as its own
  root site (NIP-5A copy, with `a`/`A` tags naming the member's site). It also records a snapshot
  (kind 5128) saying who proposed and who approved; `/changelog` shows it.

### A member's agent proposes a version

You need the member's bunker URL from their account page (`https://apps.<domain>`, "Connect to
Nostr App"). Then:

1. Build the site to a folder (`dist/`), with `index.html` at its root.
2. Publish it as a named site `website` with ngit 3:
   ```bash
   ngit account login --bunker-url 'bunker://…' --alias member
   ngit nsite publish dist --id website --title "<Community> website" \
     --blossom-server https://blossom.<domain> --relay wss://relay.<domain> --repo-relay-only --defaults
   ```
   This uploads the files to the belfry's Blossom server (allowed for keys with an active pass) and
   publishes the manifest (kind 35128, `d=website`) to the belfry's members-only relay.
3. Propose it in the **#website** channel (the groups relay, `relays.groups` in
   `https://<domain>/.well-known/belfry.json`, NIP-42 AUTH), signed by the member's key (or your
   own, as their agent), a message with a button an admin taps to publish it:
   ```
   kind 9
   tags  ["h", "website"]
         ["a", "35128:<pubkey hex that published the site>:website"]
         ["button", "publish", "Approve and publish", "primary", "belfry:website-publish"]
   content  what changed, the preview link https://preview.<domain>/p/<npub>@website,
            and that an admin can approve it with the button under this message
   ```
   The text must make sense without the button (older apps show only the text). When an admin
   taps it, the belfry checks they are an admin, copies that version as the website and answers
   under the message ("Published by <name>."); a "Website updated" line follows in #website.
   The button and its answers are in `docs/actions.md` of the Belfry repository. If there is
   no #website channel yet, post with `["h","general"]` instead and say it's for the website.
   (The older route still works: a kind-1 note on `wss://relay.<domain>` with the tags
   `["t","proposal"]`, `["t","website"]`, `["p","<the belfry's pubkey>"]`,
   `["subject","New website: <what changed>"]` and the same `a` tag; it appears at
   `https://belfry.<domain>/website`, where an admin approves it.)
4. Tell the member that an admin needs to approve it.

## A static site as an nsite (an admin publishes it)

The belfry uploads every file to its Blossom server, `https://blossom.<domain>`, and signs a
NIP-5A root site manifest (kind 15128) with its own key on its community relay. The site needs
an `index.html` at its root, and optionally a `404.html`. Dotfiles are skipped.

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
- Upload the blobs to `https://blossom.<domain>`. That needs a key with an active pass (a
  member's own key, or their belfry key through a bunker URL).
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
