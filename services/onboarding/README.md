# Belfry onboarding service

The glue between Odoo (registry and billing), Authentik (login), Keycast (the Nostr signer), the
community relay, the token mint and the walt.id issuer and verifier. It turns a paid invoice into
a working pass, closes passes on refund, chargeback or expiry, and serves the participant-facing
pages around the pass: the claim page (pass into the wallet), the tokens page, the pass on the
phone, the door check, the steward pages and the bridge that lets participants sign in with
their pass.

It keeps no state of its own: everything lives in Odoo (`belfry.pass`, `res.partner`,
`belfry.stripe.event`). Python 3.12, FastAPI.

## Layout

| Path | What |
| --- | --- |
| `belfry_onboarding/settings.py` | configuration from the environment |
| `belfry_onboarding/core.py` | activation, revocation, refunds, reactivation, `reconcile`, `expire`, sign-in links, tokens, key changes, `restore-keys` |
| `belfry_onboarding/app.py` | HTTP routes, the Stripe webhook |
| `belfry_onboarding/clients/` | Odoo (JSON-RPC), Authentik and Keycast (REST), the mint (Cashu), Issuer2 and Verifier2 |
| `belfry_onboarding/claim.py` | claim page and offers, tokens page, status list, door, the landing page, shared HTML |
| `belfry_onboarding/credentials.py` | the pass claims, Token Status List, type metadata, reading a verified presentation |
| `belfry_onboarding/bridge.py` | sign-in with the pass: an OIDC provider over Verifier2 (`../../docs/pass-sign-in.md`) |
| `belfry_onboarding/mypass.py` | the pass on the phone (`/me`) and its door check (`/check`) |
| `belfry_onboarding/steward.py` | the steward pages (`/steward`): invites, passes, reports |
| `belfry_onboarding/reports.py` | reports from the village apps and the list of hidden content |
| `belfry_onboarding/pages.py`, `content/*.md` | the text pages (privacy notice, terms, refund policy, community rules, help): edit the Markdown in `content/` |
| `belfry_onboarding/blossom.py` | the upload check for the file server |
| `belfry_onboarding/relay.py`, `relay_whitelist.py` | publishing to the community relay; its whitelist from the active passes |
| `belfry_onboarding/stripe_sig.py`, `nostr.py` | Stripe signature check; npub encoding |
| `belfry_onboarding/testwallet.py`, `nip46check.py` | a scripted wallet and a NIP-46 client, used by the smoke test |
| `tests/` | unit tests with in-memory fakes of Odoo, Authentik, Keycast and the mint |

## Run

```
python3.12 -m venv .venv && . .venv/bin/activate
pip install -e '.[dev]'
pytest                                   # unit tests, no services needed
ruff check . && ruff format --check .
uvicorn belfry_onboarding.app:app --port 8100 --http h11
```

In the stack the service runs as the Compose service `onboarding` (`make up-app`). Its command
line, for cron jobs and operators (`docker compose exec onboarding python -m belfry_onboarding …`):

| Command | What |
| --- | --- |
| `reconcile` | activate paid invoices whose webhook was missed, revoke refunded passes, mint missing tokens, sync the participants group (every few minutes) |
| `expire` | expire passes past their end date (nightly) |
| `sync-relay` | rewrite the relay whitelist from the active passes (also at start and after every change) |
| `sync-participants` | make the Authentik group `belfry-participants` match the active passes |
| `publish-starter-events` | publish missing profiles, follow lists and relay lists of active passes; never overwrites |
| `login-link <pass id>` | a new one-time sign-in link for that pass; Odoo mails it |
| `add-steward <e-mail> [name]` | create or find the Authentik user and add it to `belfry-stewards` |
| `restore-keys` | recreate every participant's signer account after losing the signer's data (custody seed needed) |

`--http h11` matters: walt.id's Java client sends `Upgrade: h2c` over plain HTTP, which the
default HTTP parser mishandles.

## Configuration

