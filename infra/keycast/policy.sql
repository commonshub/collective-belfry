-- Keycast policy "belfry-event": the scope (policy:belfry-event) that Belfry's community apps
-- request. Only allowed_kinds permissions gate signing by event kind; there is no API for
-- creating global policies (GET /api/policies is read-only), hence SQL. Idempotent: re-running
-- it updates the allowed kinds of an existing permission.
-- From docs/research/keycast-spike.md section 3. Applied by scripts/bootstrap.sh on every run.
--
-- Kinds: 0 profile, 1 note, 7 reaction, 30023 long-form, 22242 NIP-42 AUTH (the community
-- relay requires it for reading and writing). Extend when the Phase 2 apps are chosen.
INSERT INTO permissions (identifier, config, created_at, updated_at)
SELECT 'allowed_kinds_belfry_event', '{"allowed_kinds": [0, 1, 7, 22242, 30023]}', NOW(), NOW()
WHERE NOT EXISTS (SELECT 1 FROM permissions WHERE identifier = 'allowed_kinds_belfry_event');

UPDATE permissions
SET config = '{"allowed_kinds": [0, 1, 7, 22242, 30023]}', updated_at = NOW()
WHERE identifier = 'allowed_kinds_belfry_event'
  AND config::text <> '{"allowed_kinds": [0, 1, 7, 22242, 30023]}';

INSERT INTO policies (name, team_id, slug, display_name, description, created_at, updated_at)
SELECT 'Belfry Event Apps', NULL, 'belfry-event', 'Belfry event apps',
       'Profiles, notes, reactions, long-form posts and relay AUTH', NOW(), NOW()
WHERE NOT EXISTS (SELECT 1 FROM policies WHERE slug = 'belfry-event');

INSERT INTO policy_permissions (policy_id, permission_id, created_at, updated_at)
SELECT p.id, perm.id, NOW(), NOW()
FROM policies p, permissions perm
WHERE p.slug = 'belfry-event'
  AND perm.identifier = 'allowed_kinds_belfry_event'
  AND NOT EXISTS (
    SELECT 1 FROM policy_permissions pp WHERE pp.policy_id = p.id AND pp.permission_id = perm.id
  );
