//! Verification utilities - Jaccard similarity, entity overlap calculations

use std::collections::HashSet;

/// Calculate Jaccard similarity between two string sets
pub fn entity_jaccard(set_a: &HashSet<String>, set_b: &HashSet<String>) -> f64 {
    if set_a.is_empty() && set_b.is_empty() {
        return 1.0;
    }
    if set_a.is_empty() || set_b.is_empty() {
        return 0.0;
    }
    let intersection = set_a.intersection(set_b).count();
    let union = set_a.union(set_b).count();
    intersection as f64 / union as f64
}

/// Calculate Jaccard similarity on canonical entity ID sets
pub fn canonical_jaccard(set_a: &HashSet<String>, set_b: &HashSet<String>) -> f64 {
    entity_jaccard(set_a, set_b)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_entity_jaccard() {
        let mut set_a = HashSet::new();
        set_a.insert("Biden".to_string());
        set_a.insert("White House".to_string());

        let mut set_b = HashSet::new();
        set_b.insert("Biden".to_string());
        set_b.insert("Oval Office".to_string());

        let j = entity_jaccard(&set_a, &set_b);
        assert!((j - 1.0 / 3.0).abs() < 0.001);
    }

    #[test]
    fn test_entity_jaccard_empty() {
        let set_a = HashSet::new();
        let mut set_b = HashSet::new();
        set_b.insert("test".to_string());

        assert_eq!(entity_jaccard(&set_a, &set_b), 0.0);
    }

    #[test]
    fn test_entity_jaccard_identical() {
        let mut set_a = HashSet::new();
        set_a.insert("test".to_string());

        let mut set_b = HashSet::new();
        set_b.insert("test".to_string());

        assert_eq!(entity_jaccard(&set_a, &set_b), 1.0);
    }
}