Every variable is listed with a comment in `infra/.env.example`; Compose passes them in.

| Area | Variables |
| --- | --- |
| Odoo | `ODOO_URL`, `ODOO_DB`, `ODOO_USER`, `ODOO_API_KEY` |
| Authentik | `AUTHENTIK_URL`, `AUTHENTIK_PUBLIC_URL` (origin of the sign-in links), `AUTHENTIK_TOKEN`, `AUTHENTIK_SOURCE_SLUG` (`pass-bridge`), `AUTHENTIK_USER_PATH`, `AUTHENTIK_PARTICIPANTS_GROUP` (`belfry-participants`), `LOGIN_LINK_DAYS` (7) |
| Keycast | `KEYCAST_URL`, `KEYCAST_ADMIN_TOKEN`, `KEYCAST_SERVICE_TOKEN`, `KEYCAST_TENANT_DOMAIN` (sent as `Host`: Keycast picks the tenant from it) |
| Relay | `RELAY_URL` (publishing; empty: no profiles are published), `COMMUNITY_RELAY_PUBLIC_URL` (written into relay lists), `NIP05_DOMAIN`, `RELAY_CONFIG_PATH` and `RELAY_CONFIG_TEMPLATE` (whitelist sync; empty: off), `RELAY_EXTRA_PUBKEYS` |
| Tokens | `MINT_URL`, `MINT_PUBLIC_URL`, `MINT_UNIT` (`osv`), `TOKENS_PER_DAY` (50), `WALLET_PUBLIC_URL` |
| Pass certificate | `ISSUER_URL`, `ISSUER_PUBLIC_URL`, `ISSUER_PROFILE`, `OFFER_EXPIRY_SECONDS`, `VERIFIER_URL`, `PUBLIC_URL`, `STATUS_LIST_URL`, `STATUS_LIST_TTL`, `ISSUER_JWK_PATH`, `ISSUER_X5C_PATH`; without issuer and verifier the claim, door and bridge routes answer 503 |
| Pass sign-in | `BRIDGE_CLIENT_ID`, `BRIDGE_CLIENT_SECRET`, `BRIDGE_JWK_PATH` |
| Steward pages | `STEWARD_OIDC_CLIENT_ID`, `STEWARD_OIDC_CLIENT_SECRET`, `STEWARD_SESSION_SECRET`, `STEWARD_SESSION_HOURS` (12), `STEWARD_GROUP`; without the secrets `/steward` answers 503 |
| Pass on the phone | `PASS_PAGE_OIDC_CLIENT_ID`, `PASS_PAGE_OIDC_CLIENT_SECRET`, `PASS_PAGE_SECRET`, `PASS_PAGE_SESSION_DAYS` (14); without the secrets `/me` and `/check` answer 503 |
| Links on pages | `APPS_PUBLIC_URL`, `CHAT_PUBLIC_URL`, `CALENDAR_PUBLIC_URL`, `BRAND_DIR` |
| Shared secrets | `HOOK_TOKEN` (Odoo's webhooks and the admin routes), `PASS_INFO_TOKEN` (Keycast and the apps), `STRIPE_WEBHOOK_SECRET` |
| Event | `EVENT_START`, `EVENT_END`, `RECONCILE_DAYS` (30), `ALLOW_MANUAL_PAYMENTS` (off), `BLOSSOM_HOSTS`, `LOG_LEVEL` |

`ALLOW_MANUAL_PAYMENTS` accepts a paid invoice that has no card transaction at all. Keep it off in
production: Odoo's Stripe provider always records a transaction, and a payment a steward marks on
the steward pages is accepted anyway (it carries the steward's name).

## Endpoints

| Method, path | Auth | What |
| --- | --- | --- |
| `GET /` | none | the start page: what the village is, how to join (an invite from someone already in), links for participants; with `SITE_URL` it is served there (the apex) and other hosts forward |
| `GET /feedback`, `POST /feedback` | none (the `/me` session names the participant) | feedback with an optional screenshot, stored in Odoo (`belfry.feedback`) and mailed to the stewards; `?from=` the page it is about |
| `POST /me/invites`, `POST /me/invites/<id>/revoke` | `/me` session | invites for friends (`INVITES_PER_PARTICIPANT`, default 3, open or used), listed on `/me` |
| `GET /health` | none | `{"status":"ok"}` |
| `POST /hooks/odoo/invoice-paid?token=` | `HOOK_TOKEN` | Odoo's webhook: activates the pass of the invoice (`_id`) |
| `POST /hooks/odoo/refund?token=` | `HOOK_TOKEN` | Odoo's webhook: revokes the pass of a refunded transaction |
| `POST /hooks/odoo/chargeback?token=` | `HOOK_TOKEN` | Odoo's webhook for a provider's chargeback (Mollie): open revokes the pass, reversed gives back a pass revoked for it; the state is read from Odoo |
| `GET /avatar/<pubkey>.png`, `.svg` | none | the default avatar drawn from the key (`avatar.py`), named as `picture` in the starter profile; cached for a year |
| `POST /hooks/stripe` | `Stripe-Signature` | disputes: `charge.dispute.created` revokes, `charge.dispute.closed` won reactivates; every event id is recorded once, replays answer `duplicate` |
| `POST /hooks/keycast/key-changed?token=` | `HOOK_TOKEN` | Keycast reports a participant who moved to their own key: the pass and the relay whitelist follow |
| `POST /admin/reconcile?token=`, `POST /admin/sync-relay?token=` | `HOOK_TOKEN` | as the commands above |
| `POST /admin/passes/<id>/revoke?token=`, `POST /admin/passes/<id>/login-link?token=` | `HOOK_TOKEN` | revoke a pass; mint a new sign-in link (Odoo's buttons) |
| `GET /admin/partners/<id>/pass` | `PASS_INFO_TOKEN` | the partner's pass for Keycast's "Your pass" card |
| `GET /admin/active-passes` | `PASS_INFO_TOKEN` | pass id and public key of every active pass (polls, village apps) |
| `POST /admin/reports` | `PASS_INFO_TOKEN` | a participant's report of content, forwarded by the village server after its NIP-98 check (`reports.py`): checked (the reporter holds an active pass, known app, kind and reason, a valid reference), stored as `belfry.report` in Odoo and mailed to every member of the stewards group |
| `GET /admin/moderation/hidden` | `PASS_INFO_TOKEN` | what stewards hid after a report: event ids, offer coordinates and the pubkeys of directory entries; the village apps do not show them |
| `GET /claim/<token>`, `GET /claim/<token>/offer` | claim token from the activation mail | the claim page: a fresh credential offer as QR code and link (JSON for wallets and tests); 410 when the pass is not active |
| `GET /tokens/<token>` | claim token | the village tokens of the pass: into the wallet, or as text for any Cashu wallet |
| `GET /me`, `/me/qr`, `/me/login`, `/me/callback`, `POST /me/logout` | Authentik sign-in (application `belfry-pass`) | the pass on the phone: tier, dates and a QR code renewed every 30 s |
| `GET /check/<token>` | the token (signed, valid 90 s) | door check for that QR code with any camera: valid or not, tier, dates, never a name; reads the pass state live and says when a code was already scanned |
| `GET /door`, `POST /door/sessions`, `GET /door/sessions/<id>` | none (keep the page at the door) | door check of a wallet presentation: asks for the tier only |
| `/bridge/*` | OIDC client secret and PKCE | sign-in with the pass for Authentik (`../../docs/pass-sign-in.md`) |
| `GET /status/1` | none | the Token Status List (`application/statuslist+jwt`): one bit per pass, 1 = revoked or expired, built from Odoo on every request |
| `GET /vct/village-pass/v1` (also under `/.well-known/vct/…`) | none | the pass's type metadata (name, logo, colours for wallets) |
| `GET /issuer-metadata`, `/issuer-shim/*` | none | compatibility layer in front of Issuer2 for wallets on an older OpenID4VCI draft, and the gate that only lets authorization-code flows through with a claim page's `issuer_state` |
| `GET /blossom/auth` | `Authorization: Nostr <event>` | forward-auth for the file server: 200 when the upload signature is valid and its key holds an active pass |
| `/steward`, `/steward/*` | Authentik sign-in (application `belfry-steward`, group `belfry-stewards`), CSRF token per form | steward pages: invites (create, copy, revoke), passes (mark paid, new sign-in link, revoke) |
| `GET /privacy`, `/terms`, `/refunds`, `/rules`, `/help` | none | the text pages, rendered from `content/*.md`; `[TO FILL: …]` is highlighted, `/help` lists the apps from `COMMUNITY_APPS` |
| `GET /brand/*` | none | the shared brand files (`brand/`) |

## How the operations work

**Activation** (`activate_pass`, from the invoice webhook or `reconcile`) runs only for a paid,
posted invoice with a completed card transaction (or a steward's manual payment), and only for a
`pending` pass. It is idempotent: a repeated webhook finds the existing Authentik user (by member
number attribute, then e-mail) and the existing signer account (by `odoo-partner-<id>`), so
nothing is created twice. In order:

1. Member number (`OSV-0042`) and pass dates: from the payment date or the event start, whichever
   is later, for the product's duration; full access lasts until the event's end.
2. Authentik user (username = member number in lower case, no password), added to the group
   `belfry-participants`, which is what lets it use the signer and the pass page.
3. Signer account through Keycast's preload API, with the key derived from the custody seed and
   the partner id; the public key becomes the pass's npub.
4. Relay whitelist with the new key, then the participant's profile (name and NIP-05 name, nothing
   else), follow list and relay lists, signed through the signer and published.
5. Village tokens (50 per pass day) minted and stored on the pass.
6. A one-time Authentik sign-in link (valid `LOGIN_LINK_DAYS`), the claim token and the status
   list index, written on the pass together with `state = active` in one write. Odoo then mails
   the activation mail.

If any step fails, the pass stays `pending` and the next webhook or `reconcile` completes it.
Profiles and the token mint are best effort; the rest is strict.

**Revocation and expiry** set the pass state, remove the key from the relay whitelist, suspend the
signer account unless the partner holds another active pass, and remove the Authentik user from
`belfry-participants` unless it holds another active pass (checked per Authentik user, because two
partners with the same e-mail share one). The status list bit flips on the next request, so wallet
passes fail at the door after at most `STATUS_LIST_TTL` seconds. Reactivation (a won dispute)
reverses all of it.

**Reconcile** activates paid invoices of the last `RECONCILE_DAYS` whose pass is still pending,
revokes passes whose card transaction was refunded, mints tokens a pass is missing, and syncs the
participants group.

**Pass certificates.** The claim page asks Issuer2 for a fresh pre-authorized offer on every load
(nothing is stored). The credential's visible claim is the tier; display name, member number and
npub can be disclosed selectively; validity comes from the pass dates; the status claim points at
`/status/1`. The door and the bridge accept only credentials signed by Belfry's own issuer
certificate (`infra/issuer/README.md`).

**Key changes.** When a participant moves to their own key (Keycast's self-custody page), Keycast
calls `/hooks/keycast/key-changed`; the pass gets the new npub and the whitelist follows. Polls and
the village apps key on the pass, so nothing else changes.

**Disputes** are matched by the Stripe PaymentIntent stored on the pass.

## Known limits

- Bridge state (sign-ins in progress) is in memory: run one instance.
- The activation tries to pre-link the pass sign-in source to the Authentik user; Authentik's API
  refuses that, so the first sign-in with the pass links it by username instead.
- Whether a signer suspension stops an app authorization that was already cached has not been
  verified; revocation also removes the relay access and the Authentik group, which close the apps
  in any case.
