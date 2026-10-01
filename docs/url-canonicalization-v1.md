# URL canonicalization scheme u1

## What this is

`url_hash` is both the identity key and the dedup key for an article. Two
implementations of it exist in this project and they disagree, so the same
article can produce two different hashes depending on which code path wrote it.

  1. The pipeline implementation, `canonicalize_url` in
     `src/utils/trafilatura_extract.py`. This is what `src/ingestion` calls.
  2. The worker implementation, `canonicalize_url` in
     `scripts/ingest_gdelt_daily.py`. This is what writes the dev database rows.

This batch added `canonicalize_url_v1`, which is the versioned form of the
pipeline implementation and the scheme every new writer should converge on.
`canonicalize_url` is now a thin alias that calls it, so every existing caller
keeps working and no existing caller changes behaviour outside the rules listed
below. `compute_url_hash` hashes the v1 form.

The function is pure. A string goes in and a string comes out, with no network,
no database, and no clock. The full specification is the numbered rule list in
the docstring of `canonicalize_url_v1`, and `tests/test_url_canonicalization_v1.py`
pins it with 99 vectors from real feed URLs.

## Which scheme the dev database rows use today

The dev rows use the worker scheme, which is the seed scheme. The worker states
this in its own header comment and in the comment above its `canonicalize_url`,
and its `url_hash_of` is a plain sha256 over the seed style canonical URL with
no lowercasing. The rows written by the daily GDELT job and the rows written by
the original seed are therefore the same scheme as each other and a different
scheme from the pipeline.

So today, an article ingested by the pipeline and the same article ingested by
the daily worker land in `raw_articles` as two rows under two `url_hash` values,
and the unique index on `url_hash` does not catch it because the values differ.

## What was deliberately not changed

  * `raw_articles.url` was not rewritten.
  * `raw_articles.url_hash` was not rewritten, for any row.
  * The unique index on `raw_articles.url_hash` was not dropped or relaxed.
  * `scripts/ingest_gdelt_daily.py` was not edited. It belongs to the ingest
    workstream.

The reason is the transparency log. `src/verification/revisions.py` appends a
payload containing `raw_articles.url`, plus the article id, the content hashes,
and the revision number, to the signed hash chain in `src/transparency/log.py`.
A payload is hashed into a leaf hash and chained, so changing a value that a
payload carries invalidates the chain from that entry onward and breaks every
proof after it. Article lookup also resolves by `RawArticle.url`, so rewriting
that column would break the revision scan as well. The u1 identity therefore
lives in new columns beside the old ones, and the old ones stay exactly as the
log saw them.

## What was added

| Where | What |
| --- | --- |
| `src/utils/trafilatura_extract.py` | `canonicalize_url_v1`, the `canonicalize_url` alias, `compute_url_hash` pinned to the v1 form |
| `supabase/migrations/20261001000300_url_canonicalization_v1.sql` | `raw_articles.canonical_url_v1`, `raw_articles.url_hash_v1`, a non unique index, and the `url_aliases` table |
| `src/schema/models.py` | the two new columns on `RawArticle` and the `UrlAlias` model, in one delimited additive block |
| `scripts/backfill_url_hash_v1.py` | idempotent backfill with `--dry-run` and `--limit` |
| `tests/test_url_canonicalization_v1.py` | 99 canonical vectors, 21 adversarial pairs, 3 equivalence groups, 7 hash pins |
| `docs/url-canonicalization-v1.md` | this file |

`url_hash_v1` has an index but no unique constraint on purpose. The backfill is
expected to find legacy rows that are one article under u1, and that merge is a
decision for the ingest workstream rather than a database error.

`url_aliases` maps each legacy `url_hash` onto its u1 identity, so a lookup by
an old hash can still be answered and a lookup by a u1 hash can be traced back.

## Behavioral differences between the worker scheme and u1

Every row below was produced by running both implementations over the same input.
The worker column is the reference copy at
`.batch-refs/worker-ingest_gdelt_daily.REF.py`, which is byte for byte the worker
that writes the dev rows (the worker itself lives uncommitted in the main
worktree and is owned by the ingest workstream).

