-- Give entity_edges the business-key UNIQUE constraint it never had.
--
-- entity_edges had only its primary key. The business key of an edge is the
-- 5-tuple (subject_type, subject_id, predicate, object_type, object_id): the
-- same typed relation between the same two things is the same edge, whatever
-- confidence or source_unit_id it was recorded with. Without the constraint
-- the table could hold the same edge many times over.
--
-- Both writers are check-then-insert and therefore idempotent only against
-- themselves, not against each other:
--   - src/verification/narrative.py link_narrative_arcs() SELECTs the 5-tuple
--     and inserts when nothing comes back (story-to-story SAME_EVENT_AS /
--     PART_OF_NARRATIVE edges);
--   - src/ingestion/rss_evidence.py link_to_gdelt_radar() does the same for
--     article/article SAME_EVENT_AS edges.
-- A second run, or two runs at once, can have both pass the SELECT and both
-- insert. The unique constraint is what makes the outcome correct anyway, and
-- it is the last line of defence behind the check rather than a replacement
-- for it: the check is what keeps the writers from raising at all.
--
-- ADD CONSTRAINT fails if duplicates are already present, so the dedupe has to
-- be part of this migration rather than a one-time manual step -- otherwise a
-- re-apply against a table that has picked up a duplicate would fail, and an
-- idempotent migration that can fail is not idempotent. Dedupe keeps the
-- earliest row (min id) of each 5-tuple group, so the surviving row is the one
-- that was written first, with the confidence and source_unit_id recorded at
-- the time.
--
-- Idempotent: a table that does not exist is skipped, and a table that
-- already carries uq_entity_edge is left entirely alone (no dedupe either --
-- the constraint makes duplicates impossible, so the DELETE would be a
-- guaranteed no-op that needlessly locks the table).
--
-- dev was checked before this ran: entity_edges held 0 rows, so there were 0
-- duplicate 5-tuple groups to remove and the DELETE is a no-op there today.
-- The dedupe is kept because the next deployment to have data might not be so
-- lucky.
--
-- Same shape and reason as 20261002030000_missing_id_defaults.sql, which is
-- where the missing sibling constraint was noticed and flagged rather than
-- silently widened.

DO $$
DECLARE
    tbl     constant text := 'entity_edges';
    con     constant text := 'uq_entity_edge';
    removed integer;
BEGIN
    IF to_regclass('public.' || tbl) IS NULL THEN
        RETURN;  -- table not created yet; the later schema migration will carry it
    END IF;

    IF EXISTS (
        SELECT 1
        FROM pg_constraint
        WHERE conname = con
          AND conrelid = ('public.' || tbl)::regclass
    ) THEN
        RETURN;  -- already applied
    END IF;

    -- Collapse existing duplicates first, keeping min(id) per 5-tuple. The
    -- window function picks the earliest row as rn = 1, so everything below it
    -- is a later copy of an edge that is already recorded.
    EXECUTE format(
        'DELETE FROM public.%I e
         USING (
             SELECT id FROM (
                 SELECT id,
                        row_number() OVER (
                            PARTITION BY subject_type, subject_id, predicate, object_type, object_id
                            ORDER BY id
                        ) AS rn
                 FROM public.%I
             ) ranked
             WHERE ranked.rn > 1
         ) doomed
         WHERE e.id = doomed.id',
        tbl, tbl
    );
    GET DIAGNOSTICS removed = ROW_COUNT;
    IF removed > 0 THEN
        RAISE NOTICE 'entity_edges: removed % duplicate 5-tuple row(s) before adding %', removed, con;
    END IF;

    EXECUTE format(
        'ALTER TABLE public.%I ADD CONSTRAINT %I UNIQUE (subject_type, subject_id, predicate, object_type, object_id)',
        tbl, con
    );
    RAISE NOTICE 'entity_edges: added %', con;
END $$;
