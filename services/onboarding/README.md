# Belfry onboarding service

The glue between Odoo (registry and billing), Belfry's signer (`../signer`, the members' keys),
the community relay, the token mint and the walt.id issuer and verifier. It turns a paid invoice
into a working pass, closes passes on refund, chargeback or expiry, and serves the
participant-facing pages around the pass: the sign-in (login page, e-mail link and code, the pass
from a wallet), the account page, the apps' "Sign in with Open Source Village", the claim page
(pass into the wallet), the tokens page, the pass on the phone, the door check and the steward
pages. Since 2026-11-19 it is the sign-in that Authentik and Keycast were.

It keeps no state of its own: everything lives in Odoo (`belfry.pass`, `res.partner`,
`belfry.login.code`, `belfry.stripe.event`) and the keys in the signer. Python 3.12, FastAPI.

## Layout

| Path | What |
| --- | --- |
| `belfry_onboarding/settings.py` | configuration from the environment |
| `belfry_onboarding/core.py` | activation, revocation, refunds, reactivation, `reconcile`, `expire`, sign-in links, tokens, key changes, `restore-keys` |
| `belfry_onboarding/app.py` | HTTP routes, the Stripe webhook |
| `belfry_onboarding/clients/` | Odoo (JSON-RPC), the signer (`signer.py`), the mint (Cashu), Issuer2 and Verifier2 |
| `belfry_onboarding/session.py` | Belfry's sign-in session: a signed cookie on `PUBLIC_URL`'s host, where a sign-in may end |
| `belfry_onboarding/login.py` | the login page (`/login`: e-mail link and code, the pass), logout, and the routes of `passlogin.py` and `oidc.py` |
| `belfry_onboarding/passlogin.py` | sign-in with the pass: a presentation to Verifier2 signs the browser in (`../../docs/pass-sign-in.md`) |
| `belfry_onboarding/oidc.py` | Belfry as the OpenID provider of Issuer2's sign-in (authorization-code offers) |
| `belfry_onboarding/connect.py` | the apps' sign-in with Keycast's OAuth shape (`/api/oauth/*`), bunker URLs and the HTTP RPC (`/api/nostr`) |
| `belfry_onboarding/account.py` | the account page (`/account`): pass, apps, identity, signed-in apps, "Connect a Nostr app", key export, the move to an own key |
| `belfry_onboarding/nip05.py` | NIP-05 names (`/.well-known/nostr.json`) from Odoo |
| `belfry_onboarding/claim.py` | claim page and offers, tokens page, status list, door, the landing page, shared HTML |
| `belfry_onboarding/credentials.py` | the pass claims, Token Status List, type metadata, reading a verified presentation |
| `belfry_onboarding/mypass.py` | the pass on the phone (`/me`) and its door check (`/check`) |
| `belfry_onboarding/steward.py` | the steward pages (`/steward`): invites, passes, reports |
| `belfry_onboarding/reports.py` | reports from the village apps and the list of hidden content |
| `belfry_onboarding/pages.py`, `content/*.md` | the text pages (privacy notice, terms, refund policy, community rules, help): edit the Markdown in `content/` |
| `belfry_onboarding/blossom.py` | the upload check for the file server |
| `belfry_onboarding/holder_invites.py` | invites by e-mail or as a code for a pass holder or their agent (`/api/invites`, NIP-98) |
| `belfry_onboarding/discovery.py` | what an app needs to connect: `/.well-known/belfry.json`, `/api/me`, CORS of `/api/*` |
| `belfry_onboarding/accounting.py`, `clients/odoo_json2.py` | books each paid ticket (invoice, payment, credit note) in the Odoo of the organisation that sells the tickets, over its JSON-2 API (`../../infra/DEPLOY.md`, "Accounting at the ticket seller") |
| `belfry_onboarding/email_code.py` | an app's own key proves an e-mail address with a mailed 6-digit code (`/api/email-code`) |
| `belfry_onboarding/relay.py`, `relay_whitelist.py` | publishing to the community relay; its whitelist from the active passes |
| `belfry_onboarding/stripe_sig.py`, `nostr.py` | Stripe signature check; npub encoding |
| `belfry_onboarding/testwallet.py`, `nip46check.py` | a scripted wallet and a NIP-46 client, used by the smoke test |
| `belfry_onboarding/agent.py`, `agentcheck.py` | an agent acting for a pass holder through their bunker URL: one action per call (`me`, `invite`, `invites`, `status`, `revoke`), and the smoke test's run through every step |
| `tests/` | unit tests with in-memory fakes of Odoo, the signer and the mint |

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
| `reconcile` | activate paid invoices whose webhook was missed, revoke refunded passes, mint missing tokens (every few minutes) |
| `expire` | expire passes past their end date (nightly) |
| `sync-relay` | rewrite the relay whitelist from the active passes (also at start and after every change) |
| `publish-starter-events` | publish missing profiles, follow lists and relay lists of active passes; never overwrites |
| `login-link <pass id>` | a new one-time sign-in link for that pass; Odoo mails it |
| `add-steward <e-mail> [name]` | mark the Odoo contact with that address a steward (created if needed) and give the steward a free pass |
| `steward-passes` | give every steward their free pass (idempotent; run on deploys) |
| `accounting-check` | connect to the ticket seller's Odoo: the configured product, tax and journal ids, and the candidates |
| `accounting-book [pass id]` | book a paid ticket at the ticket seller; without an id, every one not booked yet (also part of `reconcile`) |
| `restore-keys` | recreate every participant's signer account from Odoo after losing the signer's data (custody seed needed) |
| `import-keycast`, `import-stewards` | the move from Keycast and Authentik (`infra/scripts/migrate-from-keycast.sh`): Keycast's sealed keys, the stewards' e-mail addresses, on stdin |

