-- Restore the id defaults on the five tables CREATE TABLE IF NOT EXISTS left behind
-- without one, so phases 1-3 can write again.
--
-- 20260923000000, 20260924000300 and 20260926000000 all declare these ids as
-- SERIAL, but every one of these five tables already existed in
-- news-pipeline-dev when those migrations ran, so IF NOT EXISTS skipped the
-- CREATE TABLE and the SERIAL (and with it the sequence) was never attached:
-- each has `id integer NOT NULL` with column_default IS NULL in
-- information_schema. No client-side default rescues them either -- all five
-- models (src/schema/models.py) declare
-- id = Column(Integer, primary_key=True, autoincrement=True), so SQLAlchemy
-- emits no id and relies on the database sequence.
--
-- The first write dies immediately: tonight's ingest stopped in log_status()
-- (src/ingestion/run.py) with 'null value in column "id" of relation
-- "status_log"', so nothing after it in the run happened -- no reporting units,
-- no stories, no gate.
--
-- This re-attaches the sequence the SERIAL was meant to create and moves each
-- sequence past any existing rows. Idempotent twice over: a table that does
-- not exist is skipped, and a table whose id already has a default (a correct
-- SERIAL or an identity column) is left untouched.
--
-- Same shape and reason as 20261002000000_entity_aliases_id_default.sql and
-- 20261002010000_story_unit_links_id_default.sql, for all five tables at once.

DO $$
DECLARE
    tbl text;
BEGIN
    FOREACH tbl IN ARRAY ARRAY[
        'status_log',
        'claim_evidence',
        'entity_edges',
        'source_reliability_snapshots',
        'story_topic_groups'
    ]
    LOOP
        IF to_regclass('public.' || tbl) IS NULL THEN
            CONTINUE;
        END IF;

        IF NOT EXISTS (
            SELECT 1
            FROM pg_attrdef d
            JOIN pg_attribute a ON a.attrelid = d.adrelid AND a.attnum = d.adnum
            WHERE d.adrelid = ('public.' || tbl)::regclass
              AND a.attname = 'id'
        ) THEN
            EXECUTE format('CREATE SEQUENCE IF NOT EXISTS public.%I_id_seq', tbl);
            EXECUTE format(
                'ALTER TABLE public.%I ALTER COLUMN id SET DEFAULT nextval(''public.%I_id_seq'')', tbl, tbl);
            -- OWNED BY so the sequence follows the column if the table is dropped.
            EXECUTE format('ALTER SEQUENCE public.%I_id_seq OWNED BY public.%I.id', tbl, tbl);
            -- Start past whatever is already there, so re-running cannot collide.
            EXECUTE format(
                'SELECT setval(''public.%I_id_seq'', GREATEST(COALESCE((SELECT MAX(id) FROM public.%I), 0), 1))',
                tbl, tbl);
        END IF;
    END LOOP;
END $$;