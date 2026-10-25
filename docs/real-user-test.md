# Real-user test of the happy path (belfry.tofauti.net)

The plan's happy path with a real person, a real mailbox and a real wallet, on the public test
deployment (`infra/DEPLOY.md`). The smoke test already covers every step with a scripted wallet
and fake addresses (91 checks green on the server, 2026-10-23); this run is about what it cannot
see: mail delivery to a real inbox, a real wallet, and what a participant actually reads and clicks.

Status 2026-10-23: deployment ready, test not run yet. Results go in the table at the end.

## What you need

- Two browser windows: a normal one for the steward (admin), a private one for the participant.
- A real e-mail address for the participant (a second address of yours works).
- For steps 7–9 only: a wallet. Steps 1–6 work with e-mail alone.
  1. walt.id web wallet, https://wallet.demo.walt.id (in the browser, no install; same software
     family as Belfry's issuer and verifier, so the best chance). Untested with this pass format.
  2. wwWallet, https://demo.wwwallet.org (also in the browser), if walt.id fails.
  3. EUDI reference wallet (Android): later; it will most likely refuse Belfry as an issuer until
     Belfry's CA is published (NEXT_STEPS §3e open item 1).
- A steward account for yourself (once; the e-mail receives the steward magic links):

  ```bash
  ssh belfry@188.245.110.191 'cd belfry && docker compose -f infra/docker-compose.yml -f infra/docker-compose.prod.yml --profile app exec -T onboarding python -m belfry_onboarding add-steward <your e-mail> "<your name>"'
  ```

- The admin passwords (only for Odoo/Authentik admin work, not needed for the test), read in your own terminal (never paste them into a chat):

  ```bash
  ssh belfry@188.245.110.191 "cd belfry/infra/secrets && head odoo_admin_password authentik_bootstrap_password"
  ```

## Steps

| # | Who | Do | Expect |
| --- | --- | --- | --- |
| 1 | Steward | Open https://pass.belfry.tofauti.net/steward, enter your steward e-mail, open the magic link (same browser). Type a note, **Create invite**, **Copy link**. | Invite listed as `open`; the link is `odoo.belfry.tofauti.net/belfry/invite/<token>`. |
| 2 | Participant | Open the invite URL. Pick a tier (day pass is cheapest), enter name and the real e-mail; `OSV10` gives 10 % off. Submit. | Redirect to Odoo's payment page. Stripe is not connected yet: stop here. |
| 3 | Steward | Reload https://pass.belfry.tofauti.net/steward: the new pass is `pending` with its invoice. **Mark paid** → confirm (cash or bank transfer received). | "Payment registered. The pass is active…"; the row shows `active`, the invoice `paid`, "marked paid by <you>". |
| 4 | Participant | Wait for the activation mail from `belfry@tofauti.net`. | Mail with "Sign in to Belfry" and "Add the pass to your wallet". Not there after a minute: spam folder, then Odoo **Settings › Technical › Emails** (developer mode) for the failure reason. |
| 5 | Participant | Click **Sign in to Belfry** in the mail. | Signed in to Authentik without a password, user portal shown. The link works once (valid 7 days). |
| 6 | Participant | Open https://apps.belfry.tofauti.net → **Sign in with Belfry**. If asked, enter the e-mail and open the mailed link **in the same browser**. | Back in Keycast, signed in, the Nostr key shown. |
| 6b | Participant | In a fresh private window open https://login.belfry.tofauti.net, enter the e-mail, open the magic link, click **Continue**. Then open the same link again. | First time: signed in. Second time: refused. Link valid 10 minutes. |
| 7 | Participant | Open the claim link from the mail (`pass.belfry.tofauti.net/claim/<token>`) on a laptop and scan the QR with the wallet (on the phone itself: use the link/button on the page). | The wallet shows the village pass with tier and dates; name, member number and npub are selectively disclosable. |
| 8 | Steward + participant | Open https://pass.belfry.tofauti.net/door on a second screen, scan its QR with the wallet. | The wallet asks for the pass tier only; the door page shows tier, valid, not revoked. |
| 9 | Participant | In a fresh private window open https://login.belfry.tofauti.net → **Village pass (EUDI wallet)**, scan, approve in the wallet. | Signed in as the same participant, no e-mail, no password. The first time links the pass to the account. |
| 10 | Steward | Optional, last: open the pass in Odoo → **Revoke pass**, repeat step 8. | The door refuses the pass within 5 minutes (`STATUS_LIST_TTL=300`). App sign-in is refused too. |

## Known limitations

- Magic links for an app login (step 6) must be opened in the browser where the login started;
  elsewhere Keycast answers with a bare HTTP 400 (safe, unfriendly).
- An unknown e-mail on the login page gets "Failed to authenticate." and no mail.
- The door only works with a wallet; the plan's printed-QR fallback is not built.
- Stripe is not connected; a steward registers the payment by hand (step 3). A payment registered in Odoo's own UI does **not** activate the pass (no Stripe transaction, no steward mark; `ALLOW_MANUAL_PAYMENTS` is off).
- Odoo's UI (`admin`) is for admins and accounting only; stewards use `/steward`.
- Admin login to Authentik: https://login.belfry.tofauti.net/if/flow/default-authentication-flow/?next=/if/admin/
  (`akadmin`, `AUTHENTIK_BOOTSTRAP_PASSWORD`); the login page itself has no password field.

## When something breaks

Note the step number, the URL, the error text or a screenshot, and for wallet steps the wallet's
name and version. Server-side logs (in your own terminal):

```bash
ssh belfry@188.245.110.191 'cd belfry && docker compose -f infra/docker-compose.yml -f infra/docker-compose.prod.yml --profile app logs --since 30m onboarding authentik-server issuer verifier'
```

## Results

| Date | Tester | Wallet | Steps passed | Problems |
| --- | --- | --- | --- | --- |
| | | | | |
