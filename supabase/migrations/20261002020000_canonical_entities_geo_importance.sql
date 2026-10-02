-- Record how prominent a geocoded place's match was.
--
-- canonical_entities already stores where a place is, but not how much the
-- geocoder believed the match. Without that, two places of equal granularity
-- are indistinguishable, and the tie has to be broken on the name alone: in dev
-- "Man City" (a village in Cote d'Ivoire, Nominatim importance ~0.4) sorts
-- before "Manchester City" (importance ~0.74), so a story about Manchester was
-- placed in West Africa. Nominatim publishes a prominence score with every hit
-- (0.0-1.0, near 1.0 for a country, far below for a hamlet) and this stores it
-- so the choice can be made on evidence.
--
-- Nullable, like the rest of the geolocation columns: an entity that was never
-- geocoded, or was geocoded before this column existed, has no score and simply
-- loses every tie.

ALTER TABLE canonical_entities
    ADD COLUMN IF NOT EXISTS geo_importance DOUBLE PRECISION;
