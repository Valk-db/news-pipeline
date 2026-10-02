"""Score alias resolution against a hand-checked gold set.

tests/data/alias_gold.json is the instrument. It holds the canonical entities a small
world is built from, and pairs of surface forms that are either the same referent or
two referents, each with the dev corpus mention counts behind it. Recall is measured
over the pairs that must merge, precision over every merge the resolver makes.

The number is asserted at 1.0 in both directions on purpose: this is a corpus with 65
hand-checked pairs, so there is nothing statistical about it and every failure names a
real surface form somebody has to read. A drop in either direction is a regression
someone can act on, not a tolerance to renegotiate.
"""

import json
from pathlib import Path

import pytest

from src.utils.ner import ENGLISH_ALIASES, EntityCanonicalizer, _normalize_text

GOLD_PATH = Path(__file__).parent / "data" / "alias_gold.json"


def _gold():
    return json.loads(GOLD_PATH.read_text(encoding="utf-8"))


def _label(side):
    return f'{side["surface"]!r} ({side["type"]})'


class TestAliasGoldSet:
    """Precision and recall on the gold set, asserted against the labels in the file."""

    @pytest.mark.asyncio
    async def test_every_gold_pair_is_labelled_the_way_it_resolves(self, db_session):
        gold = _gold()

        seeder = EntityCanonicalizer(db_session)
        await seeder.initialize()
        for entity in gold["entities"]:
            await seeder.get_or_create(entity["name"], entity["type"])
        await db_session.commit()

        true_positives = 0
        false_positives = []
        false_negatives = []
        for pair in gold["pairs"]:
            resolver = EntityCanonicalizer(db_session)
            await resolver.initialize()
            a = resolver.resolve(pair["a"]["surface"], pair["a"]["type"])
            b = resolver.resolve(pair["b"]["surface"], pair["b"]["type"])
            merged = a is not None and b is not None and a.canonical_id == b.canonical_id

            if pair["label"] == "same":
                if merged:
                    true_positives += 1
                else:
                    false_negatives.append(
                        f'{_label(pair["a"])} -> {_name(a)}, {_label(pair["b"])} -> {_name(b)}'
                    )
            elif merged:
                false_positives.append(
                    f'{_label(pair["a"])} and {_label(pair["b"])} both -> {a.canonical_name}'
                )

        should_merge = [pair for pair in gold["pairs"] if pair["label"] == "same"]
        recall = true_positives / len(should_merge)
        precision = (
            true_positives / (true_positives + len(false_positives))
            if true_positives + len(false_positives)
            else 1.0
        )

        assert not false_positives, (
            f"{len(false_positives)} merge(s) the gold set says are two referents: "
            + "; ".join(false_positives)
        )
        assert not false_negatives, (
            f"{len(false_negatives)} gold pair(s) that name one referent stayed apart: "
            + "; ".join(false_negatives)
        )
        assert (precision, recall) == (1.0, 1.0)

    @pytest.mark.asyncio
    async def test_a_bare_surname_merge_is_reported_as_a_guess(self, db_session):
        """A surname that reaches its owner does so at less than full confidence.

        The corpus says "Burnham" as often as it says "Andy Burnham", so the resolver
        has to answer from one token where the wire printed two. It is a guarded guess
        and the mention has to say so, because this is the number downstream code that
        weights corroboration is reading.

        Curated seed aliases are excluded: "Zelensky" and "Trump" are in ALIAS_SEED
        precisely so they match exactly, and a curated row is not a guess.
        """
        gold = _gold()
        seeder = EntityCanonicalizer(db_session)
        await seeder.initialize()
        for entity in gold["entities"]:
            await seeder.get_or_create(entity["name"], entity["type"])
        await db_session.commit()

        checked = 0
        for pair in gold["pairs"]:
            for side in (pair["a"], pair["b"]):
                if pair["label"] != "same" or side["type"] != "PERSON" or " " in side["surface"]:
                    continue
                if _normalize_text(side["surface"], side["type"]) in ENGLISH_ALIASES:
                    continue
                resolver = EntityCanonicalizer(db_session)
                await resolver.initialize()
                mention = resolver.resolve(side["surface"], side["type"])
                assert mention is not None, f'{side["surface"]!r} did not resolve'
                assert mention.confidence < 1.0, (
                    f'{side["surface"]!r} resolved at confidence {mention.confidence}; a surname '
                    "reached from one token is a guess, not an exact match"
                )
                checked += 1
        assert checked, "no bare-surname pair in the gold set; the file lost its cases"

    @pytest.mark.asyncio
    async def test_the_gold_set_still_describes_the_corpus(self, db_session):
        """Every pair names a type the pipeline keeps, and no two pairs are identical.

        A gold set that quietly stops matching the corpus is worse than none: it would
        keep reporting 1.0 over a set of pairs the newswire stopped printing.
        """
        gold = _gold()
        assert gold["pairs"], "no pairs"
        for entity in gold["entities"]:
            assert entity["type"] in {"PERSON", "ORG", "GPE"}
        seen = set()
        for pair in gold["pairs"]:
            assert pair["label"] in gold["labels"]
            key = (
                pair["a"]["surface"],
                pair["a"]["type"],
                pair["b"]["surface"],
                pair["b"]["type"],
            )
            assert key not in seen, f"duplicate pair: {key}"
            seen.add(key)
            attested = sum(side["dev_mentions"] for side in (pair["a"], pair["b"]))
            assert attested or "constructed" in pair["why"], (
                f"{key} is unattested and does not say why it exists"
            )


def _name(mention):
    return mention.canonical_name if mention else "nothing"
