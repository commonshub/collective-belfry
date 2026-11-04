---
name: belfry-homepage
description: Set what a Belfry shows at its main address (https://<domain>/) - Belfry's join page, the community's existing website through a proxy, or a static site published as an nsite (NIP-5A) on the belfry's own relay and Blossom server. Use when someone asks to put their community's website, homepage or landing page on their belfry, to update it, or to move it from another host.
---

# The homepage of a Belfry

The apex of a belfry (`https://<domain>/`) is the community's front door. One setting,
`HOMEPAGE` in `infra/.env`, decides what it shows:

| `HOMEPAGE` | The apex shows | Use when |
| --- | --- | --- |
| *(empty)* | Belfry's start page: what the community is, how to join | there is no website yet |
| `https://site.example` | that website, through a proxy; visitors stay on `https://<domain>` | the site lives elsewhere (Vercel, Netlify, WordPress, GitHub Pages) and keeps being edited there |
| `nsite` | a static site published by the belfry itself (the belfry's own key, `belfry_nsec`) | the community wants its site on its own infrastructure |
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

## A static site as an nsite (the belfry publishes it)

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
3. The first time, set `HOMEPAGE=nsite` (above). Later publishes need nothing else: the apex
   picks up a new manifest within a minute.

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
  `/help`, `/privacy`, `/terms`, `/refunds`, `/rules`, `/brand/…`, `/signin/…`, `/welcome/…`

These redirect to `pass.<domain>`.
