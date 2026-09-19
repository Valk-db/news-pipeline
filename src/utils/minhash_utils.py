"""MinHash utilities for near-duplicate detection using algebraic containment."""

from datasketch import MinHash
from typing import List, Set, Tuple
import json


def tokens_to_minhash(tokens: Set[str], num_perm: int = 128) -> MinHash:
    """Create a MinHash from a set of tokens."""
    m = MinHash(num_perm=num_perm)
    for token in tokens:
        m.update(token.encode("utf-8"))
    return m


def minhash_to_json(m: MinHash) -> str:
    """Serialize MinHash to JSON string for storage."""
    return json.dumps({"hashvalues": m.hashvalues.tolist(), "num_perm": m.num_perm})


def minhash_from_json(data: str) -> MinHash:
    """Deserialize MinHash from JSON string."""
    import numpy as np
    obj = json.loads(data)
    m = MinHash(num_perm=obj["num_perm"])
    m.hashvalues = np.array(obj["hashvalues"], dtype=np.uint64)
    return m


def jaccard_from_minhash(m1: MinHash, m2: MinHash) -> float:
    """Estimate Jaccard similarity from two MinHash objects."""
    return m1.jaccard(m2)


def containment_from_jaccard(
    jaccard: float,
    size_a: int,
    size_b: int
) -> float:
    """
    Compute symmetric containment from Jaccard and set sizes.

    Jaccard = |A∩B| / (|A| + |B| - |A∩B|)
    => |A∩B| = J * (|A| + |B|) / (1 + J)

    Containment = |A∩B| / min(|A|, |B|)
    """
    if jaccard <= 0:
        return 0.0
    if jaccard >= 1:
        return 1.0

    intersection = jaccard * (size_a + size_b) / (1 + jaccard)
    return intersection / min(size_a, size_b)


def shingle_text(text: str, k: int = 5) -> Set[str]:
    """Generate k-shingles from text for MinHash."""
    words = text.lower().split()
    if len(words) < k:
        return {" ".join(words)}
    return {" ".join(words[i:i+k]) for i in range(len(words) - k + 1)}


def compute_containment(
    tokens_a: Set[str],
    tokens_b: Set[str],
    num_perm: int = 128
) -> float:
    """Direct containment computation via MinHash (no LSH index needed)."""
    if not tokens_a or not tokens_b:
        return 0.0

    m1 = tokens_to_minhash(tokens_a, num_perm)
    m2 = tokens_to_minhash(tokens_b, num_perm)

    jaccard = m1.jaccard(m2)
    return containment_from_jaccard(jaccard, len(tokens_a), len(tokens_b))


def cluster_articles_by_containment(
    articles: List[Tuple[str, Set[str]]],  # [(article_id, tokens)]
    threshold: float = 0.9,
    num_perm: int = 128
) -> List[List[str]]:
    """
    Cluster articles by pairwise containment.
    Returns list of clusters (each cluster is a list of article_ids).
    Simple O(n^2) within small buckets — no LSH index needed.
    """
    if not articles:
        return []

    # Build MinHash for each
    minhashes = {}
    for aid, tokens in articles:
        minhashes[aid] = tokens_to_minhash(tokens, num_perm)

    # Union-find clustering
    parent = {aid: aid for aid, _ in articles}

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(x, y):
        px, py = find(x), find(y)
        if px != py:
            parent[px] = py

    ids = [aid for aid, _ in articles]
    n = len(ids)

    for i in range(n):
        for j in range(i + 1, n):
            aid1, aid2 = ids[i], ids[j]
            m1, m2 = minhashes[aid1], minhashes[aid2]
            jaccard = m1.jaccard(m2)
            containment = containment_from_jaccard(jaccard, len(articles[i][1]), len(articles[j][1]))
            if containment >= threshold:
                union(aid1, aid2)

    # Collect clusters
    clusters = {}
    for aid in ids:
        root = find(aid)
        clusters.setdefault(root, []).append(aid)

    return list(clusters.values())