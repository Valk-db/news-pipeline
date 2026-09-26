use crate::models::MinHashSignature;
use serde::{Deserialize, Serialize};
use std::collections::{HashMap, HashSet};

/// Number of permutations for MinHash (default 128)
pub const DEFAULT_NUM_PERM: usize = 128;

/// MinHash implementation for near-duplicate detection
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct MinHash {
    hashvalues: Vec<u64>,
    num_perm: usize,
}

impl MinHash {
    pub fn new(num_perm: usize) -> Self {
        Self {
            hashvalues: vec![u64::MAX; num_perm],
            num_perm,
        }
    }

    pub fn from_hashvalues(hashvalues: Vec<u64>, num_perm: usize) -> Self {
        Self { hashvalues, num_perm }
    }

    /// Update with a token
    pub fn update(&mut self, token: &[u8]) {
        // Use a simple but effective hash function (xxhash-like)
        // For production, consider using a proper xxhash implementation
        let hash = Self::hash_token(token);
        for i in 0..self.num_perm {
            // Use different seeds for each permutation (use wrapping_mul to avoid overflow)
            let seed = (i as u64).wrapping_mul(0x9e3779b97f4a7c15);
            let perm_hash = hash.wrapping_add(seed);
            let perm_hash = Self::mix64(perm_hash);
            if perm_hash < self.hashvalues[i] {
                self.hashvalues[i] = perm_hash;
            }
        }
    }

    /// Get Jaccard similarity with another MinHash
    pub fn jaccard(&self, other: &MinHash) -> f64 {
        if self.num_perm != other.num_perm {
            return 0.0;
        }
        let matches = self.hashvalues.iter().zip(&other.hashvalues).filter(|(a, b)| a == b).count();
        matches as f64 / self.num_perm as f64
    }

    /// Serialize to JSON string
    pub fn to_json(&self) -> Result<String, serde_json::Error> {
        serde_json::to_string(&MinHashSignature {
            hashvalues: self.hashvalues.clone(),
            num_perm: self.num_perm,
        })
    }

    /// Deserialize from JSON string
    pub fn from_json(json: &str) -> Result<Self, serde_json::Error> {
        let sig: MinHashSignature = serde_json::from_str(json)?;
        Ok(Self::from_hashvalues(sig.hashvalues, sig.num_perm))
    }

    fn hash_token(token: &[u8]) -> u64 {
        // Simple FNV-1a hash
        let mut hash: u64 = 0xcbf29ce484222325;
        for &byte in token {
            hash ^= byte as u64;
            hash = hash.wrapping_mul(0x100000001b3);
        }
        hash
    }

    fn mix64(mut x: u64) -> u64 {
        x ^= x >> 33;
        x = x.wrapping_mul(0xff51afd7ed558ccd);
        x ^= x >> 33;
        x = x.wrapping_mul(0xc4ceb9fe1a85ec53);
        x ^= x >> 33;
        x
    }
}

/// Generate k-shingles from text for MinHash
pub fn shingle_text(text: &str, k: usize) -> HashSet<String> {
    let lower = text.to_lowercase();
    let words: Vec<&str> = lower.split_whitespace().collect();
    if words.len() < k {
        if words.is_empty() {
            return HashSet::new();
        }
        return [words.join(" ")].into_iter().collect();
    }
    words
        .windows(k)
        .map(|w| w.join(" "))
        .collect()
}

/// Compute containment from Jaccard and set sizes
pub fn containment_from_jaccard(jaccard: f64, size_a: usize, size_b: usize) -> f64 {
    if jaccard <= 0.0 {
        return 0.0;
    }
    if jaccard >= 1.0 {
        return 1.0;
    }
    let intersection = jaccard * (size_a + size_b) as f64 / (1.0 + jaccard);
    intersection / (size_a.min(size_b) as f64)
}

/// Compute containment directly via MinHash
pub fn compute_containment(
    tokens_a: &HashSet<String>,
    tokens_b: &HashSet<String>,
    num_perm: usize,
) -> f64 {
    if tokens_a.is_empty() || tokens_b.is_empty() {
        return 0.0;
    }

    let mut m1 = MinHash::new(num_perm);
    let mut m2 = MinHash::new(num_perm);

    for token in tokens_a {
        m1.update(token.as_bytes());
    }
    for token in tokens_b {
        m2.update(token.as_bytes());
    }

    let jaccard = m1.jaccard(&m2);
    containment_from_jaccard(jaccard, tokens_a.len(), tokens_b.len())
}

