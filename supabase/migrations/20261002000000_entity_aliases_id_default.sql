-- Restore the id default on entity_aliases so the canonicalizer can write.
--
-- 20260924000300 declares entity_aliases.id as SERIAL PRIMARY KEY, but the
-- table that actually exists in news-pipeline-dev has `id integer NOT NULL`
-- with no default (column_default IS NULL in information_schema). Every
-- EntityAlias insert from src/utils/ner.py get_or_create() therefore fails with
-- "null value in column id", so canonical_entities and entity_aliases have both
-- stayed empty in dev and the entity-canonicalization path has never run.
--
-- This re-attaches the sequence the SERIAL was meant to create and moves the
-- sequence past any existing rows. Idempotent: a table that already has a
-- default on id (a correct SERIAL or an identity column) is left untouched.

DO $$
BEGIN
    IF to_regclass('public.entity_aliases') IS NULL THEN
        RETURN;
    END IF;

    IF NOT EXISTS (
        SELECT 1
        FROM pg_attrdef d
        JOIN pg_attribute a ON a.attrelid = d.adrelid AND a.attnum = d.adnum
        WHERE d.adrelid = 'public.entity_aliases'::regclass
          AND a.attname = 'id'
    ) THEN
        EXECUTE 'CREATE SEQUENCE IF NOT EXISTS public.entity_aliases_id_seq';
        EXECUTE 'ALTER TABLE public.entity_aliases ALTER COLUMN id SET DEFAULT nextval(''public.entity_aliases_id_seq'')';
        -- OWNED BY so the sequence follows the column if the table is dropped.
        EXECUTE 'ALTER SEQUENCE public.entity_aliases_id_seq OWNED BY public.entity_aliases.id';
        -- Start past whatever is already there, so re-running cannot collide.
        EXECUTE 'SELECT setval(''public.entity_aliases_id_seq'', GREATEST(COALESCE((SELECT MAX(id) FROM public.entity_aliases), 0), 1))';
    END IF;
END $$;