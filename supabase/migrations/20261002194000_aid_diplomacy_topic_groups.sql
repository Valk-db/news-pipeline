-- Add the two topic groups the GDELT attention measurement says this taxonomy
-- was missing: the diplomatic layer (COOPERATION 44.74% + VERBAL 22.60% +
-- DISAPPROVE 10.43% = 77.77% of 111,716 measured GDELT events, versus 8.54%
-- of the 1,838 pipeline articles the GDELT batch attributed) and the
-- humanitarian layer (AID, 3,687 events, the 6th largest family). Before this,
-- a story about a UN resolution, a NATO communique or a UNHCR appeal was
-- labelled Geopolitics and nothing else -- the words that name those layers had
-- no group to land in.
--
-- These are rows, not behaviour: the keyword side lives in TOPIC_KEYWORDS in
-- src/verification/topics.py, and assign_story_topic_groups() skips a match
-- whose group row is missing (it logs "Topic group ... not found in database").
-- So without this migration the new keywords would compute a match and silently
-- drop it, which is exactly the class of quiet loss this repo has been bitten
-- by. scripts/seed_topic_groups.py carries the same two names so the seed path
-- and the migration path agree.
--
-- "Diplomacy & Multilateral" nests under Geopolitics (it is that layer's
-- machinery). "Humanitarian Aid & Development" is top level: relief and
-- development work is not a subset of great-power politics, and nesting it
-- there would mis-file every famine and every displacement story.
--
-- Idempotent via ON CONFLICT DO NOTHING with no conflict target. The unique key
-- is (name, parent_group_id), and parent_group_id is NULL for the top-level
-- row, so a target of (name, parent_group_id) would not match on a re-run --
-- NULLs are distinct in a unique index. A bare DO NOTHING is also what makes
-- this safe if a group was seeded by hand under a different parent: the row
-- stays, and the hierarchy is repaired by seed_topic_groups.py rather than
-- duplicated here.

-- Idempotence is enforced by WHERE NOT EXISTS rather than by ON CONFLICT. The
-- unique key is (name, parent_group_id), and parent_group_id is NULL for the
-- top-level group, so a re-run of "ON CONFLICT DO NOTHING" would insert a
-- second 'Humanitarian Aid & Development' row rather than skip: NULLs are
-- distinct inside a unique index. Guarding each row on the name alone is what
-- actually makes this re-runnable, and it also leaves a hand-seeded row alone
-- instead of failing.

-- id and created_at are spelled out rather than left to the column defaults:
-- the schema migration declares DEFAULT gen_random_uuid() and DEFAULT NOW(),
-- but the dev database's copy of topic_groups has neither (verified: omitting
-- them fails with NOT NULL violations -- that table was created by the app's
-- create_all, which puts Python-side defaults on the columns and leaves the
-- server with none). A seed row is exactly the kind of statement that gets run
-- by hand against whichever database is in front of you, so it carries its own
-- values and works on both shapes of schema.

INSERT INTO topic_groups (id, name, parent_group_id, description, created_at)
SELECT
    gen_random_uuid(),
    'Diplomacy & Multilateral',
    (SELECT id FROM topic_groups WHERE name = 'Geopolitics' AND parent_group_id IS NULL),
    'Treaties, summits, foreign ministries and multilateral institutions (UN, EU, AU, NATO, G7/G20).',
    NOW()
WHERE NOT EXISTS (
    SELECT 1 FROM topic_groups WHERE name = 'Diplomacy & Multilateral'
);

INSERT INTO topic_groups (id, name, parent_group_id, description, created_at)
SELECT
    gen_random_uuid(),
    'Humanitarian Aid & Development',
    NULL,
    'Humanitarian response and development institutions: UNHCR, UNICEF, WFP, Red Cross/Red Crescent, World Bank, IMF.',
    NOW()
WHERE NOT EXISTS (
    SELECT 1 FROM topic_groups WHERE name = 'Humanitarian Aid & Development'
);
