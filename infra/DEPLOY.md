# Deploying Belfry

Belfry runs on one VM with Docker Compose. The public deployment is the local stack
(`README.md`) plus `docker-compose.prod.yml`, which puts a Caddy reverse proxy with automatic TLS
in front of everything and publishes no other port.

## What you need

- A VM with Docker and Compose v2, 4 vCPU and 8 GB RAM, ports 80 and 443 open. amd64: the relay
  image has no ARM build.
- A domain. Every service gets a subdomain: `shop.` (Odoo; `ODOO_HOST_PREFIX` changes the
  name), `login.`, `apps.`, `pass.`, `relay.`, `bunker.`, `blossom.`, `mint.`, `issuer.`,
  `verifier.`, `chat.`, `calendar.`, `wallet.`, `vote.`, `people.`, `market.`, `photos.`, plus the
  domain itself (NIP-05 names are `<member>@<domain>`). A wildcard record covers them all: A and
  AAAA for `<domain>` and `*.<domain>`.
- An SMTP relay for the mails (sign-in links, activation mails) and a sender address whose domain
  allows that relay in its SPF record (better also DKIM). Otherwise the relay accepts the mails and
  recipients silently drop them.
- Stripe keys for Odoo's Stripe payment provider.
- For the admin network: a ZeroTier account (free tier is enough).
- For the custody seed and the backups: two YubiKeys, or software keys while testing.

## Example: Hetzner Cloud with DNS at OVH

`hetzner/provision.sh` creates the VM with the `hcloud` CLI: SSH key, firewall (22, 80,
443/tcp+udp) and a cx33 (4 vCPU, 8 GB) set up by `hetzner/cloud-init.yaml` (Docker, a user
`belfry`, no password or root login over SSH). `DRY_RUN=1` prints the commands. Allow about two
minutes for cloud-init before logging in. Hetzner blocks outgoing ports 25 and 465: use SMTP
submission on 587.

With the zone at OVH, the `ovhcloud` CLI creates the records (a token limited to the zone):

```bash
ovhcloud domain-zone record create example.net --sub-domain '*.belfry' --field-type A --target <ipv4> --ttl 300
ovhcloud domain-zone refresh example.net
```

## First installation

```bash
git clone <this repo> belfry && cd belfry
infra/scripts/gen-secrets.sh                                   # .env, keys, Odoo config
infra/scripts/prod-env.sh belfry.example ops@belfry.example     # public URLs and hostnames in .env
# the issuer and verifier certificates must carry the public hostnames; regenerate them
# (only before the first pass is issued: existing passes are signed with the old key)
rm infra/issuer/keys/issuer.* infra/issuer/keys/verifier.* infra/issuer/keys/ca.*
infra/scripts/gen-secrets.sh
```

In `infra/.env` set `SMTP_HOST`, `SMTP_PORT`, `SMTP_USER`, `SMTP_ENCRYPTION` and `MAIL_FROM`. The
SMTP password never goes into `.env`; copy it from your machine:
`infra/scripts/put-secret.sh <ssh target> smtp_password`.

```bash
C="docker compose -f infra/docker-compose.yml -f infra/docker-compose.prod.yml"
$C up -d                                  # builds the signer on the first run (about 15 minutes)
infra/scripts/custody-seed.sh unlock …    # from a steward's laptop, see "Custody seed"
ODOO_URL=https://shop.belfry.example AK_URL=https://login.belfry.example KC_URL=https://apps.belfry.example \
  infra/scripts/bootstrap.sh
$C --profile app up -d --build            # onboarding service and apps
```

On a server, bootstrap reaches the services through their public hostnames and runs the calls
the proxy blocks with `docker compose exec` inside the containers.

Stripe: put the publishable key into `infra/.env` (`STRIPE_PUBLISHABLE_KEY=pk_test_…`) and the
secret key onto the server with `infra/scripts/put-secret.sh <ssh target> stripe_secret_key`, then
run bootstrap again. It configures Odoo's Stripe provider (test keys: test mode, live keys:
enabled), has Odoo create its own webhook, creates Belfry's dispute webhook
(`https://pass.<domain>/hooks/stripe`, `charge.dispute.created` and `.closed`) and writes its
signing secret into `.env` as `STRIPE_WEBHOOK_SECRET`; restart the onboarding service after that.
Without keys, stewards mark payments by hand. Then add the first steward (see "Stewards"), set
up the admin network and the backups.

### The smoke test on the server

```bash
export ODOO_URL=https://shop.belfry.example AK_URL=https://login.belfry.example \
  KC_URL=https://apps.belfry.example ONB_URL=https://pass.belfry.example \
  BLOSSOM_URL=https://blossom.belfry.example WALLET_MAP="" BUNKER_MAP="" ONB_ADMIN_EXEC=1
infra/scripts/smoke.sh
```

