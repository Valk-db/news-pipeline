-- Restore the id default on story_unit_links so story grouping can write.
--
-- 20260923000000 declares story_unit_links.id as SERIAL NOT NULL, but the table
-- that actually exists in news-pipeline-dev has `id integer NOT NULL` with no
-- default (column_default IS NULL in information_schema). Unlike every other id
-- column, StoryUnitLink.id has no client-side default either: the model is
-- Column(Integer, primary_key=True, autoincrement=True), so SQLAlchemy emits no
-- id and relies on the database sequence. Every link that src/verification/
-- stories.py build_stories() writes therefore fails with "null value in column
-- id", which is why story grouping has never completed in dev: no story was
-- attached to a unit, so canonical_entities stayed empty and the globe had
-- nothing to place.
--
-- This re-attaches the sequence the SERIAL was meant to create and moves the
-- sequence past any existing rows. Idempotent: a table that already has a
-- default on id (a correct SERIAL or an identity column) is left untouched.
--
-- Same shape and reason as 20261002000000_entity_aliases_id_default.sql.

DO $$
BEGIN
    IF to_regclass('public.story_unit_links') IS NULL THEN
        RETURN;
    END IF;

    IF NOT EXISTS (
        SELECT 1
        FROM pg_attrdef d
        JOIN pg_attribute a ON a.attrelid = d.adrelid AND a.attnum = d.adnum
        WHERE d.adrelid = 'public.story_unit_links'::regclass
          AND a.attname = 'id'
    ) THEN
        EXECUTE 'CREATE SEQUENCE IF NOT EXISTS public.story_unit_links_id_seq';
        EXECUTE 'ALTER TABLE public.story_unit_links ALTER COLUMN id SET DEFAULT nextval(''public.story_unit_links_id_seq'')';
        -- OWNED BY so the sequence follows the column if the table is dropped.
        EXECUTE 'ALTER SEQUENCE public.story_unit_links_id_seq OWNED BY public.story_unit_links.id';
        -- Start past whatever is already there, so re-running cannot collide.
        EXECUTE 'SELECT setval(''public.story_unit_links_id_seq'', GREATEST(COALESCE((SELECT MAX(id) FROM public.story_unit_links), 0), 1))';
    END IF;
END $$;
