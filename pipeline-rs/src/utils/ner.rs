//! Entity canonicalization for story grouping
//!
//! This module handles everything *except* the spaCy NER call itself.
//! The actual NER extraction (spaCy -> entities) is in T5.
//! Here we implement: normalization, alias generation, EntityCanonicalizer,
//! resolve_entities_to_canonical, canonical_jaccard.

use crate::database::PgPool;
use regex::Regex;
use serde::{Deserialize, Serialize};
use sqlx::Row;
use std::collections::{HashMap, HashSet};
use std::sync::Arc;
use tokio::sync::RwLock;
use tracing::{debug, warn, info, error};
use uuid::Uuid;

// rust-bert NER using tch backend (libtorch)
use rust_bert::pipelines::ner::{NERModel, Entity};

/// Entity labels we care about (OntoNotes style)
pub const ENTITY_LABELS: &[&str] = &["PERSON", "ORG", "GPE", "LOC", "EVENT", "PRODUCT"];

/// Primary entity labels for story grouping
pub const PRIMARY_ENTITY_LABELS: &[&str] = &["PERSON", "ORG", "GPE"];

/// A resolved entity mention with canonical ID
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct CanonicalMention {
    pub surface_form: String,   // What was in the text
    pub canonical_id: String,   // UUID of canonical entity
    pub canonical_name: String, // Preferred display name
    pub entity_type: String,    // PERSON, ORG, GPE, etc.
    pub confidence: f64,        // Resolution confidence (0-1)
}

/// Extracted entities format: Dict<label, Vec<String>>
pub type EntitiesDict = HashMap<String, Vec<String>>;

// NER using tch backend (libtorch) - enabled for NER and embeddings
// Uses dedicated worker threads with channels to own models (which are not Send/Sync)
use rust_bert::pipelines::sentence_embeddings::{SentenceEmbeddingsBuilder, SentenceEmbeddingsModelType};
use tokio::sync::oneshot;
use std::sync::OnceLock;

type NerRequest = (String, oneshot::Sender<Result<Vec<Vec<Entity>>, String>>);
type EmbeddingRequest = (String, oneshot::Sender<Result<Vec<Vec<f32>>, String>>);

static NER_WORKER_SENDER: OnceLock<tokio::sync::mpsc::UnboundedSender<NerRequest>> = OnceLock::new();
static EMBEDDING_WORKER_SENDER: OnceLock<tokio::sync::mpsc::UnboundedSender<EmbeddingRequest>> = OnceLock::new();

/// Initialize the NER worker thread (call once at startup)
pub async fn init_ner_worker() {
    if NER_WORKER_SENDER.get().is_some() {
        return; // Already initialized
    }

    let (tx, mut rx): (tokio::sync::mpsc::UnboundedSender<NerRequest>, tokio::sync::mpsc::UnboundedReceiver<NerRequest>) = tokio::sync::mpsc::unbounded_channel();

    // Spawn dedicated blocking thread for NER
    tokio::task::spawn_blocking(move || {
        // Load NER model once in this thread
        let model = match NERModel::new(Default::default()) {
            Ok(m) => {
                info!("NER model loaded successfully in worker thread");
                m
            }
            Err(e) => {
                error!("Failed to load NER model: {}", e);
                // Keep running to respond to requests with errors
                loop {
                    if let Some((text, resp_tx)) = rx.blocking_recv() {
                        let _ = resp_tx.send(Err(format!("NER model failed to load: {}", e)));
                    } else {
                        break;
                    }
                }
                return;
            }
        };

        // Process requests
        while let Some((text, resp_tx)) = rx.blocking_recv() {
            let max_chars = 2000;
            let truncated = if text.len() > max_chars {
                &text[..max_chars]
            } else {
                &text
            };
            let result = model.predict(&[truncated]);
            let _ = resp_tx.send(Ok(result));
        }
    });

    NER_WORKER_SENDER.set(tx).ok();
    info!("NER worker thread initialized");
}

