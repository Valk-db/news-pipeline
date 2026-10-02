"""MinHash utilities for near-duplicate detection using algebraic containment."""

import math
from datasketch import MinHash
from typing import List, Set, Tuple
import json


# Token sets at or below this size are compared exactly instead of through
# MinHash. With 128 permutations the Jaccard estimate over a handful of
# shingles is coarse enough to merge unrelated articles, and exact comparison
# on sets this small is cheaper than hashing them. Above it the estimate is
# tight enough that the inversion below is well behaved.
EXACT_COMPARE_MAX_TOKENS = 64

# Merge predicate floors for cluster_articles_by_containment. Containment is
# not transitive: one long aggregator article can >=90%-contain several
# unrelated short ones, and single-link union on one-sided containment puts
# them all in one unit (inflating distinct_owners at the gate). The reverse
# containment floor requires the larger article to be substantially covered by
# the overlap too, which true syndications satisfy and aggregator/short pairs
# do not. The absolute floor guards degenerate estimates on small sets.
REVERSE_CONTAINMENT_FLOOR = 0.5
MIN_ABSOLUTE_OVERLAP = 5  # shingles


def tokens_to_minhash(tokens: Set[str], num_perm: int = 128) -> MinHash:
    """Create a MinHash from a set of tokens."""
    m = MinHash(num_perm=num_perm)
    # update_batch hashes all tokens in one vectorised pass (~8x faster than a per-token
    # update loop on article-sized shingle sets) and yields identical hashvalues.
    m.update_batch([token.encode("utf-8") for token in tokens])
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


def _estimate_intersection(jaccard: float, size_a: int, size_b: int) -> float:
    """Invert the Jaccard identity to estimate |A intersect B|.

    Jaccard = |A∩B| / (|A| + |B| - |A∩B|)
    => |A∩B| = J * (|A| + |B|) / (1 + J)
    """
    return jaccard * (size_a + size_b) / (1 + jaccard)


def _jaccard_estimate_sigma(jaccard: float, num_perm: int) -> float:
    """Standard error of the MinHash Jaccard estimate.

    Each of the num_perm signature components is an independent Bernoulli
    trial with success probability ~= the true Jaccard, so the estimate's
    standard error is sqrt(j(1-j)/k). Used to tell estimator noise apart from
    an incoherent estimate (one that violates set algebra).
    """
    if num_perm <= 0:
        return 0.0
    return math.sqrt(max(jaccard, 0.0) * max(1.0 - jaccard, 0.0) / num_perm)


def containment_from_jaccard(
    jaccard: float,
    size_a: int,
    size_b: int,
    num_perm: int = 128
) -> float:
    """
    Compute symmetric containment from Jaccard and set sizes.

    containment = |A∩B| / min(|A|, |B|), always within [0, 1].

    Two guards beyond the plain inversion. First, the result is clamped: the
    inversion is only as good as the Jaccard estimate, and an overestimate can
    imply a containment above 1.0 (e.g. J=0.9 with sizes (100, 10) inverts to
    5.211), which the old code passed straight into the union test. Second, an
    estimate that implies an intersection larger than the smaller set *by more
    than estimator noise allows* is incoherent -- no true set configuration
    produces it -- and the pair is refused (0.0) instead of merged. Overshoot
    within 3 sigma of the estimate's standard error is still clamped to 1.0,
    so genuine near-duplicates at the boundary keep merging.
    """
    if size_a <= 0 or size_b <= 0:
        return 0.0
    if jaccard <= 0:
        return 0.0
    if jaccard >= 1:
        return 1.0

    small = min(size_a, size_b)
    intersection = _estimate_intersection(jaccard, size_a, size_b)
    if intersection > small:
        # Set algebra caps the true intersection at the smaller set's size,
        # so an estimate above it is either noise or incoherent. Propagate the
        # Jaccard standard error through the inversion (d(intersection)/dJ =
        # (sa+sb)/(1+J)^2) and refuse only overshoot beyond 3 sigma: that far
        # out the numbers cannot be a noisy reading of a real near-duplicate.
        sigma_j = _jaccard_estimate_sigma(jaccard, num_perm)
        sigma_intersection = sigma_j * (size_a + size_b) / (1 + jaccard) ** 2
        if intersection - small > 3 * sigma_intersection:
            return 0.0
    return min(1.0, max(0.0, intersection / small))


def shingle_text(text: str, k: int = 5) -> Set[str]:
    """Generate k-shingles from text for MinHash."""
    words = text.lower().split()
    if len(words) < k:
        return {" ".join(words)}
    return {" ".join(words[i:i+k]) for i in range(len(words) - k + 1)}


