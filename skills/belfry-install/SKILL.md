---
name: belfry-install
description: Set up a new Belfry (a digital belfry for a community) on a VPS, or change the settings of one. Use when someone asks you to install Belfry on a server, give a community its own belfry, connect mail (Resend or SMTP), Stripe, Mollie or another organisation's Odoo to a Belfry, or check whether one is running.
---

# Installing a Belfry on a server

A Belfry runs a community's sign-in, members' Nostr keys, relays, files, membership passes and
apps on one VM. One script sets it up: `https://belfry.sh/install.sh` (the source is
`install.sh` in github.com/commonshub/collective-belfry). Everything below happens over SSH as root.

## Before you start, ask the person for

1. **The server**: SSH access as root (or a sudoer). Ubuntu 24.04/26.04 or Debian 12/13, amd64,
   **4 GB RAM at least, 8 GB for real use**, and 15 GB of free disk with the prebuilt images
   (about 12 GB; 30 GB to build them on the server). Under 3.5 GB of RAM or 15 GB of disk the
   check fails; don't use `--force` to get past RAM, the stack won't run.
2. **The domain** and access to its DNS: A (and AAAA) records for `<domain>` and `*.<domain>`
   pointing at the server. The apex serves the community's start page, so don't use a domain
   that already hosts a website; a subdomain like `belfry.example.org` is fine.
3. **An e-mail** for Let's Encrypt notices (default `admin@<domain>`). The first admin is
   whoever opens the setup page first (below), not this address.
4. **The community's name.**
5. **Mail**: a Resend API key (resend.com → API Keys, sending access) or a Lettermint API token,
   and a sender address on a domain verified there. Without mail nobody can sign in. Other SMTP
   providers work too.
6. Optional, only with the Odoo add-on (`--odoo`; a new belfry keeps its own register, runs no
   Odoo and its memberships are free: `docs/register.md`): **Stripe** keys (publishable `pk_…`
   and secret `sk_…` or restricted `rk_…`) or a **Mollie** key for paid passes; **another Odoo**
   (URL, database, API key, organisation name) when another organisation books the sales.
7. Optional but recommended before real members join: the stewards' **age or YubiKey
   recipients** (`age1…`) for the custody seed that members' keys derive from.

Simplest, and what to do unless the person asks otherwise: install with only the domain (and the
community's name), then give the person the **setup page** the installer prints at the end
(`https://belfry.<domain>/setup`) and ask them to open it **right away**: while the belfry has no
admin it needs no code, and the first person to open it becomes the admin (their e-mail and a
username: `<username>@<domain>`; their browser makes the first session key). It then asks for a
Resend or Lettermint key so members can get sign-in codes, then verifies their address. They type
the keys there, over HTTPS; you never see them. If someone else got there first, `belfry
setup-reset` on the server removes that admin and opens the page again (`docs/setup.md`).

**To set the belfry up for them** (channels and so on), ask to join it as their agent right after
the install: follow `https://belfry.<domain>/agents.md` with your own Nostr key, give your
person's e-mail as `for`, and tell them to let you in at `https://belfry.<domain>/agents` once
they are the admin (they also get an e-mail). Once in, you can create channels (`docs/agents.md`).

Never ask for or print secrets in the chat.

## Steps

1. Check first, changing nothing:
   `ssh root@<host> 'curl -fsSL https://belfry.sh/install.sh | bash -s -- --dry-run --domain <domain> --email <email> --yes'`.
   Fix what it marks with ✗ (usually DNS; records can take a few minutes).
2. Write the settings to `/root/belfry.env` on the server, mode 600, one `BELFRY_*=value` per
   line: `BELFRY_DOMAIN`, `BELFRY_EMAIL`, `BELFRY_NAME`, `BELFRY_RESEND_API_KEY`
   (or `BELFRY_LETTERMINT_API_KEY`, or `BELFRY_SMTP_HOST`/`_PORT`/`_USER`/`_PASSWORD`),
   `BELFRY_MAIL_FROM`, and as needed `BELFRY_STRIPE_PUBLISHABLE_KEY`, `BELFRY_STRIPE_SECRET_KEY`,
   `BELFRY_MOLLIE_API_KEY`, `BELFRY_ACCOUNTING_ODOO_URL`, `_DB`, `_ISSUER`, `_API_KEY`,
   `BELFRY_CUSTODY_RECIPIENTS="age1… age1…"`. Send the file over SSH's standard input, not as
   a command-line argument.
3. Install: `ssh root@<host> 'curl -fsSL https://belfry.sh/install.sh | bash -s -- --config /root/belfry.env --yes'`.
   The first run takes about 10 minutes when prebuilt images exist for the code, and 20 to 40
   when the server builds the signer itself; run it in the background or with a long timeout. The log is `/var/log/belfry-install.log`. Running it again resumes and keeps
   what exists.
4. Check: `ssh root@<host> belfry status`. The public checks should answer 200, and
   `https://<domain>/.well-known/belfry.json` describes the new belfry for apps.
5. Tell the person what is left by hand (`infra/DEPLOY.md`): the admin network (ZeroTier) for the
   steward pages, the backups, and the custody seed under YubiKeys if it is still on the disk.
   Delete `/root/belfry.env` once they no longer need it (`shred -u`).

## Changing settings later

`https://belfry.<domain>/setup` (an admin, or a code from `belfry setup-code`) covers mail,
payments, accounting, the name, the theme, the homepage and `/git`. Over SSH: write the changed `BELFRY_*` lines to a file on the server and run `belfry config <file>`; it
writes them, runs bootstrap again and restarts what needs it. `BELFRY_ENV_<NAME>=<value>` sets
any other `infra/.env` setting. `belfry update` moves to the latest code. `belfry help` lists the
rest. The domain cannot be changed this way. The look of the pages: the `belfry-theme` skill.

## What the installer does to the server

Docker and ufw (SSH, 80, 443 and 25 for mail in; the rest denied), nginx only with `--nginx`, a 4 GB swap file on machines under
12 GB, a user `belfry` that also takes root's SSH keys, the code in `/opt/belfry`. Belfry's own
proxy (Caddy) does the certificates; with `--nginx`, nginx passes HTTPS through to it so other
sites can share the server (`/etc/nginx/stream.d/belfry.conf` explains how). The systemd unit
`belfry-setup.path` applies what the setup page saves.