It creates a test participant with a fake address, so the activation mail itself is refused by the
relay; that step is reported as a note. Test real mail delivery with a real address.

### Checks after the first start

- `https://issuer.<domain>/.well-known/openid-credential-issuer/openid4vci` lists `village_pass`.
- `https://pass.<domain>/status/1` answers `application/statuslist+jwt`;
  `https://pass.<domain>/vct/village-pass/v1` the type metadata.
- `https://login.<domain>/application/o/keycast/.well-known/openid-configuration` exists.
- `https://<domain>/.well-known/nostr.json?name=<member>` after the first activation.
- A test purchase in Stripe test mode: the activation mail arrives with its links, the claim page
  issues to a wallet, the door accepts the pass, the pass signs in.

Day-to-day operation, scheduled jobs and incidents: `../docs/runbook.md`.

## Updating a deployment

Pull or copy the new commits, rebuild what changed and start it:

```bash
$C --profile app build <services> && $C --profile app up -d <services>
```

- Run bootstrap again when public URLs, SMTP settings, Authentik blueprints or the signer's
  policy changed.
- After restarting `keycast`, wait until it reports healthy before running the smoke test; it
  takes over a minute to start.
- After a change to `proxy/Caddyfile` (the directory is mounted into the proxy), reload it:
  `$C exec -T proxy caddy reload --config /etc/caddy/Caddyfile --adapter caddyfile`, or
  `$C restart proxy`. `$C up -d proxy` alone does nothing when only the Caddyfile changed.
- Image builds leave a large build cache. `docker builder prune --keep-storage 10GB -f` keeps the
  recently used layers and frees the rest.

## Sign-in for participants and admins

Participants sign in with an e-mail link or their pass; they have no passwords. The login page
mails a one-time link valid for 10 minutes, at most five per account in 30 minutes. The mails go
out through the SMTP settings bootstrap writes into Authentik (`SMTP_*`,
`infra/secrets/smtp_password`); run bootstrap again after changing them.

The Authentik admin `akadmin` signs in with its password at
`https://login.<domain>/if/flow/default-authentication-flow/?next=/if/admin/`, over the admin
network only.

## Admin network (ZeroTier)

The admin pages answer only over a private ZeroTier network: Odoo's back office and login
(`/odoo`, `/web`, `/web/login`, the database manager, XML/JSON-RPC), Authentik's admin interface
and the password login, the steward pages (`pass.<domain>/steward`) and the signer's admin pages.
From anywhere else they answer 403. Participants' pages are not affected.

The proxy compares each connection's address with `ADMIN_NETS`: the ZeroTier subnet, the VM's
own public IPv4 and IPv6 addresses and Docker's ranges. The VM's own addresses are needed because
bootstrap and the smoke test on the VM reach the proxy through the public hostnames.

1. In ZeroTier Central (https://my.zerotier.com) create a network with access control
   **private** and an IPv4 auto-assign pool, for example `10.147.17.0/24`. Note the network id.
2. On the VM: `infra/scripts/zerotier.sh install`, then `infra/scripts/zerotier.sh join <network id>`.
3. In ZeroTier Central authorise the VM's node (the id `join` printed) and give it a fixed address.
4. On the VM: `infra/scripts/zerotier.sh status` prints the VM's ZeroTier address and an
   `ADMIN_NETS=…` line. Put that line into `infra/.env` and recreate the proxy
   (`$C up -d proxy`).
5. On each admin's and steward's device: install ZeroTier (desktop, Android, iOS), join the
   network, authorise the device in ZeroTier Central, and point the hostnames at the VM's
   ZeroTier address in the hosts file, for example
   `10.147.17.10 shop.<domain> login.<domain> pass.<domain> apps.<domain>`. The TLS certificates
   stay valid (same names). On phones without a hosts file, use ZeroTier's DNS setting in Central.
6. Check from outside the network: `https://pass.<domain>/steward` answers 403; from inside it
   leads to the sign-in.

Stewards who work at the door from a phone need ZeroTier on that phone. Opening UDP 9993 in the
VM's firewall allows direct ZeroTier connections; without it traffic goes through ZeroTier's
relays, which is slower but works.

## Stewards

Stewards work on `https://pass.<domain>/steward` (over the admin network): create and revoke
invites, mark a pass paid by hand, re-send a sign-in link, revoke a pass. They sign in like
participants. Odoo's back office is for admins and accounting. Make someone a steward (creates a
passwordless Authentik user if needed and adds it to the group `belfry-stewards`):

```bash
$C --profile app exec -T onboarding python -m belfry_onboarding add-steward <e-mail> "<name>"
```