`--http h11` matters: walt.id's Java client sends `Upgrade: h2c` over plain HTTP, which the
default HTTP parser mishandles.

## Configuration

Every variable is listed with a comment in `infra/.env.example`; Compose passes them in.

| Area | Variables |
| --- | --- |
| Odoo | `ODOO_URL`, `ODOO_DB`, `ODOO_USER`, `ODOO_API_KEY` |
| Signer | `SIGNER_URL` (`http://signer:8200`), `SIGNER_TOKEN` |
| Sign-in | `SESSION_SECRET` (32 characters at least; sessions and the apps' tokens), `SESSION_DAYS` (30), `SESSION_FRESH_SECONDS` (300, key export and the move to an own key), `LOGIN_LINK_DAYS` (7, the activation mail's link) |
| Apps | `OAUTH_CLIENTS` (Belfry's own apps, `client_id=redirect_uri`, comma separated: no consent screen, silent sign-in), `DERIVE_SECRETS` (`client:purpose|purpose;…`), `BELFRY_COMMUNITY_APPS` (the account page's apps, `Name|URL|Description;…`) |
| Relay | `RELAY_URL` (publishing; empty: no profiles are published), `COMMUNITY_RELAY_PUBLIC_URL` (written into relay lists), `NIP05_DOMAIN`, `RELAY_CONFIG_PATH` and `RELAY_CONFIG_TEMPLATE` (whitelist sync; empty: off), `RELAY_EXTRA_PUBKEYS` |
| Tokens | `MINT_URL`, `MINT_PUBLIC_URL`, `MINT_UNIT` (`osv`), `TOKENS_PER_DAY` (50), `WALLET_PUBLIC_URL` |
| Pass certificate | `ISSUER_URL`, `ISSUER_PUBLIC_URL`, `ISSUER_PROFILE`, `OFFER_EXPIRY_SECONDS`, `VERIFIER_URL`, `PUBLIC_URL`, `STATUS_LIST_URL`, `STATUS_LIST_TTL`, `ISSUER_JWK_PATH`, `ISSUER_X5C_PATH`; without issuer and verifier the claim, door and bridge routes answer 503 |
| Issuer2's sign-in | `ISSUER_OIDC_CLIENT_ID` (`belfry-issuer`), `ISSUER_OIDC_CLIENT_SECRET`, `BRIDGE_JWK_PATH` (the ID tokens' key) |
| Steward pages | `STEWARD_SESSION_SECRET`, `STEWARD_SESSION_HOURS` (12); without the secrets `/steward` answers 503 |
| Pass on the phone | `PASS_PAGE_SECRET` (the QR codes); without it `/me` and `/check` answer 503 |
| Links on pages | `APPS_PUBLIC_URL`, `CHAT_PUBLIC_URL`, `CALENDAR_PUBLIC_URL`, `BRAND_DIR` |
| Discovery, API | `COMMUNITY_NAME`, `ODOO_PUBLIC_URL`, `GROUPS_RELAY_URL`, `BUNKER_PUBLIC_RELAYS`, `BLOSSOM_PUBLIC_URL` (listed in `/.well-known/belfry.json`), `API_CORS_ORIGINS` (origins, comma separated or `*`, whose pages may call `/api/*`; empty: none) |
| Shared secrets | `HOOK_TOKEN` (Odoo's webhooks and the admin routes), `PASS_INFO_TOKEN` (the apps' servers), `STRIPE_WEBHOOK_SECRET` |
| Event | `EVENT_START`, `EVENT_END`, `RECONCILE_DAYS` (30), `ALLOW_MANUAL_PAYMENTS` (off), `BLOSSOM_HOSTS`, `LOG_LEVEL` |

