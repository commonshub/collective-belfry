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
--   apps (2026-10-23)     the hosted calendar (formstr nostr-calendar) and chat (Coracle) also sign:
--                         3 contact list, 6 repost, 16 generic repost, 1984 report, 1985 label,
--                         10000 mute list, 10003 bookmarks, 10063 Blossom server list, 30000 follow
--                         sets, 30002 relay sets, 30003 bookmark sets, 30015 interest sets,
--                         30078 app data (NIP-78), 31890 feeds; calendar extras 31926 busy list,
--                         31927 scheduling page, 32069 private RSVP, 32123 private calendar list,
--                         32678 private calendar event, 32680 scheduling-pages list
-- Not included: marketplace (NIP-15 / NIP-99) and links (NIP-B0 / NIP-51), still open.
INSERT INTO permissions (identifier, config, created_at, updated_at)
SELECT 'allowed_kinds_belfry_event', '{"allowed_kinds": [0, 1, 3, 5, 6, 7, 13, 16, 20, 1018, 1059, 1068, 1111, 1984, 1985, 10000, 10002, 10003, 10050, 10063, 22242, 24242, 27235, 30000, 30002, 30003, 30015, 30023, 30078, 31890, 31922, 31923, 31924, 31925, 31926, 31927, 32069, 32123, 32678, 32680]}', NOW(), NOW()
WHERE NOT EXISTS (SELECT 1 FROM permissions WHERE identifier = 'allowed_kinds_belfry_event');

UPDATE permissions
SET config = '{"allowed_kinds": [0, 1, 3, 5, 6, 7, 13, 16, 20, 1018, 1059, 1068, 1111, 1984, 1985, 10000, 10002, 10003, 10050, 10063, 22242, 24242, 27235, 30000, 30002, 30003, 30015, 30023, 30078, 31890, 31922, 31923, 31924, 31925, 31926, 31927, 32069, 32123, 32678, 32680]}', updated_at = NOW()
WHERE identifier = 'allowed_kinds_belfry_event'
  AND config::text <> '{"allowed_kinds": [0, 1, 3, 5, 6, 7, 13, 16, 20, 1018, 1059, 1068, 1111, 1984, 1985, 10000, 10002, 10003, 10050, 10063, 22242, 24242, 27235, 30000, 30002, 30003, 30015, 30023, 30078, 31890, 31922, 31923, 31924, 31925, 31926, 31927, 32069, 32123, 32678, 32680]}';

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
