-- Raw Articles Terminal State Migration
-- Records where an article ended up, so a lost article is distinguishable from a
-- deliberately dropped one. Added on the procmon branch procmon/batch-trust.

-- Documented values, a single string so reading it needs no join:
--   pending                      row exists, not yet through dedupe, cluster, or geocode
--   dropped:<reason>             deliberately discarded, e.g. dropped:parse_failed
--   duplicate_of:<uuid>          deduped into the article named by the uuid
--   unit:<uuid>                  member of the reporting unit named by the uuid
--   story:<uuid>                 reached the story named by the uuid
--   candidate                    surfaced for curation, no story yet
-- Existing rows are pending by definition, so the constant default backfills them without
-- a table rewrite on PostgreSQL 11 and later.
ALTER TABLE raw_articles
    ADD COLUMN IF NOT EXISTS terminal_state TEXT NOT NULL DEFAULT 'pending';

CREATE INDEX IF NOT EXISTS ix_raw_articles_terminal_state ON raw_articles(terminal_state);
