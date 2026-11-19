# Collective Belfry

Belfry runs access and identity for **Open Source Village**, a week-long gathering. It sells and
checks passes, gives every participant a login and a Nostr identity, and hosts the community apps
participants use during the event: chat, calendar, polls, a directory, a marketplace, photos and a
wallet for village tokens.

Participant-facing pages say "Open Source Village, run by Belfry". Belfry is the operator.

## What a participant does

1. **Gets invited.** A steward sends a single-use invite link.
2. **Buys a pass** (day, week or full access) on the invite page and pays by card. Odoo issues
   the invoice; payment goes through Stripe.
3. **Is set up automatically** once the invoice is paid: a login without a password, a
   Nostr key held by Belfry's signer, a name like `osv-0042@<domain>`, access to the community
   relay, and village tokens in the amount of 50 per pass day. An e-mail brings a one-time
   sign-in link and a link to add the pass to an EUDI wallet.
4. **Signs in** on `pass.<domain>/login` with an e-mail link or code, or by presenting the pass
   from the wallet. The community apps sign in silently once the participant is signed in; the
   account page (`pass.<domain>/account`) lists them and the apps signed in.
5. **Shows the pass at the door**, either from the EUDI wallet or as a renewing QR code on the
   phone (`pass.<domain>/me`).

Refunds, chargebacks and expiry close the pass: the login, the signer, the relay and the pass
certificate stop working. Participants can take their Nostr key into their own custody at any
time.

## The parts

| Part | What it does | Software |
| --- | --- | --- |
| Registry and billing | who holds which pass, invoices, payment links | Odoo 19 Community with the module `odoo/addons/belfry_pass`, Stripe |
| Onboarding service | turns a paid invoice into accounts; the login (e-mail link and code, the pass), the account page and the apps' single sign-on; claim, door and pass pages; steward pages; NIP-05 names; status list | Python, `services/onboarding` |
| Signer | holds participants' Nostr keys, signs for the apps (HTTPS through the onboarding service, or NIP-46) | Rust, `services/signer` |
| Community relay | Nostr relay that only participants with an active pass can read and write | rnostr, `infra/relay` |
| Files | photo and file uploads for participants | Blossom, `infra/blossom` |
| Pass certificates | the pass as an SD-JWT VC in EUDI wallets, door checks | walt.id Issuer2 and Verifier2, `infra/issuer` |
| Village tokens | tokens without monetary value, passed between participants | Cashu mint (cdk-mintd), `infra/mint` |
| Community apps | chat, calendar, wallet (patched upstream apps); polls, directory, marketplace, photos (Belfry's own) | `infra/apps/*`, `services/vote`, `services/village` |
| Proxy | TLS, one hostname per service, rate limits, admin pages only over a private network | Caddy, `infra/proxy` |

Everything runs as one Docker Compose project on one machine.

## Repository layout

| Path | What |
| --- | --- |
| `infra/` | Compose stack, configuration, patched upstream builds, scripts (`infra/README.md`) |
| `services/onboarding/` | the onboarding service |
| `services/signer/` | the signer |
| `services/vote/` | anonymous polls (Semaphore) |
| `services/village/` | directory, marketplace and photos |
| `odoo/addons/belfry_pass/` | Odoo module: passes, invites, pricing, webhooks |
| `brand/` | the Open Source Village look: colours, fonts, logos, app icons, app bar |
| `docs/` | how things work, the plan, background research (`docs/README.md`) |

## Run it locally

Needs Docker with Compose v2, `make`, `curl`, `jq`, `python3`, `openssl` and about 6 GB of RAM.

```
make up            # generate secrets, start the core services (the first run builds the
                   # signer, a few minutes)
make bootstrap     # create the Odoo database, users and tokens
make up-app        # build and start the onboarding service and the apps
make smoke         # end-to-end test: invite, payment, accounts, apps, pass, door
```

Details, ports and logins: `infra/README.md`. Running it on a server: `infra/DEPLOY.md`.

## Documentation

`docs/README.md` lists every document. The main entry points:

- `infra/README.md`: the stack, its services and the scripts.
- `infra/DEPLOY.md`: a public deployment, the admin network, the custody seed, backups.
- `docs/pass-sign-in.md`: signing in with the pass from an EUDI wallet.
- `docs/identity-membership-plan.md`: the plan behind the system.
