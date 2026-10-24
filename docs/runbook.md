# Event-day runbook

What to do on the server before and during the event, and when something breaks. The setup itself
is in `../infra/DEPLOY.md`. All commands run on the VM in the repository checkout, with:

```bash
cd ~/belfry
C="docker compose -f infra/docker-compose.yml -f infra/docker-compose.prod.yml --profile app"
ONB="$C exec -T onboarding python -m belfry_onboarding"
```

Admin and steward pages need the admin network (ZeroTier, `../infra/DEPLOY.md`).

## Contacts

| Role | Who | How |
| --- | --- | --- |
| On-call admin | [TO FILL] | [TO FILL] |
| Stewards with a YubiKey (custody seed) | [TO FILL] | [TO FILL] |
| Hosting (Hetzner Cloud console) | [TO FILL: account holder] | |
| DNS and mail relay (OVH) | [TO FILL: account holder] | |

## Before the event (once)

1. **Scheduled jobs.** `reconcile` catches payments whose webhook was lost and retries unfinished
   activations; `expire` ends passes after their last day. Add them to the deploying user's
   crontab (`crontab -e`):

   ```
   */5 * * * * cd ~/belfry && docker compose -f infra/docker-compose.yml -f infra/docker-compose.prod.yml --profile app exec -T onboarding python -m belfry_onboarding reconcile >> ~/belfry-cron.log 2>&1
   15 0 * * * cd ~/belfry && docker compose -f infra/docker-compose.yml -f infra/docker-compose.prod.yml --profile app exec -T onboarding python -m belfry_onboarding expire >> ~/belfry-cron.log 2>&1
   ```

2. **Log rotation.** Docker keeps container logs without limit by default. In
   `/etc/docker/daemon.json`:

   ```json
   { "log-driver": "json-file", "log-opts": { "max-size": "20m", "max-file": "5" } }
   ```

   then `sudo systemctl restart docker` and recreate the containers (`$C up -d --force-recreate`).
   The custody seed stays in memory; no unlock needed.

3. **Backups** run nightly and go off the VM; `infra/scripts/backup.sh status` says when the last
   one succeeded. Do one restore drill on a laptop (`../infra/DEPLOY.md`, "Backups").

4. **Door without the network.** Print the list of active passes (member number, tier, dates) the
   evening before, in case the server is unreachable at the door.

5. **A full test** with a real person, mailbox and wallet: `real-user-test.md`.

## Every morning

```bash
$C ps --format '{{.Service}} {{.Status}}' | grep -v healthy   # anything not healthy?
infra/scripts/custody-seed.sh status belfry@<host>              # from a steward's laptop
infra/scripts/backup.sh status                                  # last backup < 26 h ago
df -h / && docker system df                                     # disk
tail -n 20 ~/belfry-cron.log                                    # reconcile and expire ran
```

From outside, `https://pass.<domain>/health` answers `{"status":"ok"}`, and the landing page,
`https://apps.<domain>/` and `https://chat.<domain>/` load.

## After a reboot of the VM

Containers start by themselves. The custody seed is gone from memory, so new participants cannot
be activated until it is unlocked (existing participants are not affected).

1. Wait until everything is healthy: `$C ps`. The signer (`keycast`) takes over a minute.
2. A steward unlocks the seed from their laptop:
   `infra/scripts/custody-seed.sh unlock belfry@<host> custody-seed.age yubikey-<name>.txt`.
3. Activations that waited are retried by `reconcile` (or run `$ONB reconcile` now).
4. Optional: the smoke test (`../infra/DEPLOY.md`, "The smoke test on the server"). It creates and
   keeps a test participant.

If services have to be started by hand, start the data layer first, then the rest; Compose
waits for the health checks in between:

```bash
$C up -d postgres redis
$C up -d          # everything else, in dependency order
```

## Incidents

### A participant gets no sign-in mail

1. Ask them to check spam, and that they use the address they bought the pass with.
2. More than five links in 30 minutes for one account are refused; wait.
3. A link must be opened in the same browser where the sign-in started, within 10 minutes.
4. Mail delivery: Authentik's events (admin interface › Events) show the e-mail stage; Odoo's
   outgoing queue (Settings › Technical › Emails) shows failed activation mails. A relay that
   accepts but recipients never receive usually means the sender domain's SPF does not cover the
   relay (`MAIL_FROM`, `../infra/DEPLOY.md`).
5. A steward can send a new one-time sign-in link from the steward pages (passes › "New sign-in
   link"), or the participant can show their pass from a wallet instead.

### A participant cannot use the apps

- Is the pass active? Steward pages, or Odoo › Belfry › Passes.
- They must be in Authentik's group `belfry-participants`; `$ONB sync-participants` repairs it.
- Their key must be on the relay whitelist; `$ONB sync-relay` repairs it.
- Is the signer healthy (`$C ps keycast`)? After a restart, wait until it is.
- A participant who took their key into their own custody signs in with their own signer, not
  with "Sign in with Open Source Village".

### The door check does not work

- **QR code on the phone** (`/me`, checked at `/check/…`) needs only the onboarding service and
  Odoo. **Wallet passes** at `/door` also need the verifier.
- `$C ps onboarding verifier odoo`; restart what is unhealthy: `$C restart <service>`.
- A pass just revoked or refunded is refused; a wallet pass up to `STATUS_LIST_TTL` seconds later.
- If the server is unreachable: the printed list.

### A pass was revoked by mistake

```bash
$C exec -T onboarding python -c "from belfry_onboarding import core; \
from belfry_onboarding.app import build_services; from belfry_onboarding.settings import Settings; \
print(core.reactivate_pass(build_services(Settings()), <pass id>))"
```

It sets the pass active again and restores the signer account, the Authentik group and the relay
access. A pass whose last day has passed stays closed (`expired`).

### New participants are not activated

- Custody seed locked (after a reboot): unlock it (above).
- `$C logs --tail 100 onboarding` shows the failing step; `$ONB reconcile` retries.
- Odoo's webhook to the onboarding service failed: `reconcile` picks the invoice up within five
  minutes.

### The disk is full

```bash
df -h /; docker system df
docker builder prune --keep-storage 10GB -f     # build cache
ls -la ~/belfry-backups                           # local copies of backups (two are kept)
sudo du -sh /var/lib/docker/containers            # container logs (see "Log rotation")
```

### Something is badly broken

Restore from the last backup onto a new VM (`../infra/DEPLOY.md`, "Restore on a new VM"). With a
working VM but a lost signer database, `$ONB restore-keys` recreates every participant's account
with the same key from the custody seed.

## After the event

- Passes end by themselves (`expire`).
- [TO FILL: how long the apps stay online, and when accounts and data are deleted (privacy
  notice).]