/// Cluster articles by pairwise containment
/// Returns list of clusters (each cluster is a list of article IDs)
pub fn cluster_articles_by_containment(
    articles: Vec<(String, HashSet<String>)>, // [(article_id, tokens)]
    threshold: f64,
    num_perm: usize,
) -> Vec<Vec<String>> {
    if articles.is_empty() {
        return Vec::new();
    }

    // Build MinHash for each
    let mut minhashes: HashMap<String, MinHash> = HashMap::new();
    for (aid, tokens) in &articles {
        let mut m = MinHash::new(num_perm);
        for token in tokens {
            m.update(token.as_bytes());
        }
        minhashes.insert(aid.clone(), m);
    }

    // Union-find clustering
    let mut parent: HashMap<String, String> = articles.iter().map(|(aid, _)| (aid.clone(), aid.clone())).collect();

    fn find(parent: &mut HashMap<String, String>, x: &str) -> String {
        let mut current = x.to_string();
        while parent.get(&current).map(|p| p.as_str()) != Some(&current) {
            let p = parent[&current].clone();
            parent.insert(current.clone(), p.clone());
            current = p;
        }
        current
    }

    fn union(parent: &mut HashMap<String, String>, x: &str, y: &str) {
        let px = find(parent, x);
        let py = find(parent, y);
        if px != py {
            parent.insert(px, py);
        }
    }

    let ids: Vec<String> = articles.iter().map(|(aid, _)| aid.clone()).collect();
    let n = ids.len();

    for i in 0..n {
        for j in (i + 1)..n {
            let aid1 = &ids[i];
            let aid2 = &ids[j];
            let m1 = &minhashes[aid1];
            let m2 = &minhashes[aid2];
            let jaccard = m1.jaccard(m2);

            // Find original tokens for size calculation
            let tokens_a = articles.iter().find(|(aid, _)| aid == aid1).map(|(_, t)| t.len()).unwrap_or(0);
            let tokens_b = articles.iter().find(|(aid, _)| aid == aid2).map(|(_, t)| t.len()).unwrap_or(0);

            let containment = containment_from_jaccard(jaccard, tokens_a, tokens_b);
            if containment >= threshold {
                union(&mut parent, aid1, aid2);
            }
        }
    }

    // Collect clusters
    let mut clusters: HashMap<String, Vec<String>> = HashMap::new();
    for aid in ids {
        let root = find(&mut parent, &aid);
        clusters.entry(root).or_default().push(aid);
    }

    clusters.into_values().collect()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_shingle_text() {
        let text = "hello world this is a test";
        let shingles = shingle_text(text, 3);
        assert_eq!(shingles.len(), 4); // 6 words - 3 + 1 = 4
    }

    #[test]
    fn test_shingle_text_short() {
        let text = "hello world";
        let shingles = shingle_text(text, 5);
        assert_eq!(shingles.len(), 1);
        assert!(shingles.contains("hello world"));
    }

    #[test]
    fn test_minhash_identical() {
        let mut m1 = MinHash::new(128);
        let mut m2 = MinHash::new(128);
        m1.update(b"test");
        m2.update(b"test");
        assert_eq!(m1.jaccard(&m2), 1.0);
    }

    #[test]
    fn test_minhash_different() {
        let mut m1 = MinHash::new(128);
        let mut m2 = MinHash::new(128);
        m1.update(b"hello");
        m2.update(b"world");
        let j = m1.jaccard(&m2);
        assert!(j < 1.0);
    }

    #[test]
    fn test_containment_from_jaccard() {
        // Identical sets
        assert_eq!(containment_from_jaccard(1.0, 10, 10), 1.0);
        // No overlap
        assert_eq!(containment_from_jaccard(0.0, 10, 10), 0.0);
        // Partial - Jaccard 0.5 with sizes 10, 20 gives containment = 1.0
        let c = containment_from_jaccard(0.5, 10, 20);
        assert!((c - 1.0).abs() < 0.001);
    }

    #[test]
    fn test_cluster_identical() {
        let tokens_a: HashSet<String> = ["a", "b", "c"].iter().map(|s| s.to_string()).collect();
        let tokens_b: HashSet<String> = ["a", "b", "c"].iter().map(|s| s.to_string()).collect();
        let articles = vec![("1".to_string(), tokens_a), ("2".to_string(), tokens_b)];
        let clusters = cluster_articles_by_containment(articles, 0.9, 128);
        assert_eq!(clusters.len(), 1);
        assert_eq!(clusters[0].len(), 2);
    }

    #[test]
    fn test_cluster_different() {
        let tokens_a: HashSet<String> = ["a", "b", "c"].iter().map(|s| s.to_string()).collect();
        let tokens_b: HashSet<String> = ["x", "y", "z"].iter().map(|s| s.to_string()).collect();
        let articles = vec![("1".to_string(), tokens_a), ("2".to_string(), tokens_b)];
        let clusters = cluster_articles_by_containment(articles, 0.9, 128);
        assert_eq!(clusters.len(), 2);
    }
}