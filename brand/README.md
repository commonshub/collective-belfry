# Open Source Village brand

Every page a participant sees says **Open Source Village, run by Belfry** and uses the look of
[opensourcevillage.org](https://opensourcevillage.org): dark theme only, green greys, a lime
accent, Geist. Decided 2026-10-23 (steward). Belfry stays the name of the operator (footers,
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
`flow-background.svg` (authentik page background).

## Where each surface reads it

| Surface | How |
| --- | --- |
| Onboarding pages (claim, door, pass sign-in, errors) | `claim.page()` links `/brand/fonts.css` and `/brand/tokens.css`, header logo, footer |
| Pass in the wallet | `credentials.type_metadata` (`rendering.simple`, logo URL) and `infra/issuer/config/credential-issuer-metadata.conf` (issuer display, colours, logo); values repeated |
| Authentik (login, magic link, consent, user settings) | blueprint `infra/authentik/blueprints/belfry-brand.yaml`: title, logo, favicon, background from `BELFRY_BRAND_URL`, theme dark, custom CSS mapping PatternFly variables (values repeated); mail `templates/belfry/magic_link.html` inline; the `cid:logo` PNG is mounted over authentik's |
| Keycast (apps, "Sign in with Open Source Village" popup) | patch `infra/keycast/build/patches/0004-*`: `web/src/app.css` theme (values repeated), `web/static/` logos, favicons, fonts, `brand.ts`, `api/src/brand.rs`; `OIDC_PROVIDER_NAME` and the tenant name (`policy.sql`) |
| Odoo (invite page, payment page, portal, activation mail) | `views/templates.xml` `brand_frontend_layout` links the two CSS files from `<belfry.public_url>/brand` and overrides Bootstrap (values repeated); `data/mail_template_data.xml` inline; company logo set by bootstrap |

Changing a colour: edit `tokens.css`, then the copies named above (grep the old value).
