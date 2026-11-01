-- Keycast policy "belfry-event": the scope (policy:belfry-event) that Belfry's community apps
-- request. Only allowed_kinds permissions gate signing by event kind; there is no API for
-- creating global policies (GET /api/policies is read-only), hence SQL. Idempotent: re-running
-- it updates the config of an existing permission.
-- From docs/research/keycast-spike.md section 3. Applied by scripts/bootstrap.sh on every run.
--
-- Kinds: any (2026-11-01, docs/NEXT_STEPS.md §3h). The permission has no "allowed_kinds" list,
-- which Keycast reads as "all kinds" (AllowedKinds::can_sign: None => true), on the HTTP RPC and
-- on NIP-46 alike. Until then it listed the kinds of Belfry's own apps, so every new app or
-- protocol (tasklist.sh tasks 2100-2103, zaps, links, ...) needed a change here first. What a
-- key may do is decided by the relays and the file server (active pass, NIP-29 group roles),
-- not by the signer. nip44_encrypt/nip44_decrypt were never gated by kinds.
INSERT INTO permissions (identifier, config, created_at, updated_at)
SELECT 'allowed_kinds_belfry_event', '{"allowed_kinds": null}', NOW(), NOW()
WHERE NOT EXISTS (SELECT 1 FROM permissions WHERE identifier = 'allowed_kinds_belfry_event');

UPDATE permissions
SET config = '{"allowed_kinds": null}', updated_at = NOW()
WHERE identifier = 'allowed_kinds_belfry_event'
  AND config::text <> '{"allowed_kinds": null}';

UPDATE policies SET description = 'Sign any event for you in the village apps; private chat and relay AUTH', updated_at = NOW()
WHERE slug = 'belfry-event' AND description <> 'Sign any event for you in the village apps; private chat and relay AUTH';

-- Branding of the seed tenant (shown by the OAuth popup and the e-mails): Divine -> Open Source Village (run by Belfry).
-- Keycast serves the tenant of the Host header (KEYCAST_TENANT_DOMAIN, psql variable :tenant from
-- bootstrap), not the seed tenant 1; rename both.
UPDATE tenants SET name = 'Open Source Village', updated_at = NOW()
WHERE (id = 1 OR domain = :'tenant') AND name <> 'Open Source Village';

INSERT INTO policies (name, team_id, slug, display_name, description, created_at, updated_at)
SELECT 'Belfry Event Apps', NULL, 'belfry-event', 'Belfry event apps',
       'Sign any event for you in the village apps; private chat and relay AUTH', NOW(), NOW()
WHERE NOT EXISTS (SELECT 1 FROM policies WHERE slug = 'belfry-event');

INSERT INTO policy_permissions (policy_id, permission_id, created_at, updated_at)
SELECT p.id, perm.id, NOW(), NOW()
FROM policies p, permissions perm
WHERE p.slug = 'belfry-event'
  AND perm.identifier = 'allowed_kinds_belfry_event'
  AND NOT EXISTS (
    SELECT 1 FROM policy_permissions pp WHERE pp.policy_id = p.id AND pp.permission_id = perm.id
  );
