# Group test with real testers (belfry.tofauti.net)

A live test of the whole participant journey with 5–10 real people on the public test deployment
(`../infra/DEPLOY.md`): a guided session of about 75 minutes, a feedback round, then a few days in
which the testers use the apps freely. The smoke test covers every step with scripted clients;
this test is about what it cannot see: mail in real inboxes, real phones and browsers, and what
people understand when they read and click.

Payments run through Mollie in **test mode**: nobody pays real money. Every start, invite and
payment page shows a test-run notice. All data of the test is deleted afterwards (a fresh install
before the event).

| | |
| --- | --- |
| Date and time | [TO FILL] |
| Place or call link | [TO FILL] |
| Testers | [TO FILL: 5–10 names, one invite each] |
| Data deleted on | [TO FILL: date of the fresh install] |

## Roles

- **Steward and moderator** (the organiser): creates the invites, opens the poll, scans passes at
  `/check`, handles reports. Works on the steward pages `https://pass.belfry.tofauti.net/steward`,
  which answer only over the admin network: ZeroTier must be on on the steward's laptop and phone.
- **Observer**: a Claude session that follows the server logs during the session and notes errors
  with the time and the step (commands in "Watching the logs").
- **Testers**: a laptop with a current browser and a phone; an e-mail address they can read on
  both.

## Preparation

A week before:

- [ ] **Mail check.** Two test passes with a Gmail and an Outlook address (yours or a helper's):
  the activation mail and a magic link reach the inbox, not spam, and the links work. If they land
  in spam, check SPF and DKIM for the sender domain (`MAIL_FROM`, `../infra/DEPLOY.md`).
- [ ] **Pass dates and the test notice.** The passes must be valid on the test days, otherwise
  `/check` answers "not valid yet" (the event is in January 2027). A pass starts on the later of
  its payment day and `EVENT_START`, so in the VM's `infra/.env` set `EVENT_START` to the test day
  and `EVENT_END` to the last free-use day, and `TEST_RUN_NOTICE` to e.g. "Test run: no real money
  is charged, and all data is deleted afterwards." Then run bootstrap and recreate the onboarding
  service (`up -d onboarding`). The notice appears on the start, invite and payment pages, with
  Mollie's test card while Mollie is in test mode. Reset both before the event (the fresh install
  does). On belfry.tofauti.net: `EVENT_START=2026-11-01`, `EVENT_END=2026-11-14` (the test runs in
  the first two weeks of November); a pass bought before 1 November starts on 1 November, so a
  dry run in October sees "not valid yet" at `/check` (expected) and skips the door step.
- [ ] **Dry run with two people** through the whole plan below, including the chat, the market
  payment and the tip, on two different phones (one Android, one iPhone if possible).
- [ ] **Tester mail** sent (text below), with the date and the link.

The day before:

- [ ] A backup: `infra/scripts/backup.sh run` on the VM, then `backup.sh status`.
- [ ] One invite per tester on the steward pages (**Invites › Create invite**, the tester's name
  as the note). Keep the links ready to send, one per tester.
- [ ] Everything healthy (`../docs/runbook.md`, "Every morning"), the custody seed unlocked.

30 minutes before:

- [ ] **Prepare the poll question** (it is opened at minute 45 with "Open for: 15 minutes", so the
  results are there for the feedback round; participants activated later still join an open poll).
- [ ] The observer starts following the logs.
- [ ] ZeroTier on on the steward's laptop and phone; `/steward` opens.

## The session

Times are minutes from the start. Each block says what the testers do, what they should see, and
what the steward does meanwhile. Anything unexpected: note the block, the time, the page and a
screenshot (see "Reporting a problem").

### 0–15: start page, invite, pass, payment

| Testers | They should see | Steward |
| --- | --- | --- |
| Open https://belfry.tofauti.net. | What Open Source Village and Belfry are, that you need an invite from someone in the community, and the test-run notice. | Sends each tester their invite link (chat or mail). |
| Open the invite link. Pick a pass, enter name and e-mail (the one they can read on the phone). | The invite page with the passes and prices and the test-run notice. | |
| Pay with the test card **4543 4740 0224 9996**, any future expiry date, any CVC. | Mollie's checkout in test mode, then "paid" back on the shop page. | Watches the passes on `/steward` turn from `pending` to `active`. |

### 15–25: activation mail, sign in, the apps

| Testers | They should see | Steward |
| --- | --- | --- |
| Open the activation mail and click **Sign in to Open Source Village**. | Signed in without a password; the account page at `apps.belfry.tofauti.net` with the pass card and the community apps. | Helps whoever has no mail: spam folder, then **New sign-in link** on `/steward`. |
| Open **Directory**, then **Your entry**: name, **About you**, tick **List me in the directory**, save. | An avatar appears even without a picture of their own; after saving, their entry is in the list. | |
| Look at the others in the directory. | Everyone who chose to be listed. | |

### 25–45: in pairs

The steward pairs the testers (A and B). Each pair does all four, in any order.

| Pair | They should see |
| --- | --- |
| **Chat** (`chat.`): A opens B's entry in the directory, **Message**, writes a private message; B answers. | The message arrives on B's side within seconds; nobody else sees it. |
| **Calendar** (`calendar.`): A creates an event and invites or shares it with B. | B sees the event. |
| **Market** (`market.`): B posts an offer with a photo and a price in tokens; A pays it with **Pay**. | The wallet opens with the amount filled in, then back in the market; both see the payment on the offer. A's token balance goes down, B's up. |
| **Photos** (`photos.`): A posts a photo with a caption; B likes it, comments and tips 1 token. | A sees the like, the comment and the tip. |

### 45–55: the poll