def _exact_containment(tokens_a: Set[str], tokens_b: Set[str]) -> float:
    """Exact containment for small token sets: no estimator, no inversion noise."""
    if not tokens_a or not tokens_b:
        return 0.0
    intersection = len(tokens_a & tokens_b)
    return intersection / min(len(tokens_a), len(tokens_b))


def compute_containment(
    tokens_a: Set[str],
    tokens_b: Set[str],
    num_perm: int = 128
) -> float:
    """Direct containment computation, exact for small token sets.

    Small shingle sets go through exact set comparison: with 128 permutations
    the MinHash estimate over a handful of shingles is coarse enough to merge
    unrelated articles, and exact comparison on sets this small is cheaper than
    hashing them. Larger sets use the MinHash path.
    """
    if not tokens_a or not tokens_b:
        return 0.0

    if max(len(tokens_a), len(tokens_b)) <= EXACT_COMPARE_MAX_TOKENS:
        return _exact_containment(tokens_a, tokens_b)

    m1 = tokens_to_minhash(tokens_a, num_perm)
    m2 = tokens_to_minhash(tokens_b, num_perm)

    jaccard = m1.jaccard(m2)
    return containment_from_jaccard(jaccard, len(tokens_a), len(tokens_b), num_perm)


def _merge_predicate(
    jaccard: float,
    size_a: int,
    size_b: int,
    threshold: float,
    reverse_threshold: float,
    min_absolute_overlap: int,
    num_perm: int,
) -> bool:
    """Decide whether two articles belong in one reporting unit.

    Three conditions, all required:

    1. Symmetric containment |A∩B|/min(|A|,|B|) >= threshold -- the short
       article is substantially covered by the overlap (syndication, reprint).
    2. Reverse containment |A∩B|/max(|A|,|B|) >= reverse_threshold -- the long
       article is substantially covered too. This is what breaks the
       aggregator chain: a 1000-shingle roundup that 90%-contains five
       unrelated 10-shingle briefs has reverse containment ~0.01, so the
       briefs no longer land in one unit and inflate distinct_owners.
    3. Absolute overlap floor -- the implied intersection is at least a few
       shingles, guarding degenerate estimates on small sets.

    The Jaccard estimate comes from MinHash; for small sets the caller should
    prefer exact comparison (see compute_containment).
    """
    if jaccard <= 0:
        return False
    small = min(size_a, size_b)
    large = max(size_a, size_b)
    if small <= 0:
        return False

    containment = containment_from_jaccard(jaccard, size_a, size_b, num_perm)
    if containment < threshold:
        return False

    intersection = _estimate_intersection(jaccard, size_a, size_b)
    # The floor can never exceed the smaller set: an overlap larger than the
    # small set is impossible, so cap it there (exact duplicates of tiny
    # texts still merge).
    if intersection < min(min_absolute_overlap, small):
        return False
    if min(1.0, intersection / large) < reverse_threshold:
        return False
    return True


def cluster_articles_by_containment(
    articles: List[Tuple[str, Set[str]]],  # [(article_id, tokens)]
    threshold: float = 0.9,
    num_perm: int = 128,
    reverse_threshold: float = REVERSE_CONTAINMENT_FLOOR,
    min_absolute_overlap: int = MIN_ABSOLUTE_OVERLAP,
) -> List[List[str]]:
    """
    Cluster articles by pairwise containment.
    Returns list of clusters (each cluster is a list of article_ids).
    Simple O(n^2) within small buckets — no LSH index needed.

    Union-find over the bidirectional merge predicate (_merge_predicate).
    Single-link union is kept for the candidate generation, but the predicate
    itself now requires reverse containment, so the classic failure -- one
    long aggregator chaining several unrelated short articles into a single
    unit -- cannot form at the pair level anymore.
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
            tokens_a, tokens_b = articles[i][1], articles[j][1]
            size_a, size_b = len(tokens_a), len(tokens_b)
            if max(size_a, size_b) <= EXACT_COMPARE_MAX_TOKENS:
                # Exact comparison for short texts: with 128 permutations the
                # MinHash estimate over a handful of shingles is coarse enough
                # to merge unrelated articles, and exact comparison here is
                # cheaper than hashing. Also makes small-set clustering
                # deterministic instead of estimate-noise dependent.
                if not tokens_a or not tokens_b:
                    continue
                inter = len(tokens_a & tokens_b)
                union_size = len(tokens_a | tokens_b)
                jaccard = inter / union_size if union_size else 0.0
            else:
                m1, m2 = minhashes[aid1], minhashes[aid2]
                jaccard = m1.jaccard(m2)
            if _merge_predicate(
                jaccard,
                size_a,
                size_b,
                threshold,
                reverse_threshold,
                min_absolute_overlap,
                num_perm,
            ):
                union(aid1, aid2)

    # Collect clusters
    clusters = {}
    for aid in ids:
        root = find(aid)
        clusters.setdefault(root, []).append(aid)

    return list(clusters.values())