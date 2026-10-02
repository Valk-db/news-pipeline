"""Named entity extraction and canonicalization for story grouping."""

import spacy
import re
from typing import List, Dict, Set, Optional, Tuple
from functools import lru_cache
from dataclasses import dataclass

# Load spaCy model (download with: python -m spacy download en_core_web_sm)
@lru_cache
def get_nlp():
    return spacy.load("en_core_web_sm")


ENTITY_LABELS = {"PERSON", "ORG", "GPE", "LOC", "EVENT", "PRODUCT"}


@dataclass
class CanonicalMention:
    """A resolved entity mention with canonical ID."""
    surface_form: str           # What was in the text
    canonical_id: str           # UUID of canonical entity
    canonical_name: str         # Preferred display name
    entity_type: str            # PERSON, ORG, GPE, etc.
    confidence: float = 1.0     # Resolution confidence (0-1)


def extract_entities(text: str, top_n: Optional[int] = 3) -> Dict[str, List[str]]:
    """
    Extract named entities from text.
    Returns dict of label -> list of entity texts (top N by frequency).
    """
    return extract_entities_top_n(text, top_n=top_n)


def extract_entities_top_n(text: str, top_n: Optional[int] = 3) -> Dict[str, List[str]]:
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

    entities_by_label: Dict[str, Dict[str, int]] = {label: {} for label in ENTITY_LABELS}

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


def get_primary_entity_set(entities: Dict[str, List[str]]) -> Set[str]:
    """
    Get a flat set of top entities across PERSON, ORG, GPE for story grouping.
    """
    primary_labels = {"PERSON", "ORG", "GPE"}
    entity_set = set()
    for label in primary_labels:
        entity_set.update(entities.get(label, []))
    return entity_set


def entity_set_jaccard(set_a: Set[str], set_b: Set[str]) -> float:
    """Jaccard similarity between two entity sets."""
    if not set_a and not set_b:
        return 1.0
    if not set_a or not set_b:
        return 0.0
    intersection = set_a & set_b
    union = set_a | set_b
    return len(intersection) / len(union)


# --- Canonicalization layer ---

# Normalization helpers
def _normalize_text(text: str, entity_type: str = "") -> str:
    """Normalize text for matching: lowercase, strip punctuation, collapse whitespace."""
    text = text.lower().strip()
    # Remove common honorifics/titles
    text = re.sub(r'\b(mr\.?|mrs\.?|ms\.?|dr\.?|prof\.?|president|prime minister|pm|secretary|minister)\s+', '', text)
    # Remove punctuation except hyphens in names
    text = re.sub(r'[^\w\s-]', '', text)
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
ALIAS_SEED: Tuple[Tuple[str, str, str], ...] = (
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
    # --- people referred to by surname or short form
    ("PERSON", "Trump", "Donald Trump"),
    ("PERSON", "Biden", "Joe Biden"),
    ("PERSON", "Pence", "Mike Pence"),
    ("PERSON", "Obama", "Barack Obama"),
    ("PERSON", "Musk", "Elon Musk"),
    ("PERSON", "Netanyahu", "Benjamin Netanyahu"),
    ("PERSON", "Putin", "Vladimir Putin"),
    ("PERSON", "Zelensky", "Volodymyr Zelensky"),
    ("PERSON", "Khamenei", "Ali Khamenei"),
    ("PERSON", "Macron", "Emmanuel Macron"),
    ("PERSON", "Merkel", "Angela Merkel"),
    ("PERSON", "Modi", "Narendra Modi"),
    ("PERSON", "Harris", "Kamala Harris"),
)


def _build_alias_indexes(rows: Tuple[Tuple[str, str, str], ...]) -> Tuple[Dict[str, str], Dict[Tuple[str, str], List[str]]]:
    """Build the forward ("TYPE:alias" -> canonical surface) and reverse
    ((entity_type, normalized canonical) -> normalized aliases) indexes."""
    forward: Dict[str, str] = {}
    reverse: Dict[Tuple[str, str], List[str]] = {}
    for entity_type, alias, canonical in rows:
        forward[_normalize_text(alias, entity_type)] = canonical
        reverse.setdefault((entity_type, _normalize_text(canonical)), []).append(
            _normalize_text(alias)
        )
    return forward, reverse


ENGLISH_ALIASES, _ALIASES_BY_CANONICAL = _build_alias_indexes(ALIAS_SEED)


def canonical_surface(surface_form: str, entity_type: str) -> str:
    """The surface form an English alias should be canonicalized onto.

    "US" -> "United States", "Netanyahu" -> "Benjamin Netanyahu", anything not in
    the seed table -> itself. Called at canonicalization time, so aliases that
    arrive before their long form still create one entity instead of two.
    """
    return ENGLISH_ALIASES.get(_normalize_text(surface_form, entity_type), surface_form)


