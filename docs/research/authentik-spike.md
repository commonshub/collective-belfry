# Spike: pass sign-in through an OIDC source in Authentik

2026-10-22. Tested: authentik **2026.8.5** (latest stable that day; image `ghcr.io/goauthentik/server:2026.8.5`), Postgres 16, Dex v2.42.0 standing in for the OpenID4VP-to-OIDC bridge. Half-day spike for items 5 and 6 of `authentik-openid4vp.md`, "Recommended design".

## Verdict

The design holds. On a stock authentik 2026.8.5 an OpenID Connect source with `pkce: S256`, confidential client, `user_matching_mode: identifier`, `enrollment_flow: null` and the source added to the identification stage behaves exactly as required:

- An unknown subject (no link) is refused with HTTP 400 "Source is not configured for enrollment." even when an authentik user with the same email exists. No user and no link are created.
- A signed-in user can link the source from user settings ("Connected services" calls the same `/source/oauth/login/<slug>/` URL); afterwards a fresh session that presents the same subject is logged in as that user.
- Links are keyed on the bridge's `sub` claim, so `sub = member_number` works as planned.

Two corrections to the note: there is no "deny" setting for enrollment, the deny comes from leaving the enrollment flow empty; and extra claims (`npub`) reach user attributes only if a `user_write` stage is added to the source authentication flow, which then also rewrites `name`/`email`/`username` from the bridge unless the property mapping nulls them (blueprint below does that). The Source stage is Enterprise-only but not needed.

## Setup that worked

Work dir: `/tmp/claude-1000/-home-friedger--repos-github-commonshub-collective-belfry/25a6ddf2-0cae-48ea-9d4d-b384572f5352/scratchpad/spikes/authentik` (compose file, `.env`, `dex/config.yaml`, `blueprints/`, `scripts/`). Torn down with `docker compose down -v`.

- Compose: `https://goauthentik.io/docker-compose.yml` (the docs-site URL `docs.goauthentik.io/docker-compose.yml` returns 404). The upstream file in 2026.8 is **Postgres + server + worker only, no Redis**. Changes made: project name `belfry-authentik`; removed the worker's `/var/run/docker.sock` mount (only for outposts); added `./blueprints:/blueprints/custom:ro` on server and worker; `extra_hosts: ["host.docker.internal:host-gateway"]` on server and worker; a `dex` service (`ghcr.io/dexidp/dex:v2.42.0`, port 5556, `storage: memory`).
- Initial setup: not clicked through. `AUTHENTIK_BOOTSTRAP_PASSWORD`, `AUTHENTIK_BOOTSTRAP_TOKEN`, `AUTHENTIK_BOOTSTRAP_EMAIL` in `.env` make the system blueprint `/blueprints/system/bootstrap.yaml` create `akadmin` and a non-expiring API token (`identifier: authentik-bootstrap-token`) on first start; `/if/flow/initial-setup/` is then skipped. Ready after about 65 s; the token works once the worker has applied the bootstrap blueprint (about 20 s later).
- Networking: Dex issuer `http://host.docker.internal:5556/dex`. Inside the containers that name resolves via `host-gateway`; on the host the test client rewrites `host.docker.internal:5556` to `127.0.0.1:5556` before each request. Verified discovery from both sides. Alternative without rewriting: use the docker0 address (`172.17.0.1` here) as issuer host; not tested.
- Dex: static client `belfry-authentik` / secret, redirect URI `http://localhost:9000/source/oauth/callback/pass-bridge/`, `skipApprovalScreen: true`, `enablePasswordDB` with two static users (`alice@example.com`, userID `member-1001`; `mallory@example.com`, userID `member-9999`). Dex cannot add custom claims, so `npub` was not exercised end to end.
- Browser flow: python3 + `requests`, redirects followed by hand, Dex login form parsed from HTML, authentik flows driven through `/api/v3/flows/executor/<slug>/` (header `X-authentik-CSRF` from cookie `authentik_csrf`). Script: `scripts/drive_flows.py`; setup calls: `scripts/setup_api_calls.sh`. No playwright needed.

## Configuration (blueprint)

Dropped into the bind-mounted `blueprints/` directory; the worker's file watcher created the `BlueprintInstance` and applied it within seconds (`status: successful`). A later `POST /api/v3/managed/blueprints/` with the same `name` fails with a unique-constraint 500, so use one route or the other. Saving `oidc_well_known_url` made authentik fill `authorization_url`, `access_token_url`, `profile_url` and `oidc_jwks_url` from Dex's discovery document.

