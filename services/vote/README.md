# Vote: anonymous polls for Open Source Village

`vote.<domain>`: any participant with an active pass opens a poll; every active pass votes once
per poll, anonymously, with Semaphore zero-knowledge proofs over groups built from the registry.

## How it works

- **Electorate.** The onboarding service lists the active passes and their Nostr keys
  (`GET /admin/active-passes`, bearer `PASS_INFO_TOKEN`; Odoo is the source). Everything is keyed
  on the pass id, so a participant who changes keys (self-custody) keeps their place.
- **Voting key.** On first use the browser creates a [Semaphore](https://docs.semaphore.pse.dev)
  identity and registers its public commitment for the pass (`PUT /api/me/identity`, NIP-98 auth
  signed through Keycast). The identity's secret stays on the device. It is deliberately **not**
  backed up encrypted to the participant's Nostr key: Keycast holds managed keys, so Belfry could
  decrypt such a backup and link votes to people. "Voting key" in the app shows an export code to
  move it to another device.
- **Groups per poll.** When a poll opens, every registered identity of an active pass joins its
  group; a pass that registers later joins every open poll it is not in yet. A pass's entry in a
  poll never changes, so it has exactly one nullifier there even if it registers a new key during
  the poll (the new key counts from the next poll). Every root the group had while open is
  accepted, so proofs made before later members joined stay valid.
- **Votes.** The browser builds a Semaphore proof (Groth16, tree depth 10, i.e. up to 1024 voters
  per poll): "one member of this poll's group chose option i", with the poll's scope and a
  nullifier. `POST /api/polls/<id>/votes` takes only the proof, no authorization header. The server
  checks scope, depth, option, root, unused nullifier and the proof itself.
- **Results** appear when the poll closes (no running tally); then every counted proof is
  published at `/api/polls/<id>/proofs`, so anyone can re-verify the tally.

What the anonymity covers: other participants and the public learn nothing about who voted how;
Belfry learns which passes registered a voting key, not their votes. It does see network
metadata (IP address and time of each request), so a vote sent right after a signed request from
the same address could be correlated by the operator; small polls are not anonymous at all (the
app warns below 5 members).

## API

| Route | Auth | |
| --- | --- | --- |
| `GET /api/polls[?session=<coordinate>]`, `GET /api/polls/<id>` | none, open to other origins | list (only the polls about one programme session with `session`), poll (results once closed) |
| `GET /api/polls/<id>/group` | none | the group's commitments in order (for the Merkle proof) |
| `GET /api/polls/<id>/proofs` | none | every counted proof, after the poll closed |
| `POST /api/polls` | NIP-98, active pass | `{title, description, options[2..10], closes_at, session?, session_title?}` (10 min to 31 days; `session` a kind-31923 coordinate) |
| `GET /api/me`, `PUT /api/me/identity` | NIP-98, active pass | the pass's registered commitment; register or replace it |
| `GET /api/polls/<id>/me` | NIP-98, active pass | the commitment the pass votes with in that poll |
| `POST /api/polls/<id>/votes` | none (the proof) | `{proof}` |

A poll can be about a programme session: the programme app's "Start a poll" opens
`#/new?session=<coordinate>&title=<title>`, the poll stores both (database column added on start
for older databases), says "About <session>" with a link back (`VITE_PROGRAMME_URL`, the
programme's `#/c/<coordinate>`), and the session page lists it from the public list.

NIP-98: `Authorization: Nostr <base64 kind-27235 event>` with `u` = `PUBLIC_URL` + path, `method`
and `payload` = SHA-256 of the raw body; 60 s validity, one use per event.

## Build and run

Node 24 (an exception to "Belfry's code is Python": Semaphore's proof verification and its
Poseidon Merkle tree are JavaScript), no framework: `server/` (node:http, node:sqlite in the
`vote-data` volume), `web/` (vanilla JS, Vite), one container serving both. The Dockerfile fetches
the depth-10 proving artifacts (`scripts/fetch-artifacts.sh`, SHA-256 pinned) into the app, so
browsers never load them from a third party, runs the tests and builds the web app with the
Keycast URL, the brand URL and the app bar's links. Sign-in is "Sign in with Open Source Village"
(Keycast client `belfry-vote`, registered and trusted by bootstrap and Compose), silent once per
tab; logout ends the Keycast and Authentik sessions like the other apps. Participants who took
their key into their own custody (Keycast patch 0012) get `error_description=self_custody` from
the sign-in and land on "Use your own signer": a NIP-07 browser extension or a `bunker://` URL from
their signer app (also linked from the sign-in card).

```bash
cd services/vote && npm ci && scripts/fetch-artifacts.sh && npm test
docker compose -f infra/docker-compose.yml --profile app up -d --build vote   # http://localhost:3500
```

Checks: `npm test` (NIP-98, validation, real proofs: double votes, replaced keys, tampered proofs,
results and proofs after close); smoke step 6n (page, artifacts, 401 without auth, 403 for a key
without a pass, 404 for an unknown poll); `infra/scripts/vote-check.mjs` drives Chromium through
silent sign-in, voting set-up, a new poll and a vote with a real proof (`COOKIE_JAR` = the
participant's Authentik session).
