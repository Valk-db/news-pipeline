"""Named entity extraction and canonicalization for story grouping."""

import spacy
import re
import unicodedata
from functools import lru_cache
from dataclasses import dataclass, replace

from src.shared.analyzer_versions import CLUSTER_VERSION, compute_input_hash

# Load spaCy model (download with: python -m spacy download en_core_web_sm)
@lru_cache
def get_nlp():
    return spacy.load("en_core_web_sm")


ENTITY_LABELS = {"PERSON", "ORG", "GPE", "LOC", "EVENT", "PRODUCT"}

# Entity types whose canonical entities carry coordinates. Only PERSON, ORG and
# GPE ever reach a canonical entity at all: resolve_entities_to_canonical is
# called with the primary labels only (src/verification/stories.py), so a LOC or
# EVENT row cannot exist to be geocoded. GPE is the one of the three that names
# a place, so it is the only type worth spending a geocoder request on.
GEOCODABLE_ENTITY_TYPES = frozenset({"GPE"})


@dataclass
class CanonicalMention:
    """A resolved entity mention with canonical ID."""
    surface_form: str           # What was in the text
    canonical_id: str           # UUID of canonical entity
    canonical_name: str         # Preferred display name
    entity_type: str            # PERSON, ORG, GPE, etc.
    confidence: float = 1.0     # Resolution confidence (0-1)


def extract_entities(text: str, top_n: int | None = 3) -> dict[str, list[str]]:
    """
    Extract named entities from text.
    Returns dict of label -> list of entity texts (top N by frequency).
    """
    return extract_entities_top_n(text, top_n=top_n)


def extract_entities_top_n(text: str, top_n: int | None = 3) -> dict[str, list[str]]:
    """
    Extract named entities from text, honoring an explicit top-N cap.

    ``top_n`` is the per-label cap (top N entities per PERSON/ORG/GPE label by
    frequency), so callers can drive it from configuration instead of hardcoding
    the value at each call site. Pass ``top_n=None`` to keep all entities per
    label. ``top_n`` must be >= 0; negative values raise ValueError.
    """
    if top_n is not None and top_n < 0:
        raise ValueError("top_n must be >= 0 (or None to keep all)")
    nlp = get_nlp()
    doc = nlp(text)

    entities_by_label: dict[str, dict[str, int]] = {label: {} for label in ENTITY_LABELS}

    for ent in doc.ents:
        if ent.label_ in ENTITY_LABELS:
            entities_by_label[ent.label_][ent.text] = entities_by_label[ent.label_].get(ent.text, 0) + 1

    # Sort by frequency, take top N per label
    result = {}
    for label, counts in entities_by_label.items():
        sorted_entities = sorted(counts.items(), key=lambda x: -x[1])
        if top_n is not None:
            sorted_entities = sorted_entities[:top_n]
        result[label] = [ent for ent, _ in sorted_entities]

    return result


def get_primary_entity_set(entities: dict[str, list[str]]) -> set[str]:
    """
    Get a flat set of top entities across PERSON, ORG, GPE for story grouping.
    """
    primary_labels = {"PERSON", "ORG", "GPE"}
    entity_set = set()
    for label in primary_labels:
        entity_set.update(entities.get(label, []))
    return entity_set


def entity_set_jaccard(set_a: set[str], set_b: set[str]) -> float:
    """Jaccard similarity between two entity sets."""
    if not set_a and not set_b:
        return 1.0
    if not set_a or not set_b:
        return 0.0
    intersection = set_a & set_b
    union = set_a | set_b
    return len(intersection) / len(union)


# --- Canonicalization layer ---

# Titles that newswire copy puts in front of a name. Every entry here is either
# already in the seed table or attested in front of a name in the corpus
# ("POTUS Donald Trump", "justice samuel alito", "prince harry", "mahatma
# gandhi"); this list is not a place to guess.
HONORIFICS: tuple[str, ...] = (
    "mr",
    "mrs",
    "ms",
    "dr",
    "prof",
    "president",
    "prime minister",
    "pm",
    "secretary",
    "minister",
    "potus",
    "justice",
    "prince",
    "princess",
    "mahatma",
)

# Longest first, so "prime minister" wins over "minister" at the same position.
_HONORIFIC_RE = re.compile(
    r"^(?:{})\.?\s+".format("|".join(sorted(map(re.escape, HONORIFICS), key=len, reverse=True))),
    re.IGNORECASE,
)
_POSSESSIVE_RE = re.compile(r"['\u2019]s$")
_LEADING_ARTICLE_RE = re.compile(r"^the\s+", re.IGNORECASE)