## What the proxy does (`proxy/Caddyfile`)

| Host | Serves | Restricted |
| --- | --- | --- |
| `shop.` | Odoo: invite pages, payment, invoices | back office, login, database manager, RPC: admin network only |
| `login.` | Authentik | admin interface and password login: admin network only |
| `apps.` | the signer: participants' account page and API | `/api/admin/*` denied; `/admin` admin network only |
| `<domain>` | `/.well-known/nostr.json` (NIP-05) from the signer; everything else redirects to `shop.` | |
| `pass.` | onboarding service: landing page, claim, tokens, the pass on the phone (`/me`, `/check`), door, pass sign-in bridge, status list, type metadata, Stripe webhook, brand files | `/admin/*`, `/hooks/odoo/*` and the API docs denied; `/steward` admin network only |
| `relay.` | the community relay | |
| `bunker.` | the signer's NIP-46 relay (`BUNKER_PUBLIC_RELAYS=wss://bunker.<domain>` goes into bunker URLs) | |
| `blossom.` | file server, through its gate | uploads need a participant's signature |
| `mint.` | the token mint | `/v1/mint/*` and `/v1/melt/*` denied: only the onboarding service mints |
| `issuer.` | Issuer2, the wallet-facing part | `/issuer2/*` (management API) denied |
| `verifier.` | Verifier2, only the request and response of a session | everything else denied |
| `chat.`, `calendar.`, `wallet.`, `vote.`, `people.`, `market.`, `photos.` | the community apps | |

Protections on top:

- **TLS** from Let's Encrypt (`ACME_EMAIL`), HSTS and the usual security headers.
- **Rate limits per client address**, set high because the venue's Wi-Fi puts hundreds of
  participants behind one address: Authentik's flow steps 300/min, claim and token pages
  120/min, `/me` sign-in 120/min, `/check` 600/min, pass sign-in bridge 300/min, door 60/min,
  issuer 300/min, invite pages 120/min. Beyond that the answer is 429 with `Retry-After`. Mail
  flooding is limited per account in Authentik, not by address.
- **Content-Security-Policy** on every browser app: scripts only from the app itself, connections
  only to Belfry's hosts (the chat may reach any relay), no framing. Odoo and Authentik ship
  inline code and get none. Blossom's files are served sandboxed.
- **Containers** run without root (most as user 10001; the signer as the deploying user,
  `HOST_UID`, who owns `master.key` and the custody seed), with `no-new-privileges`, and without
  capabilities where they start unprivileged. `perms-init` hands the data volumes to those users.
- **Networks**: Postgres and Redis are on the internal network `data` with their clients only;
  the static apps only on `edge` with the proxy. `edge` has IPv6, so the proxy sees IPv6 visitors'
  real addresses.
- **No mail catcher**: Mailpit only runs locally (Compose profile `dev` in production).

Odoo's webhooks to the onboarding service and Authentik's token and userinfo calls to the bridge
stay inside the Compose network, so the proxy can deny those paths publicly.

**Signer and Authentik behind the proxy.** The overlay unsets `OIDC_BACKCHANNEL_URL` for the
signer: it reaches Authentik through `https://login.<domain>`, not the Compose network, because
Authentik derives the issuer's scheme from the request and would announce `http://` to an internal
caller, which the signer rejects.

## Custody seed (recoverable participant keys)

Participants' Nostr keys are derived from one secret, the custody seed, and the participant's
Odoo id (`keycast/README.md`). With the seed and Odoo, every managed key can be recreated. The
seed is encrypted to the stewards' YubiKeys (either one decrypts it) and on the VM it only lives
in memory (`/run/belfry`, `CUSTODY_SEED_DIR=/run/belfry`, set by `prod-env.sh`). Needs `age` and
`age-plugin-yubikey` on the steward's laptop.

1. Each YubiKey once: `age-plugin-yubikey` (interactive: creates the key, PIN and touch policy),
   then `age-plugin-yubikey --identity > yubikey-<name>.txt` (names the key; the secret stays on
   it).
2. Once, with all YubiKeys at hand: `infra/scripts/custody-seed.sh recipients` per key, then
   `infra/scripts/custody-seed.sh init custody-seed.age <recipient 1> <recipient 2>`. Give
   `custody-seed.age` to each steward and put it into the backups; it is useless without a
   YubiKey.
3. After every reboot of the VM (the seed is gone from memory; signing keeps working, new
   participants wait and their activation is retried):
   `infra/scripts/custody-seed.sh unlock belfry@<host> custody-seed.age yubikey-<name>.txt` (touch
   the key), then `… status belfry@<host>`.
