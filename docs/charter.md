# The charter

A short list of agreements every person ticks for themselves (iOS SPEC 19.32, 2026-11-16). It
is the to-do list of the NIP-29 group `charter` on the groups relay, so no new kind is needed.
Code: `services/onboarding/belfry_onboarding/charter.py` (its docstring is the reference);
tested against the apps' vector `tests/data/charter.json` (from belfry-ios `vectors/`).

## Wire format

- **Agreement**: kind 2100, tags `["t", "d93ecde52eff27c7"]` (sha256hex("osv-village:charter")
  [:16]), `["h", "charter"]`, `["each", "1"]`, `["order", "<int>"]`, optional
  `["about", "<line ≤ 200>"]`; content = title (≤ 300). Only a 2100 by a belfry admin counts.
- **Change**: 2101 `["e", id] ["action", "edit"]` + the same `t` / `h` / `each`, by a belfry
  admin: content = the full title, `about` (missing: emptied), `order` (missing: kept). A changed
  title or explanation makes that edit's `created_at` the agreement's **text time**.
  **Removal**: 2101 `action` `delete` by a belfry admin.
- **A person's tick**: their own 2101 `done` / `undone`; the latest per author wins; agreed when
  it is `done` and `created_at` ≥ the text time (else stale: asked again).
- **Order**: `order` ascending (none last), then the 2100's `created_at`, then id.

## For people who can't read the group

| Call | Answer |
|---|---|
| `GET https://belfry.<domain>/api/v1/charter` (no auth) | `{"agreements": [{"id", "title", "about", "order" (null = none), "added", "text_at"}]}` in order; 503 `unavailable` when the groups relay can't be read. `Cache-Control: public, max-age=60`; the server caches 60 s. |
| `/.well-known/belfry.json` | `"charter": true` (at least one agreement) / `false`; left out while the relay can't say. It never waits more than 1.5 s for it, and not again for a minute after a failure. |

## Ticks signed by the belfry

Someone who ticked agreements before they were in (a guest's request in the app,
docs/membership.md; the web form `/apply`) gets, once they are a member, one 2101 `done` per
ticked agreement that still exists and whose text time is not newer than their request,
**signed as them through the signer** (Keycast, `community.member_signer`) and published on the
groups relay in `charter`. A member who holds their own key (self custody) gets none: the
signer can't sign for them; their app asks them. The ids published are kept on the application
(`agreed_published`), so it happens once.

## Open

- zooid doesn't let guests or applicants read `charter` yet (belfry-ios SERVER-NEEDS 12 (1));
  the public read above stands in for it.
- Not checked against a real zooid: whether a member who is not in the `charter` group's member
  list may publish a 2101 there (the group is `open`).
