-- Keycast policy "belfry-event": the scope (policy:belfry-event) that Belfry's community apps
-- request. Only allowed_kinds permissions gate signing by event kind; there is no API for
-- creating global policies (GET /api/policies is read-only), hence SQL. Idempotent.
-- From docs/research/keycast-spike.md section 3. Applied by scripts/bootstrap.sh.
INSERT INTO permissions (identifier, config, created_at, updated_at)
SELECT 'allowed_kinds_belfry_event', '{"allowed_kinds": [1, 7, 30023]}', NOW(), NOW()
WHERE NOT EXISTS (SELECT 1 FROM permissions WHERE identifier = 'allowed_kinds_belfry_event');

INSERT INTO policies (name, team_id, slug, display_name, description, created_at, updated_at)
SELECT 'Belfry Event Apps', NULL, 'belfry-event', 'Belfry event apps',
       'Notes, reactions and long-form posts only', NOW(), NOW()
WHERE NOT EXISTS (SELECT 1 FROM policies WHERE slug = 'belfry-event');

INSERT INTO policy_permissions (policy_id, permission_id, created_at, updated_at)
SELECT p.id, perm.id, NOW(), NOW()
FROM policies p, permissions perm
WHERE p.slug = 'belfry-event'
  AND perm.identifier = 'allowed_kinds_belfry_event'
  AND NOT EXISTS (
    SELECT 1 FROM policy_permissions pp WHERE pp.policy_id = p.id AND pp.permission_id = perm.id
  );
