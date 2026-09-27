use dashmap::DashMap;
use parking_lot::RwLock;
use std::collections::HashMap;
use std::sync::Arc;

/// Thread-safe ingestion statistics tracking
pub struct IngestStats {
    counts: DashMap<(String, String), u64>,
}

impl IngestStats {
    pub fn new() -> Self {
        Self {
            counts: DashMap::new(),
        }
    }

    /// Reset all counters
    pub fn reset(&self) {
        self.counts.clear();
    }

    /// Record an event for a source
    pub fn record(&self, source: &str, event: &str, n: u64) {
        let key = (source.to_string(), event.to_string());
        *self.counts.entry(key).or_insert(0) += n;
    }

    /// Return JSON-serializable snapshot of counts
    pub fn snapshot(&self) -> HashMap<String, u64> {
        let mut result = HashMap::new();
        for entry in self.counts.iter() {
            let ((source, event), count) = entry.pair();
            result.insert(format!("{}.{}", source, event), *count);
        }
        result
    }

    /// Render as markdown table for GitHub Actions summary
    pub fn render_markdown(&self) -> String {
        if self.counts.is_empty() {
            return "_No ingestion stats recorded._".to_string();
        }

        // Group by source
        let mut by_source: HashMap<String, HashMap<String, u64>> = HashMap::new();
        for entry in self.counts.iter() {
            let ((source, event), count) = entry.pair();
            by_source
                .entry(source.clone())
                .or_default()
                .insert(event.clone(), *count);
        }

        let mut lines = vec!["| Source | Event | Count |".to_string(), "|--------|-------|-------|".to_string()];
        let mut sources: Vec<_> = by_source.keys().cloned().collect();
        sources.sort();

        for source in sources {
            if let Some(events) = by_source.get(&source) {
                let mut events_vec: Vec<_> = events.keys().cloned().collect();
                events_vec.sort();
                for event in events_vec {
                    let count = events[&event];
                    lines.push(format!("| {} | {} | {} |", source, event, count));
                }
            }
        }

        lines.join("\n")
    }
}

impl Default for IngestStats {
    fn default() -> Self {
        Self::new()
    }
}

/// Global singleton for ingestion stats
lazy_static::lazy_static! {
    pub static ref STATS: IngestStats = IngestStats::new();
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_record_and_snapshot() {
        let stats = IngestStats::new();
        stats.record("bbc", "ok", 1);
        stats.record("bbc", "ok", 2);
        stats.record("guardian", "feed_ok", 1);

        let snapshot = stats.snapshot();
        assert_eq!(snapshot.get("bbc.ok"), Some(&3));
        assert_eq!(snapshot.get("guardian.feed_ok"), Some(&1));
    }

    #[test]
    fn test_render_markdown() {
        let stats = IngestStats::new();
        stats.record("bbc", "ok", 5);
        stats.record("bbc", "feed_ok", 3);

        let markdown = stats.render_markdown();
        assert!(markdown.contains("bbc"));
        assert!(markdown.contains("ok"));
        assert!(markdown.contains("5"));
    }

    #[test]
    fn test_empty_render() {
        let stats = IngestStats::new();
        let markdown = stats.render_markdown();
        assert_eq!(markdown, "_No ingestion stats recorded._");
    }
}