# "the" is part of the name of some places and organizations ("the Hague",
# "the Valley") and absent from the name of others ("the West Bank"), so it is
# matched away for the two types where the corpus overwhelmingly has it and never
# for PERSON, where no attested surface form starts with an article.
TYPES_WITH_LEADING_ARTICLE = frozenset({"GPE", "ORG"})


def _strip_honorifics(text: str) -> str:
    """Drop leading titles, however many of them are stacked ("Mr. President X")."""
    while True:
        stripped = _HONORIFIC_RE.sub("", text, count=1)
        if stripped == text:
            return text
        text = stripped


def _fold_accents(text: str) -> str:
    """Decompose accented characters and drop the combining marks.

    Accent folding is standard text normalization (the same family as the case
    folding two lines below) and it is what keeps one person from becoming two:
    the corpus spells the same name both ways, "Sébastien Lecornu" and
    "Sebastien Lecornu", "Jürgen Klopp" and "Jurgen Klopp", "Édouard Geffray"
    and "Edouard Geffray". Folded, they are one key; the display name on the
    canonical entity keeps the accents the newsroom wrote.
    """
    decomposed = unicodedata.normalize("NFKD", text)
    return "".join(ch for ch in decomposed if not unicodedata.combining(ch))


# Normalization helpers
def _normalize_text(text: str, entity_type: str = "") -> str:
    """Normalize text for matching: lowercase, strip punctuation, collapse whitespace.

    Match-only: nothing here changes what a human reads, because the display name
    lives on the canonical entity. Each step is there because the corpus contains
    the case it removes, and the surface forms it folds together are the same
    entity rather than two of them.
    """
    text = _fold_accents(text.lower().strip())
    # Titles only ever lead a name, so this is anchored: the unanchored version ate
    # "minister" out of "Foreign Minister" and would eat any name containing one of
    # these words mid-string.
    text = _strip_honorifics(text)
    # "Andy Burnham's" and "Nepal's" are the same entity as "Andy Burnham" and
    # "Nepal"; only a trailing genitive, which is the only place one appears.
    text = _POSSESSIVE_RE.sub("", text)
    if entity_type in TYPES_WITH_LEADING_ARTICLE:
        text = _LEADING_ARTICLE_RE.sub("", text)
    # Remove punctuation except hyphens in names
    text = re.sub(r"[^\w\s-]", '', text)
    # Collapse whitespace
    text = re.sub(r'\s+', ' ', text)
    normalized = text.strip()
    # Include entity type in normalized key to distinguish same surface form different types
    if entity_type:
        return f"{entity_type}:{normalized}"
    return normalized


# English alias seed: (entity_type, alias, canonical surface).
#
# Story grouping compares canonical entity *IDs*, so two spellings of one entity
# ("US" vs "United States", "Netanyahu" vs "Benjamin Netanyahu") become two
# canonical entities and split one event into several stories. English only, and
# deliberately small: these are the spellings that show up constantly in
# English-language newswire copy.
#
# EXTENSION POINT: add rows here. Keys are normalized on load, so aliases can be
# written the way a newsroom writes them ("U.S.", "the US", "UAE"). The canonical
# surface is the display name the merged entity keeps. Non-English tables belong
# in their own module alongside this one when the cross-lingual phase lands.
ALIAS_SEED: tuple[tuple[str, str, str], ...] = (
    # --- countries
    ("GPE", "US", "United States"),
    ("GPE", "U.S.", "United States"),
    ("GPE", "USA", "United States"),
    ("GPE", "America", "United States"),
    ("GPE", "the US", "United States"),
    ("GPE", "UK", "United Kingdom"),
    ("GPE", "Britain", "United Kingdom"),
    ("GPE", "Great Britain", "United Kingdom"),
    ("GPE", "the UK", "United Kingdom"),
    ("GPE", "UAE", "United Arab Emirates"),
    ("GPE", "Holland", "Netherlands"),
    # --- international organizations
    ("ORG", "EU", "European Union"),
    ("ORG", "the EU", "European Union"),
    ("ORG", "UN", "United Nations"),
    ("ORG", "the UN", "United Nations"),
    ("ORG", "FBI", "Federal Bureau of Investigation"),
    ("ORG", "CIA", "Central Intelligence Agency"),
    ("ORG", "DoD", "Department of Defense"),
    ("ORG", "Pentagon", "Department of Defense"),
    ("ORG", "SCOTUS", "Supreme Court of the United States"),
    ("ORG", "WHO", "World Health Organization"),
    # Acronyms attested in the corpus alongside their long form, so both spellings
    # are known to name one organization. An acronym whose long form the corpus has
    # never used is not listed: that would be inventing a name, and the row would
    # mint an entity nothing else ever resolves to.
    ("ORG", "NATO", "North Atlantic Treaty Organization"),
    ("ORG", "EC", "European Commission"),
    ("ORG", "IMF", "International Monetary Fund"),
    # --- people referred to by surname or short form
    ("PERSON", "Trump", "Donald Trump"),
    ("PERSON", "Biden", "Joe Biden"),
    ("PERSON", "Pence", "Mike Pence"),
    ("PERSON", "Obama", "Barack Obama"),
    ("PERSON", "Musk", "Elon Musk"),
    ("PERSON", "Netanyahu", "Benjamin Netanyahu"),
    ("PERSON", "Putin", "Vladimir Putin"),
    ("PERSON", "Zelensky", "Volodymyr Zelensky"),
    # The same newsroom spells this name both ways, and the doubled form is the
    # commoner one here: 7 mentions of "Volodymyr Zelenskyy" against 5 of
    # "Volodymyr Zelensky". Accent folding does not reach it, because no accent is
    # involved -- this is a transliteration convention, so it belongs in the table.
    ("PERSON", "Volodymyr Zelenskyy", "Volodymyr Zelensky"),
    ("PERSON", "Khamenei", "Ali Khamenei"),
    ("PERSON", "Macron", "Emmanuel Macron"),
    ("PERSON", "Merkel", "Angela Merkel"),
    ("PERSON", "Modi", "Narendra Modi"),
    ("PERSON", "Harris", "Kamala Harris"),
)


