-- Give an event a canonical identity, so N stories covering one event render one map pin.
--
-- events has one row per story per place (scripts/backfill_globe_events.py places a story at the
-- most specific located entity it names; the GDELT worker writes one row per GKG record). Nothing
-- ever asked whether two of those rows were the same occurrence, so a protest covered by four
-- papers drew four pins on the same plaza, and /api/globe/stats counted the news four times. The
-- corroboration filter then read each pin as a single-sourced event, which is the opposite of what
-- four papers carrying one event means.
--
-- canonical_event_id makes that judgement a row instead of a guess. Two events are the same
-- occurrence when they are the same type, their 25 km footprints overlap, their 24 h windows
-- overlap, and they share a canonical entity (or, where a producer records no entity ids, the same
-- place name) -- see src/verification/event_identity.py for the exact predicate and for the
-- measured distances behind the thresholds. The clustering is union-find over that predicate, so
-- the relation is transitive by construction and the answer does not depend on row order.
--
-- The design is a pointer, not a delete. A canonical row points at *itself*; a collapsed row
-- points at the row that won. Every row that was ever written is still there, still linked to its
-- own story, and still countable -- so "how many times was this reported" stays answerable after
-- the pins have collapsed, which is the question the corroboration filter is actually asking.
-- Each row's own source_count/tier1_source_count keeps meaning "what this story reported"; a
-- cluster's totals are summed at read time (cluster_source_count in
-- src/verification/event_identity.py) rather than written here, because a sum stored on the row
-- being summed cannot be recomputed twice without double-counting and the pass has to be safe to
-- re-run.
--
-- NULL is treated as "this row is its own canonical event". That is not a convenience: producers
-- outside this repo (the GDELT daily worker writes events over REST) never set the column, and
-- every pre-existing row is NULL. Reading NULL as canonical means the whole existing table is
-- correct the moment the migration lands, with no backfill and no window where rows vanish from
-- the map. A writer that wants to be explicit sets the self-pointer; a writer that does not know
-- the column exists still gets a correct, visible row.
--
-- ON DELETE SET NULL for the same reason: a collapsed row must never disappear because the row it
-- pointed at did, and "point at nothing" reads back as "its own canonical event".
--
-- Idempotent: ADD COLUMN IF NOT EXISTS and CREATE INDEX IF NOT EXISTS, so re-applying is a no-op.
-- The index is the access pattern this column exists to serve -- "every row that is not its own
-- canonical event", which is the filter every map reader applies. RLS is already enabled on events
-- and is not restated here.

ALTER TABLE public.events
    ADD COLUMN IF NOT EXISTS canonical_event_id uuid
        NULL REFERENCES public.events(id) ON DELETE SET NULL;

COMMENT ON COLUMN public.events.canonical_event_id IS
    'Canonical identity of this event. A canonical row points at itself; a collapsed duplicate '
    'points at the row that represents the cluster, so N stories covering one occurrence render '
    'one pin while every original row is retained. NULL means the row is its own canonical event '
    '(the reading for rows written by producers that predate this column). Written by '
    'src/verification/event_identity.py.';

CREATE INDEX IF NOT EXISTS ix_events_canonical_event_id
    ON public.events (canonical_event_id);