/// Initialize the embedding worker thread (call once at startup)
pub async fn init_embedding_worker() {
    if EMBEDDING_WORKER_SENDER.get().is_some() {
        return; // Already initialized
    }

    let (tx, mut rx): (tokio::sync::mpsc::UnboundedSender<EmbeddingRequest>, tokio::sync::mpsc::UnboundedReceiver<EmbeddingRequest>) = tokio::sync::mpsc::unbounded_channel();

    // Spawn dedicated blocking thread for embeddings
    tokio::task::spawn_blocking(move || {
        // Load embedding model once in this thread
        let model = match SentenceEmbeddingsBuilder::remote(SentenceEmbeddingsModelType::AllMiniLmL6V2)
            .create_model()
        {
            Ok(m) => {
                info!("Embedding model loaded successfully in worker thread (384 dimensions)");
                m
            }
            Err(e) => {
                error!("Failed to load embedding model: {}", e);
                // Keep running to respond to requests with errors
                loop {
                    if let Some((text, resp_tx)) = rx.blocking_recv() {
                        let _ = resp_tx.send(Err(format!("Embedding model failed to load: {}", e)));
                    } else {
                        break;
                    }
                }
                return;
            }
        };

        // Process requests
        while let Some((text, resp_tx)) = rx.blocking_recv() {
            let max_chars = 2000;
            let truncated = if text.len() > max_chars {
                &text[..max_chars]
            } else {
                &text
            };
            match model.encode(&[truncated]) {
                Ok(embeddings) => {
                    let vec_embeddings: Vec<Vec<f32>> = embeddings.into_iter().map(|e| e.to_vec()).collect();
                    let _ = resp_tx.send(Ok(vec_embeddings));
                }
                Err(e) => {
                    let _ = resp_tx.send(Err(format!("Embedding prediction failed: {}", e)));
                }
            }
        }
    });

    EMBEDDING_WORKER_SENDER.set(tx).ok();
    info!("Embedding worker thread initialized");
}

/// Run NER prediction using the dedicated worker thread
async fn run_ner_prediction_async(text: &str) -> Result<Vec<Vec<Entity>>, Box<dyn std::error::Error + Send + Sync>> {
    init_ner_worker().await;

    let sender = NER_WORKER_SENDER.get().unwrap();
    let (resp_tx, resp_rx) = oneshot::channel();

    sender.send((text.to_string(), resp_tx)).map_err(|e| format!("Failed to send NER request: {}", e))?;

    match resp_rx.await {
        Ok(Ok(result)) => Ok(result),
        Ok(Err(e)) => Err(e.into()),
        Err(e) => Err(format!("NER worker channel closed: {}", e).into()),
    }
}

/// Run embedding prediction using the dedicated worker thread
async fn run_embedding_prediction_async(text: &str) -> Result<Vec<Vec<f32>>, Box<dyn std::error::Error + Send + Sync>> {
    init_embedding_worker().await;

    let sender = EMBEDDING_WORKER_SENDER.get().unwrap();
    let (resp_tx, resp_rx) = oneshot::channel();

    sender.send((text.to_string(), resp_tx)).map_err(|e| format!("Failed to send embedding request: {}", e))?;

    match resp_rx.await {
        Ok(Ok(result)) => Ok(result),
        Ok(Err(e)) => Err(e.into()),
        Err(e) => Err(format!("Embedding worker channel closed: {}", e).into()),
    }
}

/// EntityCanonicalizer resolves entity mentions to canonical entities using database-backed aliases
#[derive(Clone)]
pub struct EntityCanonicalizer {
    pool: Arc<PgPool>,
    cache: Arc<RwLock<HashMap<String, CanonicalMention>>>, // normalized surface -> CanonicalMention
    initialized: bool,
}

impl EntityCanonicalizer {
    pub fn new(pool: Arc<PgPool>) -> Self {
        Self {
            pool,
            cache: Arc::new(RwLock::new(HashMap::new())),
            initialized: false,
        }
    }

    /// Check if initialized
    pub fn is_initialized(&self) -> bool {
        self.initialized
    }