```yaml
version: 1
metadata:
  name: belfry-pass-bridge-source
context:
  bridge_well_known: https://bridge.belfry.example/.well-known/openid-configuration
  bridge_client_id: belfry-authentik
  bridge_client_secret: !Env BELFRY_BRIDGE_CLIENT_SECRET
entries:
  - model: authentik_sources_oauth.oauthsourcepropertymapping
    id: belfry-pass-claims
    identifiers: {name: belfry-pass-claims}
    attrs:
      expression: |
        # `info` is the bridge's userinfo payload. Null the base properties so a user_write
        # stage never rewrites identity fields; None values are dropped before writing.
        attrs = {"pass_bridge_sub": info.get("sub")}
        if info.get("npub"):
            attrs["npub"] = info["npub"]
        return {"username": None, "email": None, "name": None, "attributes": attrs}
  - model: authentik_sources_oauth.oauthsource
    id: pass-bridge
    identifiers: {slug: pass-bridge}
    attrs:
      name: Village pass (EUDI wallet)
      enabled: true
      promoted: true
      provider_type: openidconnect
      consumer_key: !Context bridge_client_id
      consumer_secret: !Context bridge_client_secret
      oidc_well_known_url: !Context bridge_well_known
      pkce: S256                               # default is "none"; choices none / plain / S256
      authorization_code_auth_method: basic_auth   # or post_body
      user_matching_mode: identifier
      group_matching_mode: identifier
      enrollment_flow: null                    # this is the "deny"
      authentication_flow: !Find [authentik_flows.flow, [slug, default-source-authentication]]
      user_property_mappings: [!KeyOf belfry-pass-claims]
  - model: authentik_stages_identification.identificationstage
    identifiers: {name: default-authentication-identification}
    attrs:
      sources: [!KeyOf pass-bridge]
  # Optional, only if claims must land in user attributes on each login (see finding 5).
  - model: authentik_stages_user_write.userwritestage
    id: belfry-source-auth-write
    identifiers: {name: belfry-source-auth-write}
    attrs: {user_creation_mode: never_create, create_users_as_inactive: false}
  - model: authentik_flows.flowstagebinding
    identifiers:
      target: !Find [authentik_flows.flow, [slug, default-source-authentication]]
      stage: !KeyOf belfry-source-auth-write
      order: -10
```

Tested version used Dex's URL and a literal secret in `context`; the `!Env` form above is the usual blueprint idiom and was not run.

## Login flow results

Test user `alice` created through the API (`POST /api/v3/core/users/`, `set_password/`), email identical to Dex's `alice@example.com`. Every run showed `code_challenge=...&code_challenge_method=S256` on Dex's authorize URL.

| Scenario | Result | Evidence |
| --- | --- | --- |
| A. Fresh session, Dex user alice, authentik user alice exists but is not linked | Denied: callback returns 400 "Source is not configured for enrollment." | No `UserOAuthSourceConnection`, user count unchanged, session anonymous |
| B. alice logs in with password (passkeys not driveable from curl), then opens `/source/oauth/login/pass-bridge/` (the `configure_url` that user settings > Connected services uses) | Linked: callback 302 to `/if/user/#/settings;page-sources` | Connection `(user=5, identifier=CgttZW1iZXItMTAwMRIFbG9jYWw)` = Dex's `sub`; events `model_created`, `source_linked` |
| C. Fresh session, Dex user alice | Logged in as alice: callback 302 to `/if/flow/default-source-authentication/`, flow ends with redirect to `/if/user/`, `/api/v3/core/users/me/` returns alice | Event `login` with `source` set |
| D. Fresh session, Dex user mallory (no authentik account) | Denied, same 400 as A | No user created |
| E. alice deletes her own link (`DELETE /api/v3/sources/user_connections/oauth/1/` with her session) | 204, link gone; the next source login would be denied again | |

## Findings