4. Recovery after losing the signer's database or `master.key`: unlock the seed, start the
   signer with an empty database and run
   `docker compose exec onboarding python -m belfry_onboarding restore-keys`. Derived keys come
   back with the same public keys; participants who moved to their own key get an account without
   a key for their public key.

**Testing without YubiKeys.** Plain age keys can stand in (`age-keygen -o test-key-a.txt`, and a
second one): their `age1…` public keys are the recipients for `init` and `backup.sh keygen`, the
files are the identities for `unlock` and `decrypt`. To move to YubiKeys later, keep the seed and
re-encrypt it:
`custody-seed.sh rewrap custody-seed.age custody-seed.yubikey.age test-key-a.txt <yubikey recipient 1> <yubikey recipient 2>`,
and the same with `backup.sh rewrap` for the backup key. Check `unlock` with each YubiKey, then
destroy the test keys and the old files. Participants' keys and existing backups stay valid. A new
seed (`init`) would instead leave everyone activated before with a key that cannot be recovered.

## Backups

`scripts/backup.sh` writes one encrypted file per night and sends it off the VM. It holds every
Postgres database (`pg_dumpall`), the data volumes that cannot be rebuilt from Odoo (Odoo's file
store, mint, polls, Blossom, the community relay, Authentik's media; the SQLite and LMDB ones are
paused for the few seconds of the copy), the secret files (`.env`, `keycast/master.key`,
`issuer/keys/`, the rendered configs, `secrets/` without the custody seed) and the deployed code.
The VM only holds the public half of the backup key: it can write backups but not read them. The
plain custody seed is never in a backup; `custody-seed.age` is, if you copy it to `secrets/`.

1. On a steward's laptop, with the YubiKeys:
   `infra/scripts/backup.sh keygen backup-identity.age <recipient 1> <recipient 2>`. Keep
   `backup-identity.age` with every steward next to `custody-seed.age`. It prints
   `BACKUP_RECIPIENTS=age1…` for the VM. A second recipient whose key is kept offline, on paper,
   protects against losing both YubiKeys.
2. An off-VM target, for example a Hetzner Storage Box with SSH and **automatic snapshots**
   enabled (the VM's key can delete files there, but not snapshots): on the VM
   `ssh-keygen -t ed25519 -N '' -f ~/.ssh/belfry-backup`, add the public key to the box,
   `ssh-keyscan -p 23 <box host> >> ~/.ssh/known_hosts`, create the directory
   (`echo 'mkdir belfry' | sftp -P 23 -i ~/.ssh/belfry-backup <user>@<box host>`).
3. On the VM: `sudo apt install age`; in `infra/.env` `BACKUP_RECIPIENTS` and
   `BACKUP_TARGET=sftp://<user>@<box host>:23/belfry` (a local directory works too, but does not
   protect against losing the VM). Then `infra/scripts/backup.sh run` once and
   `infra/scripts/backup.sh install` (systemd timer, 03:15 UTC, `BACKUP_AT`). `backup.sh status`
   exits 1 when the last success is older than 26 hours (for monitoring); `backup.sh list` shows
   the target. The last two backups also stay on the VM in `~/belfry-backups`.

**Restore drill** (after the first backup, then monthly, on a laptop with Docker and a YubiKey):
fetch a file, `infra/scripts/backup.sh decrypt <file> backup-identity.age yubikey-<name>.txt -o restored`,
then `infra/scripts/backup.sh drill restored`. The drill unpacks the backup's own code into a
temporary directory, restores files, volumes and databases into the Compose project
`belfry-drill` (only Postgres runs, no ports, a running stack is not touched), compares file
hashes, volume file counts and every table's row count with the backup's manifest, and removes
everything again. Busy tables may differ by a few rows; passes, partners, signer accounts and keys
and Authentik users may not.

**Restore on a new VM**: provision and clone as above (or `tar -xpzf restored/code.tar.gz` for
the exact deployed code), copy `restored/` there, run `infra/scripts/backup.sh restore restored`
(it refuses when the volumes already exist), then build the images, start them, unlock the
custody seed, run bootstrap and the smoke test.

**Rotating the issuer key**: keep the old certificate in the accepted list of the door and the
bridge until the last pass signed with it has expired (`issuer/README.md`); old keys stay in the
backups.

## Known limits

- Real EUDI wallets: the smoke test uses a scripted wallet. Wallets that still speak an older
  draft of OpenID4VCI (walt.id's web wallet) go through a small compatibility layer in the
  onboarding service (`/issuer-shim/*`). The EUDI reference wallet also wants signed issuer
  metadata and a trusted issuer certificate, and Belfry's CA is not published for wallets.
- Verifier responses are not encrypted (`direct_post`, not `direct_post.jwt`).
- The issuer key is a file on the VM, not in a hardware module.
