# Deploying Belfry on one VM

The plan's hosting decision: one VM, Docker Compose, every service. This is the production
layout; the local stack (`infra/README.md`) is the same Compose project without the proxy.

## What you need

- A VM with Docker and Compose v2, about 8 GB RAM, ports 80 and 443 open.
- A domain and DNS records to the VM for `shop.` (Odoo), `login.`, `apps.`, `relay.`, `bunker.`, `issuer.`, `verifier.`, `pass.<domain>` and the apex `<domain>` (NIP-05 names are `<member>@<domain>`).
- An SMTP relay for the activation mails (`SMTP_*`), and Stripe keys for Odoo's Stripe provider.

## Hetzner Cloud (the test deployment, belfry.tofauti.net)

`infra/hetzner/provision.sh` creates the VM with the `hcloud` CLI (active context = the project's
API token): SSH key, firewall (22, 80, 443/tcp+udp) and a cx33 (4 vCPU, 8 GB) with
`cloud-init.yaml` (Docker, user `belfry`, no password or root login). `DRY_RUN=1` prints the
commands. Cloud-init needs about two minutes after the server is created before `belfry` can log
in. The cheap CX types are often sold out per location (`hcloud server-type describe cx33`
shows where they are available); ARM types do not work, `rnostr/rnostr` is amd64 only.

DNS for a subdomain deployment is four records: A and AAAA for `<domain>` and `*.<domain>`. For
a zone at OVH the `ovhcloud` CLI creates them with a token limited to the zone (rules GET, POST,
PUT and DELETE on `/domain/zone/<zone>/*`):

```bash
ovhcloud domain-zone record create tofauti.net --sub-domain '*.belfry' --field-type A --target <ipv4> --ttl 300
ovhcloud domain-zone refresh tofauti.net
```

Hetzner blocks outgoing ports 25 and 465; use SMTP submission on 587 (STARTTLS).

## Steps

```bash
git clone <this repo> belfry && cd belfry
infra/scripts/gen-secrets.sh                      # .env, Odoo config, local issuer PKI
infra/scripts/prod-env.sh belfry.example ops@belfry.example   # public URLs, SANs, domain
# the issuer and verifier certificates must carry the public hostnames: regenerate them
rm infra/issuer/keys/issuer.* infra/issuer/keys/verifier.* infra/issuer/keys/ca.*   # only before the first real issuance!
infra/scripts/gen-secrets.sh
# SMTP relay and sender in infra/.env (SMTP_HOST, SMTP_PORT, SMTP_USER, SMTP_ENCRYPTION, MAIL_FROM);
# the password from your machine, never through .env: infra/scripts/put-secret.sh <ssh target> smtp_password
docker compose -f infra/docker-compose.yml -f infra/docker-compose.prod.yml build keycast     # ~15 min
docker compose -f infra/docker-compose.yml -f infra/docker-compose.prod.yml up -d
ODOO_URL=https://shop.belfry.example AK_URL=https://login.belfry.example KC_URL=https://apps.belfry.example infra/scripts/bootstrap.sh
docker compose -f infra/docker-compose.yml -f infra/docker-compose.prod.yml --profile app up -d --build
```

`bootstrap.sh` talks to the services through their public hostnames here (the proxy is the only
published port); it needs `ODOO_URL`, `AK_URL`, `KC_URL` set as above. The paths the proxy denies
are handled: the verifier probe accepts any HTTP answer and Keycast's dev mail inbox (admin
confirmation) is read with `docker compose exec` inside the container. Odoo's `web.base.url` is set
from `ODOO_PUBLIC_URL` and frozen.

Then in Odoo: Invoicing › Configuration › Payment Providers › Stripe: keys and webhook (Odoo
registers its own webhook); Belfry's dispute endpoint is `https://pass.<domain>/hooks/stripe`
(`STRIPE_WEBHOOK_SECRET`). Stewards get the `Steward` group; the first steward is created by hand.

## Participant login and admin login

Participants sign in by e-mail magic link (or the pass); there are no participant passwords.
The magic-link mail goes out through the SMTP settings bootstrap writes into the e-mail stage
(`SMTP_*`, `infra/secrets/smtp_password`), the same relay as Odoo's mails. After changing the
SMTP settings or the password, run bootstrap again. The admin `akadmin` signs in with its
password at `https://login.<domain>/if/flow/default-authentication-flow/?next=/if/admin/`
(the login page itself only offers e-mail and pass).

## Stewards

Stewards work on `https://pass.<domain>/steward` (invites, mark paid, re-send sign-in link,
revoke), signed in through Authentik like participants (magic link or pass); Odoo's UI is for
admins and accounting. Make someone a steward (creates a passwordless Authentik user if the
e-mail has none, adds it to `belfry-stewards`):

```bash
docker compose -f infra/docker-compose.yml -f infra/docker-compose.prod.yml --profile app exec -T onboarding python -m belfry_onboarding add-steward <e-mail> "<name>"
```

## Keycast and Authentik behind the proxy

The overlay unsets `OIDC_BACKCHANNEL_URL` for Keycast: it fetches Authentik's discovery document, tokens and keys through the public `https://login.<domain>` instead of the Compose network, because Authentik derives the issuer from the request scheme and would publish `http://login.<domain>/...` for an internal request, which Keycast rejects against `OIDC_ISSUER`. The local stack keeps the backchannel (Authentik is only reachable as `http://localhost:9000` from the host there).

## What the proxy does (proxy/Caddyfile)

| Host | Upstream | Blocked |
| --- | --- | --- |
| `shop.` | Odoo (`ODOO_HOST_PREFIX`, default `shop`; belfry.tofauti.net keeps `odoo.`) | `/web/database/*` (master password protects it anyway) |
| `login.` | Authentik | |
| `apps.` | Keycast web app and API | `/api/admin/*` (onboarding service only) |
| apex | NIP-05 `/.well-known/nostr.json` → Keycast (with the tenant host); everything else redirects to `shop.` | |
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