| Behavior | Worker scheme today | Scheme u1 | Same article, different hash |
| --- | --- | --- | --- |
| Scheme | `http` stays `http` | every `http` and `https` becomes `https` | yes, for any `http` URL |
| `www.` label | kept | stripped | yes |
| `m.` and `mobile.` labels | kept | stripped | yes |
| `amp.` host label | kept | stripped | yes |
| `amp.` label before the tld, as in `example.amp.com` | kept | stripped | yes |
| `/amp` as the last path segment | kept | stripped | yes |
| `/amp` as the first path segment below the root | kept | stripped | yes |
| `output=1`, `amp=1`, `ampmode`, `amp_js_v` markers | kept | dropped when the value is empty, 1, `amp`, or `true` | yes |
| Trailing slash | kept | dropped | yes |
| `.` and `..` path segments | kept | resolved per RFC 3986 | yes |
| Unreserved percent escapes, as in `%61` | kept encoded | decoded | yes |
| Percent escape letter case | kept as it arrived | uppercased, so `%2f` and `%2F` agree | yes |
| Protocol relative input, as in `//host/path` | passed through broken | becomes `https` | yes |
| Trailing root dot on the host | kept | dropped | yes |
| Redirector unwrapping | none | Google `/url`, Google AMP cache `/c/s/` and `/amp/s/`, FeedBurner and FeedProxy target parameters | yes |
| Query name case | kept, `ID=1` stays `ID=1` | lowercased, `ID=1` becomes `id=1` | yes, at canonical level |
| Query value case | kept | kept | no |
| Path case | kept | kept | no, at canonical level |
| Lowercasing before hashing | none, `url_hash_of` hashes the canonical string as it stands | the canonical string is lowercased, which is the old pipeline behaviour | yes, in the other direction |
| Query param sorting | sorted | sorted | no |
| Tracking names | 13 names, and the `pk_campaign` and `pk_kwd` pair | every worker name plus 32 more, and the `pk_` prefix covers the pair | no |
| Tracking prefixes | `utm_`, `piwik_`, `matomo_` | those three plus `at_`, `mtm_`, `pk_`, `hsa_`, `vero_`, `oly_`, `ns_`, `_ga_` | no |
| Fragment, userinfo, blank values, non default ports, non http schemes | already handled | handled the same way | no |

### The two opposite case defects

The two schemes are wrong about case in opposite directions, and this is the part
worth understanding before converging.

  * The worker preserves path case in the canonical form and does not lowercase
    before hashing, so `/Story` and `/story` are two different articles to it.
  * u1 preserves path case in the canonical form and then lowercases before
    hashing, so `/Story` and `/story` are one article to it. This is the old
    pipeline behaviour and it is pinned in `compute_url_hash` and in
    `test_case_only_pairs_still_collide_in_the_hash`.

Converging on u1 therefore trades one false negative for one false positive. The
false negative is rare, since few outlets serve two URLs that differ only in
path case. The false positive merges two articles that a case sensitive server
really does serve separately, which is the more damaging direction for an
archive. A later scheme, u2, should drop the global lowercasing. That change
alters every hash, so it needs its own column set and its own signed log epoch,
the same way u1 does now.

## What the ingest workstream still has to change in the worker

None of this has been done. It is listed so the ingest batch can pick it up.

1. Apply `20261001000300_url_canonicalization_v1.sql` before anything writes a
   u1 column, since the worker writes `raw_articles` rows.
2. Replace the worker `canonicalize_url` body with a call into
   `src.utils.trafilatura_extract.canonicalize_url_v1`, or port the rule list
   into the worker and pin it against the same vectors. A call into the pipeline
   module is preferred, because one implementation is the only way the test
   vectors keep meaning anything for both writers. The worker already imports
   from `src.ingestion`, so the import path is established.
3. Replace `url_hash_of` with a sha256 over `canonicalize_url_v1(url).lower()`
   encoded UTF8. Keeping the worker hash as it is today would leave the two
   writers disagreeing about the one thing that matters.
4. Make the worker write `canonical_url_v1` and `url_hash_v1` in its
   `raw_articles` insert, alongside the existing `url` and `url_hash`. The
   existing two columns stay exactly as they are, for the log.
5. Make the phase 2 pre check consult both columns. `db.existing_hashes`
   currently filters on `url_hash` only, so a second pass after the switch would
   insert everything again as new rows unless it also filters on `url_hash_v1`.
6. Extend the checkpoint and `processed_url_hashes` handling, or clear the
   checkpoint on the switch. The checkpoint stores legacy hashes, so after the
   switch every URL looks unprocessed and the run inserts duplicates.
7. Record the alias rows for new inserts, or accept that `url_aliases` covers
   only the backfilled rows until the next backfill pass.
8. Decide what happens to the legacy rows that collapse under u1. The backfill
   reports the count and prints examples, and it merges nothing. Only after that
   decision can `url_hash_v1` get its unique constraint.
9. Leave `scripts/ingest_gdelt_daily.py` out of the verification path here.
   It belongs to the ingest workstream and it was not edited by this batch.

## Known limits of u1, recorded on purpose

  * Opaque redirectors are not resolved. Bitly style short paths, FeedBurner feed
    labels, and the base64 article ids under `news.google.com` stay as the URLs
    they are, because resolving them needs a network fetch and this function does
    no network. The fetch layer already follows redirects with
    `follow_redirects=True`, so the fix is to feed the final URL back through
    u1 in the worker, not to make this function impure.
  * `rel=canonical` is out of scope. It is a fetch layer concern, since it needs
    the fetched page.
  * A bare `src` query parameter is kept, while the old pipeline list dropped
    `ref`. Both choices are judgement calls about what is tracking. The full list
    is rule 26 of the docstring, so changing a mind is a one line change with a
    vector to update.
  * An `output=1` parameter is assumed to be an AMP marker. On a site where
    `output` is content, u1 drops real data. No such site is known in the feeds,
    and the parameter list in rule 26 is where to fix it.
  * The pipeline has a Rust port, `pipeline-rs/src/utils/trafilatura_extract.rs`,
  which still carries the old rule set. It is outside the file set this batch owns and it
  was not changed. If it writes `url_hash`, it needs the same convergence work.
  * Duplicate slashes inside a path are preserved, so `/a//b` and `/a/b` stay
    distinct. Collapsing them would merge paths that some routers treat
    differently.
