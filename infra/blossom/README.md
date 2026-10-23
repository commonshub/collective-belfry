# Blossom file server

Photos and files of the community apps are blobs on a [Blossom](https://github.com/hzrd149/blossom) server (`hzrd149/blossom-server`, image pinned by digest in `docker-compose.yml`; the project has no releases). Clients upload with a Nostr-signed authorization (BUD-11, kind 24242) and reference blobs by SHA-256 URL.

## Hardening

Two independent checks guard every write:

1. **blossom-server** itself (`config.yml`): `requireAuth` on upload and delete (it verifies the kind-24242 signature), 25 MB size limit, MIME allowlist with retention (images 1 year, video/audio 3 months, PDF and text 1 year), blobs without owners pruned, no mirror endpoint (SSRF surface), no server-side media processing, no listing, no admin dashboard.
2. **The gate** (`Caddyfile`, container `blossom-gate`): every `PUT`/`DELETE` is first sent to the onboarding service's `GET /blossom/auth` (Caddy `forward_auth`, with the original method in `X-Forwarded-Method`). The service re-validates the event (kind, id, Schnorr signature, `t` verb matching the method, `expiration` in the future and at most 24 h after `created_at`, optional `server` tag naming one of `BLOSSOM_HOSTS`) and requires that the pubkey holds an **active pass** in Odoo. 401 for a bad token, 403 for a valid token without a pass or with the wrong verb. `/mirror`, `/media`, `/list/*`, `/report` and `/admin` are not forwarded at all. Reads (`GET`/`HEAD /<sha256>`) pass through: blobs are content-addressed and not listable.

So a revoked or expired participant cannot upload anymore (the gate refuses), and cannot even sign the authorization event once Keycast suspended the account. Public reads are by design (the apps embed blob URLs); if private files are needed later, that is a separate server or an auth-on-read rule.

The participants' signer must be allowed to sign kind 24242: `infra/keycast/policy.sql` includes it.

## URLs

Locally `http://localhost:3100` (the gate; the server itself is not published). In production the proxy routes `blossom.<domain>` to the gate; `BLOSSOM_PUBLIC_DOMAIN` (bare host[:port]) is what blob URLs and the auth event's `server` tag carry.

## Checked by the smoke test

Step 6f: a kind-24242 event signed through Keycast with the participant's key uploads a blob (200 with the descriptor), the blob is readable without auth, an upload without authorization gets 401, the mirror endpoint 404, a delete with an upload token 403; after the pass is revoked (step 11), signing is refused by Keycast and an upload with a fresh token gets 403 from the gate.
