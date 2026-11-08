# The belfry's own register (Odoo as an add-on)

Decided 2026-11-08: Odoo becomes optional. A belfry keeps who is who in its **own register**
(`REGISTER=belfry`, `services/onboarding/belfry_onboarding/register.py`): one SQLite file in the
`register-data` volume, owned by the onboarding service. With `REGISTER=odoo` (the add-on) Odoo
stays the register, as Open Source Village runs today.

## What the register keeps

People (name, e-mail, member number, key custody, verified key), passes (state, dates, keys,
wallet claim, one-time sign-in links), invites (from admins, from members, by e-mail),
applications, e-mail codes, reports, feedback and followers. It speaks the same small RPC as Odoo
(`OdooApi`), so the rest of the service is unchanged. Ids never get reused: the bunker derives every
key from `odoo-partner-<id>` (the name is historical), so a moved belfry keeps every key.

What it does not do: invoices, payments, tiers and prices, volunteer shifts. Those need the
Odoo add-on; the admin pages show them empty.

## Membership without Odoo

A membership is free and has no invoice (`tier` `member`, no end date: 2099-12-31, no tokens).

- An admin's membership: `belfry_steward_pass`, opened by `core.activate_free_pass`.
- An invite link (`<PUBLIC_URL>/invite/<token>`, `invite.py`): name and e-mail, a confirmation link
  by mail, then the membership opens (accounts, key, sign-in link mailed).
- System mails (sign-in links, codes, invites, alerts, digests) go through the mail provider
  (`SMTP_*`, `MAIL_FROM`; `mailer.py`).

## Running it

- New installs: `install.sh` sets `BELFRY_REGISTER=belfry` (no Odoo); `--odoo` keeps Odoo.
- `belfry` adds `infra/docker-compose.noodoo.yml` when `REGISTER=belfry`: Odoo only starts with
  `--profile odoo`, nothing waits for it, and `bootstrap.sh` skips its Odoo steps.
- Back up the `register-data` volume (`sqlite3 register.sqlite ".backup …"`).

## Moving a belfry off Odoo

1. With Odoo still running: `belfry compose exec onboarding python -m belfry_onboarding register-import`
   (copies people, passes, invites, applications, reports, feedback, followers with their ids;
   no billing links, no files).
2. Check the counts against Odoo.
3. `BELFRY_REGISTER=belfry belfry config`, then `belfry apply`. Odoo stops; its volume stays, so
   switching back (`BELFRY_REGISTER=odoo`) works until it is deleted.