    /// Load all canonical entities and aliases from database
    pub async fn initialize(&mut self) -> Result<(), sqlx::Error> {
        if self.initialized {
            return Ok(());
        }

        // Load all canonical entities with their aliases
        let rows = sqlx::query(
            r#"
            SELECT ce.id, ce.canonical_name, ce.entity_type, ea.alias
            FROM canonical_entities ce
            LEFT JOIN entity_aliases ea ON ea.canonical_entity_id = ce.id
            "#
        )
        .fetch_all(&*self.pool)
        .await?;

        let mut cache = self.cache.write().await;
        for row in rows {
            let canonical_id: Uuid = row.get("id");
            let canonical_id = canonical_id.to_string();
            let canonical_name: String = row.get("canonical_name");
            let entity_type: String = row.get("entity_type");

            // Add alias if present
            let alias_opt: Option<String> = row.get("alias");
            if let Some(alias) = alias_opt {
                let normalized = Self::normalize_text(&alias, &entity_type);
                cache.insert(
                    normalized,
                    CanonicalMention {
                        surface_form: alias.clone(),
                        canonical_id: canonical_id.clone(),
                        canonical_name: canonical_name.clone(),
                        entity_type: entity_type.clone(),
                        confidence: 1.0,
                    },
                );
            }

            // Also cache the canonical name itself
            let normalized = Self::normalize_text(&canonical_name, &entity_type);
            cache.insert(
                normalized,
                CanonicalMention {
                    surface_form: canonical_name.clone(),
                    canonical_id: canonical_id.clone(),
                    canonical_name,
                    entity_type: entity_type.clone(),
                    confidence: 1.0,
                },
            );
        }

        self.initialized = true;
        debug!("EntityCanonicalizer initialized with {} entries", cache.len());
        Ok(())
    }

    /// Normalize text for matching: lowercase, strip punctuation, collapse whitespace
    pub fn normalize_text(text: &str, entity_type: &str) -> String {
        let mut text = text.to_lowercase().trim().to_string();

        // Remove common honorifics/titles
        let honorifics = Regex::new(r"\b(mr\.?|mrs\.?|ms\.?|dr\.?|prof\.?|president|prime minister|pm|secretary|minister)\s+").unwrap();
        text = honorifics.replace_all(&text, "").to_string();

        // Remove punctuation except hyphens in names
        let punct = Regex::new(r"[^\w\s-]").unwrap();
        text = punct.replace_all(&text, "").to_string();

        // Collapse whitespace
        let ws = Regex::new(r"\s+").unwrap();
        text = ws.replace_all(&text, " ").to_string();

        let normalized = text.trim().to_string();

        // Include entity type in normalized key to distinguish same surface form different types
        if !entity_type.is_empty() {
            format!("{}:{}", entity_type, normalized)
        } else {
            normalized
        }
    }

    /// Generate common aliases for a canonical entity
    pub fn generate_aliases(base_name: &str, entity_type: &str) -> Vec<String> {
        let mut aliases = HashSet::new();
        let normalized = Self::normalize_text(base_name, "");
        aliases.insert(normalized.clone());

        match entity_type {
            "PERSON" => {
                let parts: Vec<&str> = normalized.split_whitespace().collect();
                if parts.len() >= 2 {
                    // "joe biden" -> "biden", "j biden"
                    aliases.insert(parts.last().unwrap().to_string());
                    aliases.insert(format!("{} {}", parts[0].chars().next().unwrap(), parts.last().unwrap()));
                    aliases.insert(format!("{} {}", parts[0], parts.last().unwrap()));
                }
            }
            "ORG" => {
                let words: Vec<&str> = normalized.split_whitespace().collect();
                if words.len() >= 2 {
                    // "european union" -> "eu", "e u"
                    let acronym: String = words.iter().filter(|w| !w.is_empty()).map(|w| w.chars().next().unwrap()).collect();
                    if acronym.len() >= 2 {
                        aliases.insert(acronym.clone());
                        aliases.insert(words.iter().map(|w| w.chars().next().unwrap()).collect::<String>());
                    }
                    // Also add individual words > 3 chars
                    for w in words {
                        if w.len() > 3 {
                            aliases.insert(w.to_string());
                        }
                    }
                }
            }
            "GPE" | "LOC" => {
                // Common country/region variants
                if normalized.contains("united states") {
                    aliases.extend(["us".to_string(), "usa".to_string(), "america".to_string()]);
                }
                if normalized.contains("united kingdom") {
                    aliases.extend(["uk".to_string(), "britain".to_string(), "great britain".to_string()]);
                }
                if normalized.contains("european union") {
                    aliases.insert("eu".to_string());
                }
            }
            _ => {}
        }

        aliases.into_iter().collect()
    }

