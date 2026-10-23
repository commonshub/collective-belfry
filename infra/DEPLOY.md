# Deploying Belfry on one VM

The plan's hosting decision: one VM, Docker Compose, every service. This is the production
layout; the local stack (`infra/README.md`) is the same Compose project without the proxy.

## What you need

- A VM with Docker and Compose v2, about 8 GB RAM, ports 80 and 443 open.
- A domain and DNS records to the VM for `odoo.`, `login.`, `apps.`, `relay.`, `bunker.`, `issuer.`, `verifier.`, `pass.<domain>` and the apex `<domain>` (NIP-05 names are `<member>@<domain>`).
- An SMTP relay for the activation mails (`SMTP_*`), and Stripe keys for Odoo's Stripe provider.

## Steps

```bash
git clone <this repo> belfry && cd belfry
infra/scripts/gen-secrets.sh                      # .env, Odoo config, local issuer PKI
infra/scripts/prod-env.sh belfry.example ops@belfry.example   # public URLs, SANs, domain
# the issuer and verifier certificates must carry the public hostnames: regenerate them
rm infra/issuer/keys/issuer.* infra/issuer/keys/verifier.* infra/issuer/keys/ca.*   # only before the first real issuance!
infra/scripts/gen-secrets.sh
# SMTP relay and sender in infra/.env (SMTP_HOST, SMTP_PORT, SMTP_USER, SMTP_PASSWORD, SMTP_ENCRYPTION, MAIL_FROM)
docker compose -f infra/docker-compose.yml -f infra/docker-compose.prod.yml build keycast     # ~15 min
docker compose -f infra/docker-compose.yml -f infra/docker-compose.prod.yml up -d
ODOO_URL=https://odoo.belfry.example AK_URL=https://login.belfry.example KC_URL=https://apps.belfry.example infra/scripts/bootstrap.sh
docker compose -f infra/docker-compose.yml -f infra/docker-compose.prod.yml --profile app up -d --build
```

`bootstrap.sh` talks to the services through their public hostnames here (the proxy is the only
published port); it needs `ODOO_URL`, `AK_URL`, `KC_URL` set as above. Odoo's `web.base.url` is set
from `ODOO_PUBLIC_URL` and frozen.

Then in Odoo: Invoicing › Configuration › Payment Providers › Stripe: keys and webhook (Odoo
registers its own webhook); Belfry's dispute endpoint is `https://pass.<domain>/hooks/stripe`
(`STRIPE_WEBHOOK_SECRET`). Stewards get the `Steward` group; the first steward is created by hand.

## What the proxy does (proxy/Caddyfile)

| Host | Upstream | Blocked |
| --- | --- | --- |
| `odoo.` | Odoo | `/web/database/*` (master password protects it anyway) |
| `login.` | Authentik | |
| `apps.` | Keycast web app and API | `/api/admin/*` (onboarding service only) |
| apex | NIP-05 `/.well-known/nostr.json` → Keycast (with the tenant host); everything else redirects to `odoo.` | |
| `relay.` | community relay (rnostr, NIP-42) | |
| `bunker.` | Keycast's NIP-46 relay | |
| `issuer.` | Issuer2 wallet-facing API | `/issuer2/*` (management API, no auth upstream) |
| `verifier.` | Verifier2 `request` and `response` of a session only | everything else (session management, no auth upstream) |
| `pass.` | onboarding service: claim pages, door, bridge, status list, vct, Stripe webhook | `/admin/*`, `/hooks/odoo/*`, API docs |

TLS: Let's Encrypt through Caddy (`ACME_EMAIL`). HSTS and the usual headers are set. Odoo's
automation webhooks and the bridge's token/userinfo calls from Authentik stay inside the Compose
network (`http://onboarding:8100`, `http://verifier:7004`), which is why the public paths can
be denied.

## Checks after the first start

- `https://issuer.<domain>/.well-known/openid-credential-issuer/openid4vci` lists `village_pass` with `credential_issuer` on the public host.
- `https://pass.<domain>/status/1` answers `application/statuslist+jwt`; `https://pass.<domain>/vct/village-pass/v1` the type metadata.
- `https://login.<domain>/application/o/keycast/.well-known/openid-configuration` (Keycast's issuer).
- `https://<domain>/.well-known/nostr.json?name=<member>` after the first activation.
- A test purchase with Stripe test mode: the activation mail arrives with both links, the claim page issues to a wallet, the door page accepts the pass, the pass signs in to Authentik.

## Backups and secrets

Back up the Postgres volume (Odoo, Authentik, Keycast databases), `infra/.env`, `infra/keycast/master.key` (every participant key is unreadable without it) and `infra/issuer/keys/` (every pass certificate was signed with it; losing it means re-issuing every pass). Rotating the issuer key: keep the old leaf in the accepted list of the door and bridge until the last certificate signed with it has expired (`infra/issuer/README.md`).

## Not covered yet

Real-wallet testing (the EUDI reference wallet wants signed issuer metadata and a trusted issuer certificate), publishing Belfry's CA for wallets, encrypted verifier responses, the issuer key in a KMS, Blossom, and the community apps themselves.
