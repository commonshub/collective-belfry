---
name: belfry-theme
description: Give a belfry the community's own look (name, colours, fonts, logo, favicon) across its pages (sign-in, pass, console, setup, members, proposals, /git) and its apps on apps.<domain> (the chat), without forking the code, so every update keeps the look. Also how to choose the code shown at /git.
---

# Theme a belfry

The code ships **one look, Belfry's**. No community's look is in the code, Open Source Village's
neither. A community's look is a **theme folder**: data on the server, in
`infra/data/theme/`. No update ever touches it (`belfry update` replaces the code, never
`infra/data/`), and it goes into the backups with the rest of `infra/data/`.

What a theme folder changes:

- **the belfry's pages**: `pass.<domain>` (sign-in, pass, `/proposals`, `/changelog`, drop box),
  `belfry.<domain>` (home, status, members, setup, monitor) and `/git`;
- **the apps on `apps.<domain>`** (the apps list, the chat, and every app installed with
  `belfry app install`): each one loads `/theme/custom/own.css`, which is the folder's
  `custom.css` (or empty).

The **homepage** on the apex (`https://<domain>`) is the community's own website, published as an
nsite: see the `belfry-homepage` skill. A theme folder does not change it.

## Never do this

- Don't edit files in the code: `services/onboarding/belfry_onboarding/themes/`, the shipped
  apps in `infra/shipped-apps/`, `infra/proxy/`. The next update overwrites them.
- Don't add a base theme to the code for one community. Everything a community needs fits in
  the folder; if it doesn't, propose a new variable or `theme.json` key upstream instead.

## The folder

| File | What it does |
| --- | --- |
| `custom.css` | loaded after Belfry's stylesheet on every page, and by every app; set the variables below, or any other rule |
| `logo.svg` / `logo.png` / `logo.webp` | the logo in the page header, next to the community name |
| `favicon.svg` / `.png` / `.ico` | the browser tab icon of the pages |
| `theme.json` | `{"name": "…", "url": "https://…", "footer": "…", "links": [["/help", "Help"]], "wordmark": false, "scheme": "light dark"}` |
| `*.woff2`, `*.woff`, images | anything `custom.css` refers to, by its bare name: `url(MyFont.woff2)` |

Flat: no subfolders. `belfry theme set` keeps only css, svg, png, jpg, webp, ico, woff2, woff,
gif and json files.

`theme.json` keys, all optional:

- `name`: the name on the pages (default: `COMMUNITY_NAME`, set on the setup page);
- `url`: where the logo links (default: the homepage);
- `footer`: the footer line (default: "<name>, run by Belfry");
- `links`: the footer links, `[["href", "text"], …]` (default: Proposals, Changelog, Code, This belfry);
- `wordmark`: `true` when the logo already spells the name (then the name isn't repeated);
- `scheme`: `"light"`, `"dark"` or `"light dark"` (the default: follows the device).

## The pages' variables

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

## The apps' variables

The chat (and the other apps built for Belfry) use their own names, set on
`:root[data-theme]`. Write the selector as `:root, :root[data-theme]` so one rule reaches both
the pages and the apps:

```css
:root, :root[data-theme] {
  --bg: #ffffff; --surface: #f4f4f5; --surface-2: #e4e4e7; --line: #e4e4e7;
  --text: #09090b; --muted: #71717a; --faint: #a1a1aa;
  --accent: #2a6f97; --on-accent: #ffffff; --danger: #dc2626;
  --radius: 10px; --font: 'MyFont', system-ui, sans-serif;
}
@media (prefers-color-scheme: dark) {
  :root, :root[data-theme] { --bg: #0b0f12; --text: #f5f5f5; /* … */ }
}
```

`--bg`, `--surface`, `--accent`, `--on-accent` and `--radius` mean the same thing on the pages
and in the apps; set them once.

Keep text readable: text on the background and on the accent needs a contrast of at least 4.5:1,
in light and dark mode. To have one scheme only, set `"scheme"` in `theme.json` and
`color-scheme: light` (or `dark`) in `custom.css`, and give only that scheme's values.

## Steps (an agent with SSH to the server)

1. Make the folder, e.g. `theme/custom.css`, `theme/logo.svg`, `theme/favicon.svg`,
   `theme/theme.json`, and the font files. Keep it in the community's own repository if it has
   one: that is the source, the server holds a copy.
2. Copy it over and apply it:
   ```sh
   scp -r theme root@<server>:/tmp/theme
   ssh root@<server> belfry theme set /tmp/theme
   ```
   `belfry theme set` checks `theme.json`, keeps the previous files in `infra/data/theme.old`,
   records the change in the changelog and restarts the pages. The apps pick up `own.css`
   within five minutes (cache), or right away after a reload without cache.
   `belfry theme show` lists the files; `belfry theme reset` goes back to Belfry's look.
3. Look at the result in light and dark mode, on a phone and a laptop:
   `https://belfry.<domain>/`, `https://pass.<domain>/proposals`, `https://apps.<domain>/` and
   `https://apps.<domain>/chat/`.
4. After an update (`belfry update`), check the same pages: the folder is untouched, but a new
   page may use a variable the theme doesn't set yet; it then shows Belfry's value.

Without SSH, ask an admin of the belfry to run step 2. Members can propose a theme as a
`/proposals` note with the files attached.

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
