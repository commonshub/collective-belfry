-- Keycast policy "belfry-event": the scope (policy:belfry-event) that Belfry's community apps
-- request. Only allowed_kinds permissions gate signing by event kind; there is no API for
-- creating global policies (GET /api/policies is read-only), hence SQL. Idempotent: re-running
-- it updates the allowed kinds of an existing permission.
-- From docs/research/keycast-spike.md section 3. Applied by scripts/bootstrap.sh on every run.
--
-- Kinds (decided 2026-10-23, docs/NEXT_STEPS.md §3h):
--   profile and base      0 profile, 1 note, 5 deletion, 7 reaction, 10002 relay list,
--                         22242 NIP-42 AUTH (the community relay requires it), 27235 NIP-98 HTTP auth
--   calendar (NIP-52)     31922 date-based event, 31923 time-based event, 31924 calendar, 31925 RSVP
--   photos (NIP-68)       20 picture; uploads use 24242 (Blossom BUD-11)
--   proposals             NIP-88 polls 1068 and responses 1018; NIP-23 long-form 30023 with
--                         NIP-22 comments 1111 (and 1 for plain replies)
--   chat (NIP-17)         13 seal (signed by the user's key), 1059 gift wrap (signed by a throwaway
--                         key, listed for clients that ask the signer anyway), 10050 DM relay list;
--                         kind 14/15 messages are never signed. nip44_encrypt/nip44_decrypt are not
--                         gated by kinds in Keycast: an allowed_kinds policy always permits them.
-- Not included: marketplace (NIP-15 / NIP-99) and links (NIP-B0 / NIP-51), still open.
INSERT INTO permissions (identifier, config, created_at, updated_at)
SELECT 'allowed_kinds_belfry_event', '{"allowed_kinds": [0, 1, 5, 7, 13, 20, 1018, 1059, 1068, 1111, 10002, 10050, 22242, 24242, 27235, 30023, 31922, 31923, 31924, 31925]}', NOW(), NOW()
WHERE NOT EXISTS (SELECT 1 FROM permissions WHERE identifier = 'allowed_kinds_belfry_event');

UPDATE permissions
SET config = '{"allowed_kinds": [0, 1, 5, 7, 13, 20, 1018, 1059, 1068, 1111, 10002, 10050, 22242, 24242, 27235, 30023, 31922, 31923, 31924, 31925]}', updated_at = NOW()
WHERE identifier = 'allowed_kinds_belfry_event'
  AND config::text <> '{"allowed_kinds": [0, 1, 5, 7, 13, 20, 1018, 1059, 1068, 1111, 10002, 10050, 22242, 24242, 27235, 30023, 31922, 31923, 31924, 31925]}';

UPDATE policies SET description = 'Profile, notes, reactions, calendar, photos, proposals, private chat and relay AUTH', updated_at = NOW()
WHERE slug = 'belfry-event' AND description <> 'Profile, notes, reactions, calendar, photos, proposals, private chat and relay AUTH';

-- Branding of the seed tenant (shown by the OAuth popup and the e-mails): Divine -> Open Source Village (run by Belfry).
UPDATE tenants SET name = 'Open Source Village', updated_at = NOW() WHERE id = 1 AND name <> 'Open Source Village';

INSERT INTO policies (name, team_id, slug, display_name, description, created_at, updated_at)
SELECT 'Belfry Event Apps', NULL, 'belfry-event', 'Belfry event apps',
       'Profile, notes, reactions, calendar, photos, proposals, private chat and relay AUTH', NOW(), NOW()
WHERE NOT EXISTS (SELECT 1 FROM policies WHERE slug = 'belfry-event');

INSERT INTO policy_permissions (policy_id, permission_id, created_at, updated_at)
SELECT p.id, perm.id, NOW(), NOW()
FROM policies p, permissions perm
WHERE p.slug = 'belfry-event'
  AND perm.identifier = 'allowed_kinds_belfry_event'
  AND NOT EXISTS (
    SELECT 1 FROM policy_permissions pp WHERE pp.policy_id = p.id AND pp.permission_id = perm.id
  );