def _generate_aliases(base_name: str, entity_type: str) -> List[str]:
    """Generate common aliases for a canonical entity."""
    aliases = set()
    normalized = _normalize_text(base_name)
    aliases.add(normalized)

    # For PERSON: add first/last name variants
    if entity_type == "PERSON":
        parts = normalized.split()
        if len(parts) >= 2:
            # "Joe Biden" -> "biden", "joe biden", "j biden"
            aliases.add(parts[-1])  # Last name only
            aliases.add(f"{parts[0][0]} {parts[-1]}")  # Initial + last
            aliases.add(f"{parts[0]} {parts[-1]}")  # First + last
        if len(parts) >= 3:
            # "President Joe Biden" -> "biden", "joe biden"
            pass

    # For ORG: add acronyms
    if entity_type == "ORG":
        words = normalized.split()
        if len(words) >= 2:
            # "European Union" -> "eu", "e u"
            acronym = ''.join(w[0] for w in words if w)
            if len(acronym) >= 2:
                aliases.add(acronym)
                aliases.add(' '.join(w[0] for w in words))
            # "Federal Bureau of Investigation" -> "fbi"
            # Also add common abbreviation patterns
            for w in words:
                if len(w) > 3:
                    aliases.add(w)

    # Curated country aliases: "United States" -> "us", "usa", "america"
    aliases.update(_ALIASES_BY_CANONICAL.get((entity_type, normalized), ()))

    return list(aliases)


class EntityCanonicalizer:
    """Resolves entity mentions to canonical entities using database-backed aliases."""

    def __init__(self, session=None):
        self.session = session
        self._cache: Dict[str, CanonicalMention] = {}  # normalized surface -> CanonicalMention
        self._initialized = False

    async def initialize(self):
        """Load all canonical entities and aliases from database."""
        if self._initialized:
            return
        if not self.session:
            return

        from sqlalchemy import select
        from sqlalchemy.orm import selectinload
        from src.schema.models import CanonicalEntity

        # Load all canonical entities with their aliases (eager load to avoid MissingGreenlet)
        stmt = select(CanonicalEntity).options(selectinload(CanonicalEntity.aliases))
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

        self._initialized = True

    def resolve(self, surface_form: str, entity_type: str) -> Optional[CanonicalMention]:
        """
        Resolve a surface form to a canonical entity.
        Returns None if no match found (caller should create new canonical entity).
        """
        if not self._initialized:
            return None

        normalized = _normalize_text(canonical_surface(surface_form, entity_type), entity_type)
        if normalized in self._cache:
            return self._cache[normalized]

        # Try fuzzy matching for common variations
        for cached_key, mention in self._cache.items():
            if mention.entity_type != entity_type:
                continue
            # Simple fuzzy: check if one contains the other
            if normalized in cached_key or cached_key in normalized:
                # Additional check: they should share significant tokens
                norm_tokens = set(normalized.split())
                cached_tokens = set(cached_key.split())
                if norm_tokens & cached_tokens:
                    return CanonicalMention(
                        surface_form=surface_form,
                        canonical_id=mention.canonical_id,
                        canonical_name=mention.canonical_name,
                        entity_type=entity_type,
                        confidence=0.8
                    )

        return None

    async def get_or_create(self, surface_form: str, entity_type: str) -> CanonicalMention:
        """
        Resolve or create a canonical entity for a surface form.
        Creates new canonical entity + aliases if not found.
        """
        # Try to resolve first
        resolved = self.resolve(surface_form, entity_type)
        if resolved:
            return resolved

        # Not found - create new canonical entity
        if not self.session:
            # Fallback: return unresolved mention with generated ID
            import uuid
            return CanonicalMention(
                surface_form=surface_form,
                canonical_id=str(uuid.uuid4()),
                canonical_name=surface_form,
                entity_type=entity_type,
                confidence=0.5
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
            entity_type=entity_type
        )
        self.session.add(canonical_entity)

        # Create initial alias (the surface form itself)
        alias = EntityAlias(
            canonical_entity_id=canonical_id,
            alias=surface_form
        )
        self.session.add(alias)

        # Generate and add common aliases
        for alias_text in _generate_aliases(canonical_name, entity_type):
            normalized_alias = _normalize_text(alias_text, entity_type)
            if normalized_alias != _normalize_text(canonical_name, entity_type):
                alias_obj = EntityAlias(
                    canonical_entity_id=canonical_id,
                    alias=alias_text
                )
                self.session.add(alias_obj)
                # Add to cache
                self._cache[normalized_alias] = CanonicalMention(
                    surface_form=alias_text,
                    canonical_id=str(canonical_id),
                    canonical_name=canonical_name,
                    entity_type=entity_type,
                    confidence=0.9
                )

        await self.session.flush()

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

        return mention


async def resolve_entities_to_canonical(
    entities: Dict[str, List[str]],
    canonicalizer: EntityCanonicalizer
) -> Tuple[Set[str], List[CanonicalMention]]:
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


def canonical_jaccard(set_a: Set[str], set_b: Set[str]) -> float:
    """Jaccard similarity on canonical entity ID sets."""
    if not set_a and not set_b:
        return 1.0
    if not set_a or not set_b:
        return 0.0
    intersection = set_a & set_b
    union = set_a | set_b
    return len(intersection) / len(union)