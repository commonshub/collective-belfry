---
name: belfry-theme
description: Rebrand a belfry's own pages (sign-in, pass, proposals, changelog, status, members, setup, /git) with the community's name, colours, fonts and logo, without forking the code, so updates keep the changes. Also how to choose the code shown at /git.
---

# Rebrand a belfry

A belfry serves two kinds of pages:

- **the homepage** on the apex (`https://<domain>`): the community's own website, published as an
  nsite. See the `belfry-homepage` skill; it is entirely yours.
- **the belfry's pages**: `pass.<domain>` (sign-in, pass, `/proposals`, `/changelog`,
  `/monitor`), `belfry.<domain>` (status, members, setup) and `/git`. The code ships them; this
  skill changes how they look.

## Two layers

1. **Base theme**, in the code: `THEME=belfry` (default for new installs: limestone, ink, bronze,
   light and dark) or `THEME=osv`. Set it with `BELFRY_THEME=belfry` in `belfry config`, or on
   `https://belfry.<domain>/setup`.
2. **The community's own files**, in `infra/data/theme/` on the server. That is data, never in git,
   so `belfry update` never overwrites it (it is in the backups like the rest of `infra/data/`):

| File | What it does |
| --- | --- |
| `custom.css` | loaded after the base theme; set the variables below, or any other rule |
| `logo.svg` / `logo.png` | the logo in the page header (shown next to the community name) |
| `favicon.svg` / `.png` / `.ico` | the browser tab icon |
| `theme.json` | `{"name": "…", "url": "https://…", "footer": "…", "links": [["/help", "Help"]], "wordmark": false}` |
| `*.woff2`, images | anything `custom.css` refers to with `url(file.woff2)` |

The community name comes from `COMMUNITY_NAME` (`BELFRY_NAME`); `theme.json` can override it for
the pages only. `"wordmark": true` shows the logo alone (a logo that already spells the name).

## The variables

```css
:root {
  --bg: #f4f0e8;  --surface: #fffdf8;  --surface-deep: #ebe5d9;
  --fg: #1d1b18;  --fg-muted: #4a453d; --fg-faint: #7a7266;
  --border: #d9d1c2; --border-strong: #bfb5a3;
  --accent: #a4532b; --accent-hover: #8c4522; --accent-2: #b3261e; --on-accent: #fffaf3;
  --ok: #3f7a4f;
  --font-sans: 'Inter', system-ui, sans-serif;
  --font-title: 'Source Serif 4', Georgia, serif;
  --font-mono: 'JetBrains Mono', ui-monospace, monospace;
  --radius: 12px;
}
@media (prefers-color-scheme: dark) { :root { --bg: #171614; /* … */ } }
```

Keep text readable: `--fg` on `--bg` and `--on-accent` on `--accent` need a contrast of at least
4.5:1. Set the dark-mode values too, or pin one scheme with `:root { color-scheme: light }`.

## Steps (an agent with SSH to the server)

1. Make a folder with the files, e.g. `theme/custom.css`, `theme/logo.svg`, `theme/theme.json`.
2. Copy it over and apply it:
   ```sh
   scp -r theme root@<server>:/tmp/theme
   ssh root@<server> belfry theme set /tmp/theme
   ```
   `belfry theme set` keeps only theme files (css, svg, png, jpg, webp, ico, woff2, woff, gif,
   json), checks `theme.json`, keeps the previous files in `infra/data/theme.old`, records the
   change in the changelog and restarts the pages. `belfry theme show` lists what is there;
   `belfry theme reset` goes back to the base theme.
3. Open `https://pass.<domain>/proposals` and `https://belfry.<domain>/` and look at both light and
   dark mode.

Without SSH, ask an admin of the belfry to run step 2; members can propose a theme as a
`/proposals` note with the files attached (Blossom links).

## /git: the code a belfry shows

`/git` shows one repository, read from Nostr (ngit, NIP-34) and fetched from its git server:

- `BELFRY_GIT_REPO=nostr://<npub>/<relay>/<identifier>`: the URL `ngit` prints. The page shows the
  maintainers' latest signed state (kind 30618) and says so ("signed state"), the README, the
  files, the latest commits, how to clone and how to propose a change (`ngit send`, open
  proposals on gitworkshop.dev).
- `BELFRY_GIT_REPO=https://…/repo.git`: any public git URL, without the Nostr parts.
- empty (the default): no `/git`; the path falls through to the homepage, which may have its own.

Set it on `https://belfry.<domain>/setup` or with `belfry config`. `/git` answers on the apex too,
so the community's website has `https://<domain>/git`.