    /// Resolve a surface form to a canonical entity
    /// Returns None if no match found (caller should create new canonical entity)
    pub async fn resolve(&self, surface_form: &str, entity_type: &str) -> Option<CanonicalMention> {
        if !self.initialized {
            return None;
        }

        let normalized = Self::normalize_text(surface_form, entity_type);
        {
            let cache = self.cache.read().await;
            if let Some(mention) = cache.get(&normalized) {
                return Some(mention.clone());
            }
        }

        // Try fuzzy matching for common variations
        let cache = self.cache.read().await;
        for (cached_key, mention) in cache.iter() {
            if mention.entity_type != entity_type {
                continue;
            }
            // Simple fuzzy: check if one contains the other
            if normalized.contains(cached_key.as_str()) || cached_key.contains(normalized.as_str()) {
                // Additional check: they should share significant tokens
                let norm_tokens: HashSet<&str> = normalized.split_whitespace().collect();
                let cached_tokens: HashSet<&str> = cached_key.split_whitespace().collect();
                if !norm_tokens.is_disjoint(&cached_tokens) {
                    return Some(CanonicalMention {
                        surface_form: surface_form.to_string(),
                        canonical_id: mention.canonical_id.clone(),
                        canonical_name: mention.canonical_name.clone(),
                        entity_type: entity_type.to_string(),
                        confidence: 0.8,
                    });
                }
            }
        }

        None
    }

    /// Resolve or create a canonical entity for a surface form
    /// Creates new canonical entity + aliases if not found
    pub async fn get_or_create(&self, surface_form: &str, entity_type: &str) -> Result<CanonicalMention, sqlx::Error> {
        // Try to resolve first
        if let Some(resolved) = self.resolve(surface_form, entity_type).await {
            return Ok(resolved);
        }

        // Not found - create new canonical entity
        let canonical_id = Uuid::new_v4();

        // Create canonical entity
        sqlx::query(
            r#"
            INSERT INTO canonical_entities (id, canonical_name, entity_type)
            VALUES ($1, $2, $3)
            "#
        )
        .bind(canonical_id)
        .bind(surface_form)
        .bind(entity_type)
        .execute(&*self.pool)
        .await?;

        // Create initial alias (the surface form itself)
        sqlx::query(
            r#"
            INSERT INTO entity_aliases (canonical_entity_id, alias)
            VALUES ($1, $2)
            ON CONFLICT (canonical_entity_id, alias) DO NOTHING
            "#
        )
        .bind(canonical_id)
        .bind(surface_form)
        .execute(&*self.pool)
        .await?;

        // Generate and add common aliases
        for alias_text in Self::generate_aliases(surface_form, entity_type) {
            let normalized_alias = Self::normalize_text(&alias_text, entity_type);
            let normalized_main = Self::normalize_text(surface_form, entity_type);
            if normalized_alias != normalized_main {
                sqlx::query(
                    r#"
                    INSERT INTO entity_aliases (canonical_entity_id, alias)
                    VALUES ($1, $2)
                    ON CONFLICT (canonical_entity_id, alias) DO NOTHING
                    "#
                )
                .bind(canonical_id)
                .bind(alias_text.clone())
                .execute(&*self.pool)
                .await?;

                // Add to cache
                let mut cache = self.cache.write().await;
                cache.insert(
                    normalized_alias,
                    CanonicalMention {
                        surface_form: alias_text.clone(),
                        canonical_id: canonical_id.to_string(),
                        canonical_name: surface_form.to_string(),
                        entity_type: entity_type.to_string(),
                        confidence: 0.9,
                    },
                );
            }
        }

        // Add the main entry to cache
        let normalized = Self::normalize_text(surface_form, entity_type);
        let mention = CanonicalMention {
            surface_form: surface_form.to_string(),
            canonical_id: canonical_id.to_string(),
            canonical_name: surface_form.to_string(),
            entity_type: entity_type.to_string(),
            confidence: 1.0,
        };

        let mut cache = self.cache.write().await;
        cache.insert(normalized, mention.clone());

        debug!("Created new canonical entity: {} ({})", surface_form, entity_type);
        Ok(mention)
    }
}

/// Resolve extracted entities to canonical IDs
/// Returns (canonical_id_set, canonical_mentions) for story grouping
pub async fn resolve_entities_to_canonical(
    entities: &EntitiesDict,
    canonicalizer: &EntityCanonicalizer,
) -> Result<(HashSet<String>, Vec<CanonicalMention>), sqlx::Error> {
    let mut canonical_ids = HashSet::new();
    let mut mentions = Vec::new();

    for (entity_type, surface_forms) in entities {
        for surface in surface_forms {
            let mention = canonicalizer.get_or_create(surface, entity_type).await?;
            canonical_ids.insert(mention.canonical_id.clone());
            mentions.push(mention);
        }
    }

    Ok((canonical_ids, mentions))
}

