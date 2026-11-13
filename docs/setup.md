# A new belfry's first run, and undoing a claim

Decided by Xavier on 2026-11-13: no code to set up a new belfry. First come, first served, and
one command on the server for the unlikely case that someone else claims the admin's place.

## The homepage until a website is published

`https://<domain>/` (HOMEPAGE=nsite with nothing published, which is how install.sh starts)
shows a placeholder in the belfry's look (`theme.frame`: the base theme, plus the community's
theme folder if one is set). Fixed text and the settings only, no script. Two states:

- **No admin yet**: the community's name, one line on what a belfry is, **Set up this belfry** →
  `https://belfry.<domain>/setup`, and "Or let your agent do it" with the line to give an agent
  (`Join the belfry at <domain> as my agent: follow https://belfry.<domain>/agents.md`).
- **An admin, no website**: how to publish one (ask an agent, `skills/belfry-homepage`), who can
  publish, the link to `/website`, how agents ask to join, and "Join or sign in".

`https://belfry.<domain>/` shows the same choice to visitors while there is no admin.

## Open setup

While the belfry has **no admin at all** (no `infra/data/setup/first-admin.json` and nobody in the
stewards group), `https://belfry.<domain>/setup` opens the first-run wizard without a code: name
→ e-mail → username → mail provider → the 6-digit code sent to the admin. The page sets a setup
cookie marked `open`, which only counts while there is still no admin.

- **Two people at once**: creating the first admin takes a lock (a thread lock and an `flock` on
  `infra/data/setup/.first-admin.lock`) and checks again inside it. One becomes the admin; the
  other gets "Someone else has just become this belfry's admin. Ask them to invite you." (409).
- **After the claim**, other visitors of `/setup` see that someone is setting it up, with a
  pointer to `belfry setup-reset`; they need a code or an admin's sign-in as before.
- **Codes keep working**: old links (`/setup?code=…`) and `belfry setup-code [NAME]`.
- The installer prints the plain `https://belfry.<domain>/setup`.

**Exposure**: a fresh belfry can be claimed by whoever reaches `/setup` first, from the moment
the stack answers on HTTPS until its owner opens the page. The installer says to open it at once.
Accepted by Xavier; the remedy is the reset below.

## `belfry setup-reset`

On the server, as root (or the belfry user):

    belfry setup-reset [--yes] [--all-admins] [--keep-settings]

It first shows who would be removed and which settings the setup page changed during the claim,
asks to confirm (type `yes`; `--yes` skips that), then:

1. **The first admin** (from `first-admin.json`) loses their memberships: every pass revoked, so
   the relays drop their key and their own agents go with them; their signer (Keycast) account is
   suspended; they leave the stewards group. With `--all-admins`, every other admin goes the same
   way (`/setup` opens only when nobody is admin; without the flag the remaining admins are listed).
2. `first-admin.json` and `first-admin-changes.json` are kept as `first-admin.reset-<time>.json`
   and `first-admin-changes.reset-<time>.json`; `reset.json` records the time, and every setup
   cookie made before it stops working (the claimant's console cookie already ended with the pass).
3. `pending.env` (settings queued but not applied yet) is deleted.
4. **Settings** the setup page changed while the claim stood (recorded in
   `first-admin-changes.json` by `write_pending`), unless `--keep-settings`:
   - **secrets are cleared**: a mail key (Resend or Lettermint) empties `secrets/smtp_password`
     and puts mail back to none (`SMTP_HOST=mailpit`, as a new belfry starts), because mail sent
     through a stranger's account would show them every sign-in code. Stripe, Mollie and the
     accounting key are emptied too;
   - **other settings get their earlier value** (name, sender, homepage, theme, `/git`, added
     apps, accounting ids). A name with no earlier value is kept.
   - Then `belfry apply`, so the services read them.
5. It prints what it did and the open `/setup` link, and records "Setup reset" in the changelog.

What it leaves: members the claimant invited, channels they made, their kind-0 profile on the
relay. Pending agents' requests stay for the next admin to answer. Run again with no admin, it
says there is nothing to reset. The Python side is `python -m belfry_onboarding setup-reset
[--all-admins] [--dry-run]` (prints JSON; `setup.reset`), the settings side
`infra/scripts/setup-revert.py`.
