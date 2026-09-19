"""Tests for MinHash utilities."""

import pytest
from src.utils.minhash_utils import (
    shingle_text,
    tokens_to_minhash,
    minhash_to_json,
    minhash_from_json,
    jaccard_from_minhash,
    containment_from_jaccard,
    compute_containment,
    cluster_articles_by_containment,
)


class TestShingleText:
    def test_shingle_short_text(self):
        """Text shorter than k returns single shingle."""
        text = "hello world"
        shingles = shingle_text(text, k=5)
        assert shingles == {"hello world"}

    def test_shingle_normal(self):
        """Normal text generates k-shingles."""
        text = "the quick brown fox jumps"
        shingles = shingle_text(text, k=3)
        expected = {"the quick brown", "quick brown fox", "brown fox jumps"}
        assert shingles == expected

    def test_shingle_case_insensitive(self):
        """Shingles are lowercase."""
        text = "Hello World Test"
        shingles = shingle_text(text, k=2)
        assert all(s.islower() for s in shingles)


class TestTokensToMinHash:
    def test_basic_minhash(self):
        tokens = {"hello", "world", "test"}
        m = tokens_to_minhash(tokens, num_perm=128)
        assert m.num_perm == 128
        assert len(m.hashvalues) == 128

    def test_empty_tokens(self):
        tokens = set()
        m = tokens_to_minhash(tokens, num_perm=64)
        assert m.num_perm == 64


class TestMinHashSerialization:
    def test_roundtrip(self):
        tokens = {"apple", "banana", "cherry"}
        m1 = tokens_to_minhash(tokens, num_perm=64)
        json_str = minhash_to_json(m1)
        m2 = minhash_from_json(json_str)
        assert m1.num_perm == m2.num_perm
        assert m1.hashvalues.tolist() == m2.hashvalues.tolist()

    def test_jaccard_preserved(self):
        tokens_a = {"a", "b", "c", "d"}
        tokens_b = {"c", "d", "e", "f"}
        m1 = tokens_to_minhash(tokens_a, num_perm=256)
        m2 = tokens_to_minhash(tokens_b, num_perm=256)
        json1 = minhash_to_json(m1)
        json2 = minhash_to_json(m2)
        m1_restored = minhash_from_json(json1)
        m2_restored = minhash_from_json(json2)
        jaccard_original = m1.jaccard(m2)
        jaccard_restored = m1_restored.jaccard(m2_restored)
        assert abs(jaccard_original - jaccard_restored) < 0.01


class TestContainmentFromJaccard:
    def test_zero_jaccard(self):
        assert containment_from_jaccard(0.0, 10, 10) == 0.0

    def test_one_jaccard(self):
        assert containment_from_jaccard(1.0, 10, 10) == 1.0

    def test_symmetric(self):
        # Jaccard = 0.5, |A|=10, |B|=20
        # intersection = 0.5 * 30 / 1.5 = 10
        # containment = 10 / min(10, 20) = 1.0
        result = containment_from_jaccard(0.5, 10, 20)
        assert abs(result - 1.0) < 0.01

    def test_asymmetric_sets(self):
        # A=100 tokens, B=10 tokens, Jaccard=0.1
        # intersection = 0.1 * 110 / 1.1 = 10
        # containment = 10 / min(100, 10) = 1.0
        result = containment_from_jaccard(0.1, 100, 10)
        assert abs(result - 1.0) < 0.01


class TestComputeContainment:
    def test_identical_texts(self):
        tokens = shingle_text("The quick brown fox jumps over the lazy dog")
        containment = compute_containment(tokens, tokens)
        assert containment > 0.99

    def test_completely_different(self):
        tokens_a = shingle_text("The quick brown fox")
        tokens_b = shingle_text("Completely different text here")
        containment = compute_containment(tokens_a, tokens_b)
        assert containment < 0.1

    def test_subset(self):
        tokens_a = shingle_text("The quick brown fox jumps over the lazy dog and runs away")
        tokens_b = shingle_text("The quick brown fox jumps")
        containment = compute_containment(tokens_a, tokens_b)
        # B is subset of A, so containment should be high
        assert containment > 0.8


class TestClusterArticlesByContainment:
    def test_no_duplicates(self):
        articles = [
            ("1", shingle_text("Article one about politics")),
            ("2", shingle_text("Article two about sports")),
            ("3", shingle_text("Article three about tech")),
        ]
        clusters = cluster_articles_by_containment(articles, threshold=0.9)
        assert len(clusters) == 3

    def test_exact_duplicates(self):
        articles = [
            ("1", shingle_text("Exact same article content here")),
            ("2", shingle_text("Exact same article content here")),
            ("3", shingle_text("Different article entirely")),
        ]
        clusters = cluster_articles_by_containment(articles, threshold=0.9)
        assert len(clusters) == 2
        # Find cluster with 2 items
        large_cluster = next(c for c in clusters if len(c) == 2)
        assert {"1", "2"} == set(large_cluster)

    def test_near_duplicates(self):
        articles = [
            ("1", shingle_text("The president announced new policy today in washington")),
            ("2", shingle_text("The president announced new policy today in washington dc")),
            ("3", shingle_text("Stock market rises on tech earnings")),
        ]
        clusters = cluster_articles_by_containment(articles, threshold=0.85)
        assert len(clusters) == 2

    def test_empty_list(self):
        clusters = cluster_articles_by_containment([], threshold=0.9)
        assert clusters == []

    def test_single_article(self):
        articles = [("1", shingle_text("Single article"))]
        clusters = cluster_articles_by_containment(articles, threshold=0.9)
        assert len(clusters) == 1
        assert len(clusters[0]) == 1