/// Get a flat set of top entities across PERSON, ORG, GPE for story grouping
pub fn get_primary_entity_set(entities: &EntitiesDict) -> HashSet<String> {
    let mut entity_set = HashSet::new();
    for label in PRIMARY_ENTITY_LABELS {
        if let Some(entities_for_label) = entities.get(*label) {
            entity_set.extend(entities_for_label.iter().cloned());
        }
    }
    entity_set
}

/// Jaccard similarity between two entity sets
pub fn entity_set_jaccard(set_a: &HashSet<String>, set_b: &HashSet<String>) -> f64 {
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

/// Jaccard similarity on canonical entity ID sets
pub fn canonical_jaccard(set_a: &HashSet<String>, set_b: &HashSet<String>) -> f64 {
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

/// Extract entities from text using rust-bert NER pipeline
/// Maps rust-bert entity labels to OntoNotes-compatible labels:
/// - PER -> PERSON
/// - ORG -> ORG
/// - LOC -> GPE (geopolitical entity)
/// - MISC -> MISC (or dropped)
pub async fn extract_entities_top_n(text: &str, _top_n: Option<usize>) -> EntitiesDict {
    let entities = match run_ner_prediction_async(text).await {
        Ok(entities) => entities,
        Err(e) => {
            tracing::warn!("NER prediction failed: {}", e);
            return EntitiesDict::new();
        }
    };

    let mut result: EntitiesDict = HashMap::new();

    for entity in entities.into_iter().flatten() {
        // Map rust-bert labels to OntoNotes labels
        let label = match entity.label.as_str() {
            "PER" => "PERSON",
            "ORG" => "ORG",
            "LOC" => "GPE",  // Location -> Geopolitical Entity
            "MISC" => "MISC",
            _ => continue,
        };

        let surface = entity.word.trim().to_string();
        if surface.is_empty() {
            continue;
        }

        result.entry(label.to_string())
            .or_default()
            .push(surface);
    }

    // Deduplicate within each label
    for entities in result.values_mut() {
        entities.sort();
        entities.dedup();
    }

    tracing::info!("Extracted entities: {:?}", result);
    result
}

/// Extract embeddings from text using rust-bert sentence embeddings
/// Returns 384-dimensional vector for AllMiniLmL6V2 model
pub async fn extract_embeddings(text: &str) -> Option<Vec<f32>> {
    match run_embedding_prediction_async(text).await {
        Ok(embeddings) => {
            if let Some(embedding) = embeddings.into_iter().next() {
                tracing::info!("Generated embedding with {} dimensions", embedding.len());
                Some(embedding)
            } else {
                tracing::warn!("No embedding generated for text");
                None
            }
        }
        Err(e) => {
            tracing::warn!("Embedding prediction failed: {}", e);
            None
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_normalize_text() {
        let normalized = EntityCanonicalizer::normalize_text("  Mr. Joe Biden  ", "PERSON");
        assert_eq!(normalized, "PERSON:joe biden");
    }

    #[test]
    fn test_normalize_text_org() {
        let normalized = EntityCanonicalizer::normalize_text("European Union", "ORG");
        assert_eq!(normalized, "ORG:european union");
    }

    #[test]
    fn test_generate_aliases_person() {
        let aliases = EntityCanonicalizer::generate_aliases("Joe Biden", "PERSON");
        assert!(aliases.contains(&"biden".to_string()));
        assert!(aliases.contains(&"j biden".to_string()));
    }

    #[test]
    fn test_generate_aliases_org() {
        let aliases = EntityCanonicalizer::generate_aliases("European Union", "ORG");
        assert!(aliases.contains(&"eu".to_string()));
    }

    #[test]
    fn test_canonical_jaccard() {
        let mut set_a = HashSet::new();
        set_a.insert("1".to_string());
        set_a.insert("2".to_string());

        let mut set_b = HashSet::new();
        set_b.insert("2".to_string());
        set_b.insert("3".to_string());

        let j = canonical_jaccard(&set_a, &set_b);
        assert!((j - 1.0 / 3.0).abs() < 0.001);
    }

    #[test]
    fn test_entity_set_jaccard() {
        let mut set_a = HashSet::new();
        set_a.insert("Biden".to_string());
        set_a.insert("White House".to_string());

        let mut set_b = HashSet::new();
        set_b.insert("Biden".to_string());
        set_b.insert("Oval Office".to_string());

        let j = entity_set_jaccard(&set_a, &set_b);
        assert!((j - 1.0 / 3.0).abs() < 0.001);
    }
}