def _build_alias_index(rows: tuple[tuple[str, str, str], ...]) -> dict[str, str]:
    """Build the forward index: "TYPE:alias" -> canonical display name."""
    forward: dict[str, str] = {}
    for entity_type, alias, canonical in rows:
        forward[_normalize_text(alias, entity_type)] = canonical
    return forward


ENGLISH_ALIASES = _build_alias_index(ALIAS_SEED)


def canonical_surface(surface_form: str, entity_type: str) -> str:
    """The display name an English alias should be canonicalized onto.

    "US" -> "United States", "Netanyahu" -> "Benjamin Netanyahu", "London, London,
    City Of, United Kingdom" -> "London", "POTUS Donald Trump" -> "Donald Trump",
    anything not reducible -> itself. Called at canonicalization time, so aliases
    that arrive before their long form still create one entity instead of two.

    Two kinds of edit happen here. Dropping a title or a possessive genitive is
    cleanup: the mention keeps its own row in ``entity_aliases``, so what the text
    actually said is still recorded, and the canonical name is simply the name
    rather than the way the sentence introduced it. Dropping an administrative tail
    is a real merge -- "London, London, City Of, United Kingdom" is Nominatim's
    display string for a place the corpus also names bare as "London", and storing
    the long form under its own canonical entity is what put two pins on the map.
    """
    text = " ".join(surface_form.split())
    text = _strip_honorifics(text)
    text = _POSSESSIVE_RE.sub("", text).strip()
    seeded = ENGLISH_ALIASES.get(_normalize_text(text, entity_type))
    if seeded:
        return seeded
    if entity_type in GEOCODABLE_ENTITY_TYPES and "," in text:
        head = text.split(",", 1)[0].strip()
        if head and head != text:
            return ENGLISH_ALIASES.get(_normalize_text(head, entity_type), head)
    return text


def _person_surnames(mentions: dict[str, CanonicalMention]) -> dict[str, CanonicalMention]:
    """Surname -> the one canonical name that ends with it, for unambiguous surnames only.

    Wire copy says "Burnham said" as often as it says "Andy Burnham", so a bare
    surname has to reach the person it names. It is only allowed to where the corpus
    leaves no choice: a surname two different people answer to ("Lee" -> Bill Lee,
    Hoon Lee, Lee Jae Myung) is dropped rather than guessed at, because guessing
    files one person's news under another.

    Only multi-token, all-alphabetic names contribute a surname, which is what keeps
    the junk out: a canonical name minted from a bad spaCy span
    ("Burnham - Published Andy Burnham") or a role phrase ("Flydubai attacker")
    never ends in a bare surname token.
    """
    owners: dict[str, CanonicalMention] = {}
    for name, mention in mentions.items():
        tokens = name.split()
        if len(tokens) >= 2 and all(token.isalpha() for token in tokens):
            owners.setdefault(name, mention)
    claims: dict[str, list[CanonicalMention]] = {}
    for name, mention in owners.items():
        claims.setdefault(name.split()[-1], []).append(mention)
    return {surname: found[0] for surname, found in claims.items() if len(found) == 1}


