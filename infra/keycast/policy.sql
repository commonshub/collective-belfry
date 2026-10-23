-- Keycast policy "belfry-event": the scope (policy:belfry-event) that Belfry's community apps
-- request. Only allowed_kinds permissions gate signing by event kind; there is no API for
-- creating global policies (GET /api/policies is read-only), hence SQL. Idempotent: re-running
-- it updates the allowed kinds of an existing permission.
-- From docs/research/keycast-spike.md section 3. Applied by scripts/bootstrap.sh on every run.
--
-- Kinds: 0 profile, 1 note, 5 deletion, 7 reaction, 30023 long-form, 22242 NIP-42 AUTH (the
-- community relay requires it for reading and writing), 24242 Blossom upload/delete
-- authorization (BUD-11), 27235 NIP-98 HTTP auth. The kinds of the community apps themselves
-- (chat, calendar, proposals, marketplace, photos, links) are a pending decision, see
-- docs/NEXT_STEPS.md "Policy kinds".
INSERT INTO permissions (identifier, config, created_at, updated_at)
SELECT 'allowed_kinds_belfry_event', '{"allowed_kinds": [0, 1, 5, 7, 22242, 24242, 27235, 30023]}', NOW(), NOW()
WHERE NOT EXISTS (SELECT 1 FROM permissions WHERE identifier = 'allowed_kinds_belfry_event');

UPDATE permissions
SET config = '{"allowed_kinds": [0, 1, 5, 7, 22242, 24242, 27235, 30023]}', updated_at = NOW()
WHERE identifier = 'allowed_kinds_belfry_event'
  AND config::text <> '{"allowed_kinds": [0, 1, 5, 7, 22242, 24242, 27235, 30023]}';

-- Branding of the seed tenant (shown by the OAuth popup and the e-mails): Divine -> Belfry.
UPDATE tenants SET name = 'Belfry', updated_at = NOW() WHERE id = 1 AND name <> 'Belfry';

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
