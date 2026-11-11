# Belfry push gateway

Passes push notifications from any belfry to the Belfry phone apps (`sh.belfry.app`) through
Apple (APNs) and Google (FCM). Only the apps' publisher runs it (belfry.sh, at
`https://push.belfry.sh`); every other belfry sends to it (`PUSH_GATEWAY_URL`). It never sees
what a notification says: the belfry encrypts it to a key the phone made. The contract is
[`docs/push.md`](../../docs/push.md) §2.

`POST /v1/send` (NIP-98 by the belfry's key) and `GET /health` (which platforms are configured).
Without credentials it starts and answers `503 {"error": "not_configured"}` for that platform.

## Turning it on (the publisher's belfry)

1. **APNs key.** In the Apple Developer account (Certificates, Identifiers & Profiles → Keys),
   create a key with "Apple Push Notifications service (APNs)" enabled. Download the `.p8` (only
   once), note its **Key ID** and the account's **Team ID**. One key serves the sandbox and
   production; the app's bundle id `sh.belfry.app` needs the Push Notifications capability.
2. **FCM service account.** In the Firebase project of the Android app (Project settings →
   Service accounts), "Generate new private key": a JSON file with `type: service_account`. Its
   project must be the one in the app's `google-services.json`.
3. Copy both files to the server, readable by the belfry user (e.g. `/home/belfry/push/`), and
   write a settings file:

   ```
   BELFRY_PUSH_GATEWAY=1
   BELFRY_APNS_KEY_ID=ABC123DEFG
   BELFRY_APNS_TEAM_ID=XYZ987TEAM
   BELFRY_APNS_KEY_FILE=/home/belfry/push/AuthKey_ABC123DEFG.p8
   BELFRY_FCM_SERVICE_ACCOUNT_FILE=/home/belfry/push/firebase-service-account.json
   ```

   then `belfry config push.env`. The files are copied into `infra/secrets/apns_key` and
   `infra/secrets/fcm_service_account` (mode 640, never printed); delete the copies afterwards.
   `belfry status` shows whether they are set. DNS: `push.<domain>` must reach the server (the
   `*.<domain>` wildcard does).
4. Check: `curl https://push.<domain>/health` → `{"ok": true, "apns": true, "fcm": true}`.

Rotating a key: run `belfry config` again with the new file (and key id); the gateway restarts
with it on `belfry apply`.

## Other belfries

Nothing to do: they send to `https://push.belfry.sh` by default. `BELFRY_PUSH_GATEWAY_URL=` (empty)
in `belfry config` turns push off; another URL points them at another gateway.

## Limits and logs

120 sends per hour per device token, 10 000 per hour per belfry key (in memory: a restart starts
over). Logs carry no payloads, and tokens only as a 12-character SHA-256 prefix.

## Development

    uv run --frozen --extra dev pytest -q -p no:cacheprovider
    uv run --frozen --extra dev ruff check --no-cache . && uv run --frozen --extra dev ruff format --no-cache .

The tests talk to fake APNs, FCM and OAuth endpoints (respx); nothing leaves the machine.