class EntityCanonicalizer:
    """Resolves entity mentions to canonical entities using database-backed aliases.

    Requires a database session: initialize() and get_or_create() raise
    RuntimeError without one, because a session-less canonicalizer used to
    mint a fresh UUID per mention, silently splitting one entity's stories.
    """

    def __init__(self, session=None):
        self.session = session
        self._cache: dict[str, CanonicalMention] = {}  # normalized surface -> CanonicalMention
        self._surnames: dict[str, CanonicalMention] = {}  # normalized surname -> CanonicalMention
        self._initialized = False

    async def initialize(self):
        """Load all canonical entities and aliases from database."""
        if self._initialized:
            return
        if not self.session:
            # Fail loudly, not silently: the old code returned early here,
            # leaving _initialized False so resolve() returned None and
            # get_or_create() minted a fresh UUID per mention. The same
            # entity in two articles then got two IDs, canonical Jaccard
            # dropped to 0, and every reporting unit became its own story --
            # with no error anywhere. There is no legitimate session-less
            # production path (build_stories always passes one); tests that
            # need a session-free resolver use the snapshot pattern from
            # procmon-dev/eval/eval.py (populate _cache, set _initialized).
            raise RuntimeError(
                "EntityCanonicalizer.initialize() requires a database session. "
                "Without one, get_or_create() cannot persist entities and would "
                "mint a fresh UUID per mention, silently splitting stories. "
                "Pass a session, or use the eval snapshot pattern for tests."
            )

        from sqlalchemy import select
        from sqlalchemy.orm import selectinload
        from src.schema.models import CanonicalEntity

        # Load all canonical entities with their aliases (eager load to avoid MissingGreenlet).
        # Ordered so the index is a function of database content alone: two entities that
        # normalize to the same key then resolve the same way on every run.
        stmt = (
            select(CanonicalEntity)
            .options(selectinload(CanonicalEntity.aliases))
            .order_by(CanonicalEntity.entity_type, CanonicalEntity.canonical_name, CanonicalEntity.id)
        )
        result = await self.session.execute(stmt)
        canonical_entities = result.scalars().all()

        for entity in canonical_entities:
            for alias in entity.aliases:
                self._cache[_normalize_text(alias.alias, entity.entity_type)] = CanonicalMention(
                    surface_form=alias.alias,
                    canonical_id=str(entity.id),
                    canonical_name=entity.canonical_name,
                    entity_type=entity.entity_type,
                    confidence=1.0
                )
            # Also cache the canonical name itself
            self._cache[_normalize_text(entity.canonical_name, entity.entity_type)] = CanonicalMention(
                surface_form=entity.canonical_name,
                canonical_id=str(entity.id),
                canonical_name=entity.canonical_name,
                entity_type=entity.entity_type,
                confidence=1.0
            )

        self._surnames = _person_surnames(
            {
                _normalize_text(entity.canonical_name): CanonicalMention(
                    surface_form=entity.canonical_name,
                    canonical_id=str(entity.id),
                    canonical_name=entity.canonical_name,
                    entity_type=entity.entity_type,
                    confidence=1.0
                )
                for entity in canonical_entities
                if entity.entity_type == "PERSON"
            }
        )

        self._initialized = True

    def resolve(self, surface_form: str, entity_type: str) -> CanonicalMention | None:
        """
        Resolve a surface form to a canonical entity.
        Returns None if no match found (caller should create new canonical entity).
        """
        if not self._initialized:
            return None

        normalized = _normalize_text(canonical_surface(surface_form, entity_type), entity_type)
        mention = self._cache.get(normalized)
        if mention is not None:
            return mention

        if entity_type != "PERSON":
            return None

        # A bare surname, and only a bare surname. "Burnham" reaches "Andy Burnham"
        # because the corpus names exactly one person that way. "John Trump" does not
        # reach anybody: dropping a token to find a match is what let a bare "Lee" land
        # on whichever Lee happened to be cached first.
        surname = normalized.partition(":")[2]
        if " " in surname:
            return None
        owner = self._surnames.get(surname)
        if owner is None:
            return None
        return replace(owner, surface_form=surface_form, confidence=0.8)

    async def get_or_create(self, surface_form: str, entity_type: str) -> CanonicalMention:
        """
        Resolve or create a canonical entity for a surface form.

        A new entity gets one alias: the surface form that named it. Earlier revisions
        also minted every bare word of a multi-word name, which is what put "house",
        "news" and "google" into the alias table as organizations in their own right
        (and "attacker" as a person); the only aliases minted now are the ones the
        corpus attests plus the curated ALIAS_SEED.
        """
        # Try to resolve first
        resolved = self.resolve(surface_form, entity_type)
        if resolved:
            return resolved

        # Not found - create new canonical entity
        if not self.session:
            # No silent UUID minting: a fresh random ID per call makes the
            # same entity unmatchable across articles (see initialize()).
            raise RuntimeError(
                "EntityCanonicalizer.get_or_create() requires a database "
                "session to persist the new entity. Pass a session at "
                "construction time."
            )

        from src.schema.models import CanonicalEntity, EntityAlias
        import uuid

        # Create the entity under its preferred surface ("US" -> "United States"),
        # so the alias that arrived first still becomes the one canonical entity.
        canonical_name = canonical_surface(surface_form, entity_type)

        canonical_id = uuid.uuid4()
        canonical_entity = CanonicalEntity(
            id=canonical_id,
            canonical_name=canonical_name,
            entity_type=entity_type,
            # What produced this row: entity canonicalization (NER version), not the geocoder
            # that later writes coordinates onto it. The input is the (type, canonical name)
            # pair the row was minted from -- the surface forms that produced it are the
            # aliases, and each alias row hashes its own surface form.
            analyzer_version=CLUSTER_VERSION,
            input_hash=compute_input_hash(CLUSTER_VERSION, entity_type, canonical_name),
        )
        self.session.add(canonical_entity)

        # Create initial alias (the surface form itself)
        alias = EntityAlias(
            canonical_entity_id=canonical_id,
            alias=surface_form,
            analyzer_version=CLUSTER_VERSION,
            input_hash=compute_input_hash(CLUSTER_VERSION, surface_form),
        )
        self.session.add(alias)

        await self.session.flush()

        # A brand new GPE is a place the pipeline has never seen, so this is the
        # one moment geocoding costs a request instead of hitting the cache. The
        # coordinates go on the entity, not the mention: where a place is does not
        # depend on which article happened to name it first. A name the geocoder
        # cannot resolve leaves the entity unlocated, which costs the story its
        # map point but nothing else: the backfill skips an unlocated
        # entity rather than guessing.
        if entity_type in GEOCODABLE_ENTITY_TYPES:
            from src.enrichment.geocoder import get_geocoder

            place = await get_geocoder().geocode(canonical_name)
            if place is not None:
                canonical_entity.latitude = place.latitude
                canonical_entity.longitude = place.longitude
                canonical_entity.location_type = place.location_type
                canonical_entity.geo_importance = place.importance

        # Add the main entry to cache
        normalized = _normalize_text(canonical_name, entity_type)
        mention = CanonicalMention(
            surface_form=surface_form,
            canonical_id=str(canonical_id),
            canonical_name=canonical_name,
            entity_type=entity_type,
            confidence=1.0
        )
        self._cache[normalized] = mention

        # A person named in this run is a surname target for the rest of the run, so a
        # bare "Burnham" further down the same batch reaches "Andy Burnham" instead of
        # waiting for the next run to notice. Claimed only while exactly one name ends
        # with it, so the second person to share a surname retracts the claim instead
        # of inheriting it.
        if entity_type == "PERSON":
            self._claim_surname(mention)

        return mention

    def _claim_surname(self, mention: CanonicalMention) -> None:
        """Add a freshly minted name to the surname index, unless it makes a surname ambiguous."""
        tokens = _normalize_text(mention.canonical_name).split()
        if len(tokens) < 2 or not all(token.isalpha() for token in tokens):
            return
        surname = tokens[-1]
        claimed = self._surnames.get(surname)
        if claimed is None:
            self._surnames[surname] = mention
        elif claimed.canonical_id != mention.canonical_id:
            del self._surnames[surname]


async def resolve_entities_to_canonical(
    entities: dict[str, list[str]],
    canonicalizer: EntityCanonicalizer
) -> tuple[set[str], list[CanonicalMention]]:
    """
    Resolve extracted entities to canonical IDs.
    Returns (canonical_id_set, canonical_mentions) for story grouping.
    """
    canonical_ids = set()
    mentions = []

    for entity_type, surface_forms in entities.items():
        for surface in surface_forms:
            mention = await canonicalizer.get_or_create(surface, entity_type)
            canonical_ids.add(mention.canonical_id)
            mentions.append(mention)

    return canonical_ids, mentions


def canonical_jaccard(set_a: set[str], set_b: set[str]) -> float:
    """Jaccard similarity on canonical entity ID sets."""
    if not set_a and not set_b:
        return 1.0
    if not set_a or not set_b:
        return 0.0
    intersection = set_a & set_b
    union = set_a | set_b
    return len(intersection) / len(union)