| Testers | They should see | Steward |
| --- | --- | --- |
| Open **Vote** (`vote.`), the poll the steward opened, **Set up voting on this device**, then vote. | "You voted …", and the count of votes going up. Results appear when the poll closes. | Opens the poll at the start of the block ("Open for: 15 minutes"); explains that votes are anonymous: not even Belfry can tell who voted what. |
| Try to vote a second time. | Not possible: one vote per pass. | |

### 55–65: the pass at the door

| Testers | They should see | Steward |
| --- | --- | --- |
| On the phone: https://pass.belfry.tofauti.net/me (or **Show your pass** on the account page). | The pass with tier, dates and a QR code that renews itself; the screen stays on. | Scans each tester's QR with the phone's camera: the `/check` page shows *valid*, the tier and the dates, no name. |
| Optional: the wallet pass. In the walt.id web wallet (https://wallet.demo.walt.id) add the pass with **Add to your wallet** (account page or activation mail), then scan the QR at https://pass.belfry.tofauti.net/door. | The wallet shows the Open Source Village pass and asks to share the tier only; the door page answers *valid*. | Shows `/door` on a second screen. |

### 65–75: reports, logging out, phone sign-in, inviting a friend

| Testers | They should see | Steward |
| --- | --- | --- |
| Each reports one thing with **Report** (an entry in the directory, an offer or a photo) and gives a reason. | "Thank you. The stewards will look at it." | On `/steward`, **Reports**: picks one and **Hide content**. |
| Reload the reported page. | The hidden item is gone in the directory, market and photos. | |
| In any app: **Log out**. Then open another app. | Logged out everywhere; the other app asks to sign in. | |
| On the phone: open https://apps.belfry.tofauti.net, **Sign in with Open Source Village**, enter the e-mail, open the mailed link **in the same browser on the phone**. | Signed in on the phone. | Helps where the mail app opens links in its own browser: copy the link into the browser where the sign-in started. |
| On `/me`: **invite a friend** (up to 3). Send the link only to someone who wants to take part in the test. | A personal invite link; the count of invites left. | |

### Feedback round

Everyone fills in https://pass.belfry.tofauti.net/feedback (also in the app bar), then a short
round of talk:

1. What was confusing, and where did you hesitate?
2. What did you expect to happen that did not?
3. Which app would you use at the village, and which not?
4. Was anything slow or broken on your phone (which phone, which browser)?
5. Anything about privacy that worried you?

The poll has closed: look at the results together.

## After the session: free use

For [TO FILL: number of days] the testers use the apps as they like. Feedback keeps going to
`/feedback`; the stewards get each one by mail and see them on `/steward`. The steward checks the
reports once a day.

At the end: a short mail to the testers with thanks, what changes because of their feedback, and
the date their data is deleted.

## Tester mail

Subject: Test run: Open Source Village sign-in and apps, [TO FILL: date]

> Hi [name],
>
> thanks for helping us test the sign-in and the community apps for Open Source Village before
> the event. The test takes about 75 minutes on [TO FILL: date, time, place or call link],
> followed by a few days in which you can use the apps as you like.
>
> Please bring a laptop and your phone, and use an e-mail address you can read on both.
>
> You'll get your personal invite link at the start. It is a test run: when you buy a pass, pay
> with the test card 4543 4740 0224 9996, any future expiry date and any CVC. No real money is
> charged.
>
> What we store: your name, your e-mail address and what you post in the apps. Everything from the
> test is deleted on [TO FILL: date]. Please don't post anything you would mind others in the test
> group seeing.
>
> If something breaks or confuses you, that's exactly what we want to find: tell us at
> https://pass.belfry.tofauti.net/feedback or in the session.
>
> See you on [TO FILL: date],
> [TO FILL: name]

## Watching the logs

The observer, on the VM (`../docs/runbook.md` has the variables):

```bash
cd ~/belfry
C="docker compose -f infra/docker-compose.yml -f infra/docker-compose.prod.yml --profile app"
$C logs -f --since 5m onboarding keycast authentik-server authentik-worker odoo village vote mint \
  | grep -iE 'error|exception|traceback|refus|denied| 5[0-9][0-9] |timeout'
```

Also: `$C ps` for anything not healthy, and the proxy's 429 answers (rate limits) with
`$C logs --since 10m proxy | grep -c '"status":429'`. The venue network is exempt from the rate
limits only if it is in `RATE_LIMIT_EXEMPT` (`../infra/DEPLOY.md`); a remote session has no such
network.

## Reporting a problem

Note the block (for example "25–45, market"), the time, the URL, the phone and browser, the error
text or a screenshot. The observer matches it with the logs.

## Known limits

- A magic link must be opened in the browser where the sign-in started; mail apps on phones often
  open links in a browser of their own. A link opened elsewhere ends on a page with a button to
  sign in again. The 6-digit code in the same mail avoids that: type it on the sign-in page.
- Polls show results only after they close; there is no early close (the shortest is 15 minutes).
- The chat's private messages had not been tested between two real people before this test.
- The wallet pass works with walt.id's web wallet; national EUDI wallets and the EUDI reference
  wallet are not expected to accept Belfry as an issuer yet.
- Admin pages (`/steward`, Odoo's back office, Authentik's admin) answer only over ZeroTier.

## Results

| Block | Testers through | Problems (who, phone and browser, what) |
| --- | --- | --- |
| 0–15 start, invite, payment | | |
| 15–25 mail, sign-in, directory | | |
| 25–45 chat, calendar, market, photos | | |
| 45–55 poll | | |
| 55–65 pass at the door | | |
| 65–75 reports, logout, phone, invite | | |
| Feedback | | |
