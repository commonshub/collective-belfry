# Open Source Village brand

Every page a participant sees says **Open Source Village, run by Belfry** and uses the look of
[opensourcevillage.org](https://opensourcevillage.org): dark theme only, green greys, a lime
accent, Geist. Belfry stays the name of the operator (footers,
issuer display name, `iss`, internal identifiers); Odoo's back office keeps Odoo's look.

This folder is the single source. The onboarding service serves it at `/brand/*`
(`pass.<domain>/brand`, `http://localhost:8100/brand` locally; Compose mounts `../brand` at
`/brand`, env `BRAND_DIR`), and everything else reads it from there or carries a copy.

## Palette (`tokens.css`)

| Token | Value | Use |
| --- | --- | --- |
| `--osv-background` | `#2b3a31` | page background, `theme-color` |
| `--osv-surface` | `#1a231d` | cards, inputs, mail card |
| `--osv-surface-deep` | `#121914` | code blocks |
| `--osv-border` | `#ffffff29` | borders (solid equivalent on the background: `#44534a`) |
| `--osv-foreground` | `#ffffff` | text |
| `--osv-foreground-muted` | `#c9d1cb` | secondary text |
| `--osv-foreground-faint` | `#8e9a91` | footers, placeholders |
| `--osv-accent` | `#d6ff52` | buttons, links, success |
| `--osv-accent-hover` | `#c5ee41` | |
| `--osv-on-accent` | `#1a231d` | text on a lime button |
| `--osv-accent-2` | `#f89b0e` | warnings, refusals |

Fonts: `--osv-font-sans` Geist (400, 500, 600), `--osv-font-mono` Geist Mono (400); `fonts.css`
declares them from `fonts/` (latin and latin-ext, SIL OFL 1.1, `fonts/LICENSE-Geist.txt`).

## Assets

`osv-logo-horizontal-white.svg` (header wordmark, 28 px high), `osv-mark-white.svg`,
`favicon.svg` / `favicon.ico` / `favicon-96x96.png` / `apple-touch-icon.png` /
`web-app-manifest-{192x192,512x512}.png` (from the site), `osv-logo-horizontal-dark.png`
(wordmark on the background, 1440×280: mails, Odoo company logo; rendered from the SVG),
`flow-background.svg` (the background of Authentik's pages until 2026-11-19; unused since).

`apps/`: the community apps' home-screen icons (`chat-*`, `calendar-*`: 512 and 192 rounded,
maskable 512 full bleed, 180 apple-touch-icon), rendered by `make-app-icons.py` (Pillow) from the
mark in `web-app-manifest-512x512.png`: the app's symbol in lime with the mark cut out, on the dark
green. Rerun it after changing the mark or the colours.

`osv-bar.js`: the Open Source Village app bar, a 40 px strip fixed on top of every community app
with "Apps" (back to the account page, `pass.<domain>/account`) and a link per app, the current
one highlighted. Each app's image build copies it next to its `index.html` and loads it in
`<head>` with `data-home` (the account page) and `data-apps` (`Name|URL|Description;...`, the same
list as the onboarding service's `BELFRY_COMMUNITY_APPS`; Compose keeps it once as
`x-community-apps`). The script sets
`--osv-bar-h` on `<html>` (class `osv-bar`); the app moves its own top parts down by that much and
can set `--osv-bar-z`. A new app needs the copy, the tag and that one offset rule.

## Where each surface reads it

| Surface | How |
| --- | --- |
| Onboarding pages (login, account, the apps' consent screen, claim, door, pass sign-in, errors) | `claim.page()` links `/brand/fonts.css` and `/brand/tokens.css`, header logo, footer; the steward pages likewise (`steward.page()`) |
| Pass in the wallet | `credentials.type_metadata` (`rendering.simple`, logo URL) and `infra/issuer/config/credential-issuer-metadata.conf` (issuer display, colours, logo); values repeated |
| Community chat and calendar | `infra/apps/chat/` (`osv.css`, `make-env.sh` palette, logo and icons from `apps/`) and `infra/apps/calendar/` patches (theme tokens, values repeated); both load `osv-bar.js` |
| Odoo (invite page, payment page, portal, error pages, activation and sign-in mails) | `views/templates.xml` `brand_frontend_layout` links the two CSS files from `<belfry.public_url>/brand` and overrides Bootstrap (values repeated); `data/mail_template_data.xml` inline; `views/error_pages.xml` (Odoo's 4xx pages as a card, no header); company logo set by bootstrap |
| Proxy error pages (every host: admin pages from outside the admin network, closed paths, rate limits, a service down) | `infra/proxy/errors/error.html`, a Caddy template: colours inline (values repeated), the white logo SVG inline, Geist from `pass.<domain>/brand` where the host's CSP allows it |

Changing a colour: edit `tokens.css`, then the copies named above (grep the old value).