1. **Matching semantics (code: `authentik/core/sources/matcher.py`, `authentik/core/sources/flow_manager.py`).** With `user_matching_mode: identifier` the matcher looks only for an existing connection `(source, identifier)`. Found → `AUTH`. Not found → `ENROLL`, never email or username lookup. `handle_enroll` returns `bad_request_message("Source is not configured for enrollment.")` when `enrollment_flow` is empty. If the request is already authenticated, `get_action` always returns `LINK` (or `AUTH` if the identifier is already linked to someone, which would then log the session in as that other user; it does not re-link). The identifier for the OIDC type is `info["sub"]` (fallback `id`), read from the userinfo endpoint (`profile_url`).
2. **PKCE is per source**: field `pkce`, choices `none`, `plain`, `S256`, default `none`. `authorization_code_auth_method` chooses `basic_auth` (default) or `post_body` for the confidential client.
3. **Source stage is Enterprise-only.** Code is under `authentik/enterprise/stages/source/`; `POST /api/v3/stages/source/` on the unlicensed instance returns 400 `"Enterprise is required to create/update this object."`. Not needed for this design (it is for "authenticate via source in the middle of another flow").
4. **Linking is open to any authenticated session.** The redirect and callback views only check `source.enabled`. Policies bound to the source are evaluated in `GET /api/v3/sources/all/user_settings/` (what the Connected services page lists) and nowhere on the login/callback path, so they hide the button but do not block a direct call to `/source/oauth/login/<slug>/`. Authentik cannot require that the linking session was passkey-authenticated. (Policy gating of the listing was read in code, not tested.)
5. **Extra claims into attributes.** The source property mapping (model `authentik_sources_oauth.oauthsourcepropertymapping`, expression gets `info`) is evaluated on every source login and merged into base properties `username` (from `nickname`/`preferred_username`), `email`, `name`. But nothing is written unless a `user_write` stage runs: the default `default-source-authentication` flow has only `user_login`, and after scenario C alice's attributes were `{}`. With the `user_write` stage (`never_create`, order -10) added, `attributes.pass_bridge_sub` was written, and `name` was overwritten by Dex's `name` claim ("alice"); `username` survived, but I did not check whether Dex sent `preferred_username`, so treat username as at risk too. Returning `{"username": None, "email": None, "name": None, ...}` from the mapping stops the overwrite (`delete_none_values` drops them) while attributes are still written. The stage also adds `attributes["goauthentik.io/user/sources"]`. Unverified: `npub` end to end (Dex has no custom claims); the same path writes any key, so it should work.
6. **Pre-linking through the API looks possible.** `UserOAuthSourceConnectionViewSet` extends `ModelViewSet`, so `POST /api/v3/sources/user_connections/oauth/` with `user`, `source`, `identifier` should create the link (code read, not exercised). If the bridge's `sub` is `member_number`, the Odoo webhook could create the link at user creation and the manual "link once" step disappears. Worth testing next.
7. **Blueprint route works live.** A YAML file dropped into the bind-mounted `/blueprints/custom` is applied by the worker within seconds and re-applied on change. `!Find`, `!KeyOf`, `!Context` behaved as documented.
8. **Login page**: the identification challenge listed `Village pass (EUDI wallet)` with `challenge.to = /source/oauth/login/pass-bridge/` once the source was in the stage's `sources`; `promoted: true` renders it as a full button (UI not viewed).

## Contradictions with the research note

- "Enrollment flow: deny" is not a setting. The equivalent is `enrollment_flow: null`; the user sees a plain 400 page, not the access-denied flow screen. If a nicer message is wanted, use an enrollment flow whose first stage is a Deny stage (not tested).
- "Users link the pass once while signed in with a passkey": linking works from any authenticated session; the passkey requirement is a Belfry policy, not something authentik enforces (finding 4).
- "`npub` as extra claim" into authentik needs the extra `user_write` stage plus a mapping that nulls base properties (finding 5). Alternatively keep `npub` out of authentik and let Odoo be the source of that mapping.
- Not a contradiction but a correction for the ops side: the upstream compose no longer includes Redis.

## Open questions

- Will the SUNET vc verifier (or whichever bridge) expose a userinfo endpoint? Authentik's OIDC source type reads `profile_url` (filled from discovery `userinfo_endpoint`) and only falls back to decoding the id_token with the source JWKS if that fails (read in `authentik/sources/oauth/types/oidc.py`, fallback not exercised). If the bridge only issues id_tokens, verify the fallback path.
- Pre-linking via API (finding 6) and whether an API-created connection survives `update_user_connection` on the first login.
- Deny stage as enrollment flow for a friendlier refusal page.
- Whether `username` is overwritten when the bridge sends `preferred_username` (expected yes without the null mapping; not tested).
- Behaviour when a linked identifier is presented while another user's session is active: code says the session is logged in as the linked user, not re-linked; not tested.
