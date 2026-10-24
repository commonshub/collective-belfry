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
# MAIL_FROM must be on a domain whose SPF (ideally DKIM too) covers the relay, e.g. the SMTP
# login's own address: prod-env.sh's default belfry@<domain> needs its own SPF record, or the
# relay accepts the mails and recipients drop them without a bounce;
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
(the login page itself only offers e-mail and pass); that URL answers only over the admin network.
Sign-in links are limited per account: at most five in 30 minutes (a deny stage in the flow).

## Admin network (ZeroTier)

The admin pages answer only over Belfry's admin network, a private ZeroTier network: Odoo's back
office and its login (`/odoo`, `/web`, `/web/login`, the database manager, XML/JSON-RPC),
Authentik's admin interface and akadmin's password login, the steward pages (`/steward`) and
Keycast's admin pages. Everywhere else they answer 403. The proxy checks the connection's address
against `ADMIN_NETS` (the ZeroTier subnet plus Docker's ranges, so bootstrap and the smoke test on
the VM itself keep working). Participants' pages (invites, payment, portal links, sign-in,
apps) are unaffected.

1. In ZeroTier Central (https://my.zerotier.com) create a network, access control **private**,
   with an IPv4 auto-assign pool (e.g. `10.147.17.0/24`). Note the network id.
2. On the VM: `infra/scripts/zerotier.sh install`, then `infra/scripts/zerotier.sh join <network id>`.
3. In ZeroTier Central authorise the VM's node (the id `join` printed), and give it a fixed address.
4. On the VM: `infra/scripts/zerotier.sh status` prints the VM's ZeroTier address and an
   `ADMIN_NETS=...` line: put it into `infra/.env`, then
   `docker compose -f infra/docker-compose.yml -f infra/docker-compose.prod.yml up -d proxy`.
5. Each admin and steward device: install ZeroTier (desktop, Android, iOS), join the network,
   get authorised in ZeroTier Central, and map the hostnames to the VM's ZeroTier address in the
   hosts file, e.g. `10.147.17.10 odoo.<domain> login.<domain> pass.<domain> apps.<domain>`.
   The certificates stay valid (same names). Phones without a hosts file: a ZeroTier DNS entry
   (Central, "DNS") or a local resolver on the network does the same.
6. Check from outside the network: `https://pass.<domain>/steward` answers 403; from inside it
   signs in.

Stewards working at the door from a phone need ZeroTier on that phone. The Hetzner firewall may
allow UDP 9993 inbound for direct ZeroTier paths; without it traffic goes through ZeroTier's
relays (slower, works).

## Stewards

Stewards work on `https://pass.<domain>/steward` (over the admin network, see above) (invites, mark paid, re-send sign-in link,
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
| `shop.` | Odoo (`ODOO_HOST_PREFIX`, default `shop`; belfry.tofauti.net keeps `odoo.`) | back office, login, database manager, RPC: admin network only |
| `login.` | Authentik | admin interface, akadmin's password flow: admin network only |
| `apps.` | Keycast web app and API | `/api/admin/*` (onboarding service only); `/admin` admin network only |
| apex | NIP-05 `/.well-known/nostr.json` → Keycast (with the tenant host); everything else redirects to `shop.` | |
| `relay.` | community relay (rnostr, NIP-42) | |
| `chat.` | Community chat (Coracle, NIP-17), static build; `CHAT_PUBLIC_URL` and `BLOSSOM_PUBLIC_URL` are compiled in, rebuild `chat` after changing them | |
| `bunker.` | Keycast's NIP-46 relay; `BUNKER_PUBLIC_RELAYS=wss://bunker.<domain>` (set by `prod-env.sh`) is what Keycast writes into bunker URLs | |
| `issuer.` | Issuer2 wallet-facing API | `/issuer2/*` (management API, no auth upstream) |
| `verifier.` | Verifier2 `request` and `response` of a session only | everything else (session management, no auth upstream) |
| `pass.` | onboarding service: claim pages, pass on the phone (`/me`, `/check`), door, bridge, status list, vct, Stripe webhook | `/admin/*`, `/hooks/odoo/*`, API docs; `/steward` admin network only |

TLS: Let's Encrypt through Caddy (`ACME_EMAIL`). HSTS and the usual headers are set.

Hardening (2026-10-24):

- **Rate limits** per client address (Caddy with `caddy-ratelimit`, `proxy/Dockerfile`), generous
  because the venue's Wi-Fi puts hundreds of participants behind one address: Authentik's flow
  steps 300/min, claim and token pages 120/min, `/me` sign-in 120/min, `/check` 600/min, bridge
  300/min, door 60/min, issuer 300/min, invite pages 120/min. Beyond: 429 with `Retry-After`.
  Mail flooding is limited per account in Authentik, not by address.
- **Content-Security-Policy** on every browser app: scripts only from the app itself, connections
  only to Belfry's hosts (the chat may reach any relay), no framing. Odoo and Authentik ship
  inline code and get none. Blossom's files are served sandboxed.
- **Containers** run without root (the walt.id services, mint, Blossom, relays, static apps and the
  proxy as user 10001; Keycast as the deploying user, `HOST_UID`, the owner of `master.key` and
  the custody seed), with `no-new-privileges` and, where they start unprivileged, no capabilities.
  `perms-init` hands their volumes to that user once.
- **Networks**: Postgres and Redis sit on the internal `data` network with their clients only;
  the static apps only on `edge` with the proxy. `edge` has IPv6 so that the proxy sees IPv6
  visitors' own addresses (before, Docker's userland proxy showed them all as `172.18.0.1`).
- **Mailpit** does not run in production (profile `dev`). Odoo's
automation webhooks and the bridge's token/userinfo calls from Authentik stay inside the Compose
network (`http://onboarding:8100`, `http://verifier:7004`), which is why the public paths can
be denied.

## Checks after the first start

- `https://issuer.<domain>/.well-known/openid-credential-issuer/openid4vci` lists `village_pass` with `credential_issuer` on the public host.
- `https://pass.<domain>/status/1` answers `application/statuslist+jwt`; `https://pass.<domain>/vct/village-pass/v1` the type metadata.
- `https://login.<domain>/application/o/keycast/.well-known/openid-configuration` (Keycast's issuer).
- `https://<domain>/.well-known/nostr.json?name=<member>` after the first activation.
- A test purchase with Stripe test mode: the activation mail arrives with both links, the claim page issues to a wallet, the door page accepts the pass, the pass signs in to Authentik.

## Custody seed (participant keys, YubiKeys)

Participant Nostr keys derive from one custody seed (Keycast patch 0014, `infra/keycast/README.md`). It is created on a steward's laptop, encrypted to the stewards' YubiKeys (either one decrypts), and on the VM it only ever lives in memory (`/run/belfry`, `CUSTODY_SEED_DIR=/run/belfry` from `prod-env.sh`). Needs `age` and `age-plugin-yubikey` on the laptop.

1. Each YubiKey once: `age-plugin-yubikey` (interactive: creates the PIV key, PIN and touch policy), then `age-plugin-yubikey --identity > yubikey-<name>.txt` (names the slot; the secret stays on the key).
2. Once, with all YubiKeys at hand: `infra/scripts/custody-seed.sh recipients` per key, then `infra/scripts/custody-seed.sh init custody-seed.age <recipient 1> <recipient 2>`. Copy `custody-seed.age` to each steward and into the backups; it is useless without a YubiKey.
3. After every VM reboot (the seed is gone; signing keeps working, new accounts wait with 503 and the activation retries in `reconcile`): `infra/scripts/custody-seed.sh unlock belfry@<host> custody-seed.age yubikey-<name>.txt` (touch the key), `… status belfry@<host>`.
Until the ceremony, mocked tokens: plain age keys stand in for the YubiKeys (`age-keygen -o mock-token-a.txt`, `-b`); their `age1…` public keys are the recipients for `init` and `backup.sh keygen`, the files are the identities for `unlock` and `decrypt`. At the ceremony, keep the seed and re-encrypt it: `custody-seed.sh rewrap custody-seed.age custody-seed.yubikey.age mock-token-a.txt <yubikey recipient 1> <yubikey recipient 2>` and `backup.sh rewrap backup-identity.age backup-identity.yubikey.age mock-token-a.txt <…>`, check `unlock` with each YubiKey, then destroy the mock tokens and the old files. Participant keys and old backups stay valid. A fresh seed (`init`) instead would give everyone activated before a key that is no longer recoverable; only worth it if the mock keys may have leaked.

4. Recovery after losing Keycast's database or master key: unlock the seed, start Keycast with an empty database, run `docker compose exec onboarding python -m belfry_onboarding restore-keys`. Derived keys come back with the same npubs; participants who moved to their own key get a keyless account with their npub; keys created before the custody seed are reported as `mismatch` and cannot come back.

## Backups

`infra/scripts/backup.sh` makes one encrypted file per night and sends it off the VM. It holds every Postgres database (Odoo, Authentik, Keycast; `pg_dumpall`), the volumes that are not derived from Odoo (Odoo filestore, mint, polls, Blossom, community relay, Authentik media; the SQLite and LMDB ones are paused for the few seconds of the copy), the secret files (`infra/.env`, `infra/keycast/master.key`, `infra/issuer/keys/`, the rendered issuer, verifier and Odoo configs, `infra/secrets/` without the custody seed) and the code as deployed (the VM's tree can differ from the pushed history). The VM only holds the public half of the backup key: it can write backups but not read them. The plain custody seed is never in a backup; `custody-seed.age` is, if you copy it to `infra/secrets/` (it is useless without a YubiKey).

1. On a steward's laptop, with the YubiKeys (the recipients from `custody-seed.sh recipients`): `infra/scripts/backup.sh keygen backup-identity.age <recipient 1> <recipient 2>`. Keep `backup-identity.age` with every steward next to `custody-seed.age`. It prints `BACKUP_RECIPIENTS=age1…` for the VM; a second recipient whose key is kept offline (on paper) protects against losing both YubiKeys.
2. An off-VM target, for example a Hetzner Storage Box (BX11) with SSH enabled and **automatic snapshots** on (the VM's key can delete files there, snapshots it cannot): on the VM `ssh-keygen -t ed25519 -N '' -f ~/.ssh/belfry-backup`, add the public key to the box, `ssh-keyscan -p 23 <box host> >> ~/.ssh/known_hosts`, and create the directory (`echo 'mkdir belfry' | sftp -P 23 -i ~/.ssh/belfry-backup <user>@<box host>`).
3. On the VM: `sudo apt install age`, `BACKUP_RECIPIENTS` and `BACKUP_TARGET=sftp://<user>@<box host>:23/belfry` in `infra/.env`, then `infra/scripts/backup.sh run` once and `infra/scripts/backup.sh install` (systemd timer, 03:15 UTC, `BACKUP_AT`). `backup.sh status` exits 1 when the last success is older than 26 hours (for monitoring); `backup.sh list` shows the target. The last two backups also stay on the VM in `~/belfry-backups`.

Restore drill (do it after the first backup and then monthly, on a laptop with Docker and a YubiKey): fetch a file (`sftp -P 23 …`), `infra/scripts/backup.sh decrypt <file> backup-identity.age yubikey-<name>.txt -o restored`, `infra/scripts/backup.sh drill restored`. The drill unpacks the backup's own code into a temporary directory, restores files, volumes and databases into the Compose project `belfry-drill` (only Postgres runs, no ports, the live stack is not touched), compares file hashes, volume file counts and the row count of every table with the backup's manifest, and removes everything again. Busy tables may differ by a few rows; passes, partners, Keycast users and keys, and Authentik users may not.

Real restore on a new VM: provision and clone as above (or `tar -xpzf restored/code.tar.gz` for the exact deployed tree), copy `restored/` there, `infra/scripts/backup.sh restore restored` (refuses when the volumes exist), then build the images, `up -d`, unlock the custody seed, run bootstrap and the smoke test. Without `master.key`, Keycast's stored keys are unreadable; derived keys then come back through `restore-keys` ("Custody seed", step 4).

Rotating the issuer key: keep the old leaf in the accepted list of the door and bridge until the last certificate signed with it has expired (`infra/issuer/README.md`); old issuer keys stay in the backups.

## Not covered yet

Real-wallet testing (the EUDI reference wallet wants signed issuer metadata and a trusted issuer certificate), publishing Belfry's CA for wallets, encrypted verifier responses, the issuer key in a KMS, Blossom, and the community apps themselves.