`ALLOW_MANUAL_PAYMENTS` accepts a paid invoice that has no card transaction at all. Keep it off in
production: Odoo's Stripe provider always records a transaction, and a payment a steward marks on
the steward pages is accepted anyway (it carries the steward's name).

## Endpoints

| Method, path | Auth | What |
| --- | --- | --- |
| `GET /` | none | the start page: what the village is, how to join (an invite from someone already in), links for participants; with `SITE_URL` it is served there (the apex) and other hosts forward |
| `GET /feedback`, `POST /feedback` | none (the session names the participant) | feedback with an optional screenshot, stored in Odoo (`belfry.feedback`) and mailed to the stewards; `?from=` the page it is about |
| `POST /me/invites`, `POST /me/invites/<id>/revoke` | session | invites for friends (`INVITES_PER_PARTICIPANT`, default 3, open or used), listed on `/me` |
| `GET /health` | none | `{"status":"ok"}` |
| `GET /login`, `POST /login/email`, `POST /login/code`, `GET`/`POST /login/link/<secret>` | none | the login page: Odoo mails a one-time link and a 6-digit code (`belfry.login.code`: ten minutes, five tries, five mails per address in 30 minutes) to an address with an active pass or a steward's; the link signs in any browser, the code the one that asked; then `next` |
| `GET /login/pass`, `/login/pass/poll/<sid>`, `/login/pass/done/<sid>` | none (a cookie binds the attempt to the browser) | sign-in with the pass from a wallet (OpenID4VP, Belfry's issuer only, not revoked) |
| `GET /logout`, `/api/auth/oidc/logout` | none | ends the session; `return_to` only to Belfry's own origins |
| `GET /account`, `POST /account/connections`, `/account/connections/revoke`, `/account/export`, `GET /account/self-custody/challenge`, `POST /account/self-custody` | session (export and move: signed in within `SESSION_FRESH_SECONDS`), same origin | the account page |
| `GET /api/oauth/authorize`, `POST /api/oauth/consent` | session (else the login page; `prompt=none` answers `login_required`) | the apps' sign-in (Keycast's shape): PKCE S256; own apps without consent; `access_denied` with `self_custody` for a member with their own key |
| `POST /api/oauth/token`, `POST /api/oauth/revoke` | code and PKCE verifier, or the refresh token (CORS for every origin) | `{access_token, refresh_token, bunker_url, pubkey}`; the revoke ends the app's connection at the signer |
| `POST /api/nostr` | the access token (CORS for every origin) | HTTP RPC: `get_public_key`, `sign_event`, `nip44_*`, `nip04_*`, `derive_secret` (the app's purposes only) |
| `GET /.well-known/nostr.json?name=` | none (CORS) | NIP-05: `<member number>@<domain>` of an active pass; only the name asked for |
| `/oidc/*` | session; client secret and PKCE at the token endpoint | Issuer2's sign-in for authorization-code offers |
| `GET /.well-known/belfry.json` | none (CORS for every origin) | the community's name, relays, file server, mint, sign-in, APIs, apps and skills: an app asks for the domain only |
| `GET /api/me` | NIP-98 | where the signing key stands: `member`, `steward`, NIP-05 name, `email` and its passes with the tokens each brought (also one ordered with this key, not yet paid); a key without a pass gets `member: false` |
| `POST /api/email-code`, `POST /api/email-code/verify` | NIP-98, any key | Odoo mails a 6-digit code (10 minutes, five tries, five codes per address and ten per key in 30 minutes); the right code records the key on the address's contact (`belfry_verified_pubkey`, the contact created if needed), never over a key the contact holds (`409 other_key`) |
| `/api/invites`, `/api/invites/<id>`, `POST /api/invites/<id>/revoke` | NIP-98, key with an active pass | invites by e-mail, or as a code without `email` (nothing mailed), for a pass holder or their agent (`holder_invites.py`); stewards' are unlimited |
| `GET /skill.md`, `/skills/<name>.md` | none | the agent skills in `skills/` with the deployment's domain filled in (`open-source-village`, `belfry-steward`) |
| `POST /hooks/odoo/invoice-paid?token=` | `HOOK_TOKEN` | Odoo's webhook: activates the pass of the invoice (`_id`) |
| `POST /hooks/odoo/refund?token=` | `HOOK_TOKEN` | Odoo's webhook: revokes the pass of a refunded transaction |
| `POST /hooks/odoo/chargeback?token=` | `HOOK_TOKEN` | Odoo's webhook for a provider's chargeback (Mollie): open revokes the pass, reversed gives back a pass revoked for it; the state is read from Odoo |
| `GET /avatar/<pubkey>.png`, `.svg` | none | the default avatar drawn from the key (`avatar.py`), named as `picture` in the starter profile; cached for a year |
| `POST /hooks/stripe` | `Stripe-Signature` | disputes: `charge.dispute.created` revokes, `charge.dispute.closed` won reactivates; every event id is recorded once, replays answer `duplicate` |
| `POST /admin/reconcile?token=`, `POST /admin/sync-relay?token=` | `HOOK_TOKEN` | as the commands above |
| `POST /admin/passes/<id>/revoke?token=`, `POST /admin/passes/<id>/login-link?token=` | `HOOK_TOKEN` | revoke a pass; mint a new sign-in link (Odoo's buttons) |
| `GET /admin/partners/<id>/pass` | `PASS_INFO_TOKEN` | the partner's pass as the account page shows it |
| `GET /admin/active-passes` | `PASS_INFO_TOKEN` | pass id and public key of every active pass (polls, village apps) |
| `POST /admin/reports` | `PASS_INFO_TOKEN` | a participant's report of content, forwarded by the village server after its NIP-98 check (`reports.py`): checked (the reporter holds an active pass, known app, kind and reason, a valid reference), stored as `belfry.report` in Odoo and mailed to every steward |
| `GET /admin/moderation/hidden` | `PASS_INFO_TOKEN` | what stewards hid after a report: event ids, offer coordinates and the pubkeys of directory entries; the village apps do not show them |
| `GET /claim/<token>`, `GET /claim/<token>/offer` | claim token from the activation mail | the claim page: a fresh credential offer as QR code and link (JSON for wallets and tests); 410 when the pass is not active |
| `GET /tokens/<token>` | claim token | the village tokens of the pass: into the wallet, or as text for any Cashu wallet |
| `GET /me`, `/me/qr`, `/me/login`, `POST /me/logout` | session | the pass on the phone: tier, dates and a QR code renewed every 30 s |
| `GET /check/<token>` | the token (signed, valid 90 s) | door check for that QR code with any camera: valid or not, tier, dates, never a name; reads the pass state live and says when a code was already scanned |
| `GET /door`, `POST /door/sessions`, `GET /door/sessions/<id>` | none (keep the page at the door) | door check of a wallet presentation: asks for the tier only |
| `GET /status/1` | none | the Token Status List (`application/statuslist+jwt`): one bit per pass, 1 = revoked or expired, built from Odoo on every request |
| `GET /vct/village-pass/v1` (also under `/.well-known/vct/…`) | none | the pass's type metadata (name, logo, colours for wallets) |
| `GET /issuer-metadata`, `/issuer-shim/*` | none | compatibility layer in front of Issuer2 for wallets on an older OpenID4VCI draft, and the gate that only lets authorization-code flows through with a claim page's `issuer_state` |
| `GET /blossom/auth` | `Authorization: Nostr <event>` | forward-auth for the file server: 200 when the upload signature is valid and its key holds an active pass |
| `/steward`, `/steward/*` | session of a steward (`res.partner.belfry_steward`), CSRF token per form | steward pages: invites (create, copy, revoke), passes (mark paid, new sign-in link, revoke) |
| `GET /privacy`, `/terms`, `/refunds`, `/rules`, `/help` | none | the text pages, rendered from `content/*.md`; `[TO FILL: …]` is highlighted, `/help` lists the apps from `COMMUNITY_APPS` |
| `GET /brand/*` | none | the shared brand files (`brand/`) |

## How the operations work

**Activation** (`activate_pass`, from the invoice webhook or `reconcile`) runs only for a paid,
posted invoice with a completed card transaction (or a steward's manual payment), and only for a
`pending` pass. It is idempotent: a repeated webhook finds the existing signer account (by
`odoo-partner-<id>`), so nothing is created twice. In order:

1. Member number (`OSV-0042`) and pass dates: from the payment date or the event start, whichever
   is later, for the product's duration; full access lasts until the event's end.
2. Signer account, with the key derived from the custody seed and the partner id (a locked seed
   holds the activation: 503, the next webhook or `reconcile` completes it); the public key
   becomes the pass's npub. A participant who joined with their own
   key (`own_pubkey` on the pass, its proof checked again here) gets an account without a key, and
   the partner's custody becomes `self`.
3. Relay whitelist with the new key, then the participant's profile (name and NIP-05 name, nothing
   else), follow list and relay lists, signed through the signer and published. Not for a
   participant who holds their own key: Belfry cannot sign for it, and their own profile counts.
4. Village tokens (50 per pass day) minted and stored on the pass.
5. A one-time sign-in link (valid `LOGIN_LINK_DAYS`), the claim token and the status
   list index, written on the pass together with `state = active` in one write. Odoo then mails
   the activation mail.

If any step fails, the pass stays `pending` and the next webhook or `reconcile` completes it.
Profiles and the token mint are best effort; the rest is strict.

**Revocation and expiry** set the pass state, remove the key from the relay whitelist and suspend
the signer account unless the partner holds another active pass (nothing is signed for them any
more, over the HTTP RPC and NIP-46 alike; their sign-in and the apps' sign-in end, since both ask
Odoo for an active pass). The status list bit flips on the next request, so wallet
passes fail at the door after at most `STATUS_LIST_TTL` seconds. Reactivation (a won dispute)
reverses all of it.

**Reconcile** activates paid invoices of the last `RECONCILE_DAYS` whose pass is still pending,
revokes passes whose card transaction was refunded and mints tokens a pass is missing.

**Pass certificates.** The claim page asks Issuer2 for a fresh pre-authorized offer on every load
(nothing is stored). The credential's visible claim is the tier; display name, member number and
npub can be disclosed selectively; validity comes from the pass dates; the status claim points at
`/status/1`. The door and the pass sign-in accept only credentials signed by Belfry's own issuer
certificate (`infra/issuer/README.md`).

**Key changes.** When a participant moves to their own key (the account page, a challenge signed
with the new key, a fresh sign-in), the signer drops the key it held and ends every connection,
the pass gets the new npub and the whitelist follows. Polls and
the village apps key on the pass, so nothing else changes.

**Disputes** are matched by the Stripe PaymentIntent stored on the pass.

## Known limits

- Sign-ins in progress (the apps' codes, pass sign-ins, Issuer2's codes) are in memory: run one
  instance; a restart only drops sign-ins in progress. Sessions and app tokens are signed cookies
  and tokens, so they survive restarts.
- Logging out ends the session in that browser; other browsers keep theirs until they sign out
  or the pass closes (every app sign-in and signature asks Odoo and the signer again).
