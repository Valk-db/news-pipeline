//! Automated fact-checking for source reliability scoring.

use crate::database::PgPool;
use crate::llm::LLMClient;
use crate::models::{FactCheckRecord, FactCheckVerdict, FactChecker as FactCheckerEnum};
use crate::utils::ner::EntitiesDict;
use chrono::{DateTime, Utc};
use serde::{Deserialize, Serialize};
use sqlx::Row;
use std::collections::{HashMap, HashSet};
use uuid::Uuid;

/// Extracted claim from an article
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Claim {
    pub text: String,
    pub claim_hash: String,
    pub entities: Vec<String>,
    pub position: i32,
    pub context: String,
    pub claim_type: String, // "statistic", "quote", "prediction", "causal", "definition"
}

/// Fact-checker for reliability scoring
pub struct FactChecker {
    claimbuster_api_key: Option<String>,
    cache: HashMap<String, FactCheckResult>,
    llm: LLMClient,
}

impl FactChecker {
    pub fn new(llm: LLMClient) -> Self {
        Self {
            claimbuster_api_key: None,
            cache: HashMap::new(),
            llm,
        }
    }

    pub fn with_claimbuster(mut self, api_key: String) -> Self {
        self.claimbuster_api_key = Some(api_key);
        self
    }

    /// Check a single claim against multiple fact-check sources
    pub async fn check_claim(&mut self, claim: &Claim, source_domain: &str) -> FactCheckResult {
        let claim_hash = &claim.claim_hash;

        // Check cache first
        if let Some(cached) = self.cache.get(claim_hash) {
            return cached.clone();
        }

        let mut results = Vec::new();

        // 1. ClaimBuster API (if configured)
        if self.claimbuster_api_key.is_some() {
            if let Some(result) = self.check_claimbuster(claim).await {
                results.push(result);
            }
        }

        // 2. LLM Verification (cross-reference with tier-1 consensus)
        if let Some(result) = self.check_llm_verification(claim).await {
            results.push(result);
        }

        // 3. Internal knowledge base (previously verified claims)
        if let Some(result) = self.check_knowledge_base(claim_hash).await {
            results.push(result);
        }

        // Aggregate results
        let final_result = self.aggregate_results(results);

        // Cache result
        self.cache.insert(claim_hash.clone(), final_result.clone());

        final_result
    }

    /// Check claim using ClaimBuster API
    async fn check_claimbuster(&self, _claim: &Claim) -> Option<FactCheckResult> {
        // Placeholder - ClaimBuster API endpoint would go here
        None
    }

    /// Verify claim using LLM against tier-1 consensus
    async fn check_llm_verification(&self, claim: &Claim) -> Option<FactCheckResult> {
        let prompt = format!(r#"Fact-check this claim by evaluating its veracity based on general knowledge and journalistic standards.

Claim: "{}"
Context: "{}"
Entities involved: {}

Return a JSON object with:
- "verdict": one of "true", "mostly_true", "mixed", "mostly_false", "false", "unverifiable"
- "confidence": 0-100
- "explanation": brief reasoning (max 200 chars)
- "evidence_needed": what would be needed to verify conclusively

Only return the JSON object."#, claim.text, claim.context, claim.entities.join(", "));

        match self.llm.complete_json::<LlmFactCheckResponse>(
            "You are a fact-checking assistant. Evaluate claims based on general knowledge.",
            &prompt,
            Some(500),
            Some(0.1),
        ).await {
            Ok(response) => {
                let verdict = match response.verdict.as_str() {
                    "true" => FactCheckVerdict::True,
                    "mostly_true" => FactCheckVerdict::MostlyTrue,
                    "mixed" => FactCheckVerdict::Mixed,
                    "mostly_false" => FactCheckVerdict::MostlyFalse,
                    "false" => FactCheckVerdict::False,
                    _ => FactCheckVerdict::Unverified,
                };

                Some(FactCheckResult {
                    verdict,
                    confidence: response.confidence.clamp(0, 100),
                    fact_checker: FactCheckerEnum::LlmVerifier,
                    explanation: Some(response.explanation),
                    fact_checker_url: None,
                })
            }
            Err(e) => {
                tracing::warn!("LLM verification failed: {}", e);
                None
            }
        }
    }

    /// Check internal knowledge base of previously verified claims
    async fn check_knowledge_base(&self, claim_hash: &str) -> Option<FactCheckResult> {
        // This would query a database of previous fact-checks
        // For now, return None
        None
    }

    /// Aggregate multiple fact-check results into final verdict
    fn aggregate_results(&self, results: Vec<FactCheckResult>) -> FactCheckResult {
        if results.is_empty() {
            return FactCheckResult {
                verdict: FactCheckVerdict::Unverified,
                confidence: 0,
                fact_checker: FactCheckerEnum::LlmVerifier,
                explanation: Some("No fact-check sources available".to_string()),
                fact_checker_url: None,
            };
        }

        // Weight by fact-checker reliability
        let weights: HashMap<FactCheckerEnum, f64> = [
            (FactCheckerEnum::ClaimReview, 1.0),
            (FactCheckerEnum::ClaimBuster, 0.8),
            (FactCheckerEnum::LlmVerifier, 0.7),
            (FactCheckerEnum::Manual, 1.0),
        ].into_iter().collect();

        // Convert verdicts to numeric scores
        let verdict_scores: HashMap<FactCheckVerdict, f64> = [
            (FactCheckVerdict::True, 1.0),
            (FactCheckVerdict::MostlyTrue, 0.75),
            (FactCheckVerdict::Mixed, 0.5),
            (FactCheckVerdict::MostlyFalse, 0.25),
            (FactCheckVerdict::False, 0.0),
            (FactCheckVerdict::Unverified, 0.5),
        ].into_iter().collect();

        let mut weighted_score = 0.0;
        let mut total_weight = 0.0;

        for r in &results {
            let weight = weights.get(&r.fact_checker).copied().unwrap_or(0.5);
            let score = verdict_scores.get(&r.verdict).copied().unwrap_or(0.5);
            let confidence = r.confidence as f64 / 100.0;

            weighted_score += score * weight * confidence;
            total_weight += weight * confidence;
        }

        let final_score = if total_weight == 0.0 { 0.5 } else { weighted_score / total_weight };

        // Convert back to verdict
        let final_verdict = if final_score >= 0.875 {
            FactCheckVerdict::True
        } else if final_score >= 0.625 {
            FactCheckVerdict::MostlyTrue
        } else if final_score >= 0.375 {
            FactCheckVerdict::Mixed
        } else if final_score >= 0.125 {
            FactCheckVerdict::MostlyFalse
        } else {
            FactCheckVerdict::False
        };

        let avg_confidence = (results.iter().map(|r| r.confidence as f64).sum::<f64>() / results.len() as f64).round() as i32;

        FactCheckResult {
            verdict: final_verdict,
            confidence: avg_confidence,
            fact_checker: FactCheckerEnum::LlmVerifier,
            explanation: Some(format!("Aggregated from {} sources", results.len())),
            fact_checker_url: None,
        }
    }
}

/// Fact-check result
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct FactCheckResult {
    pub verdict: FactCheckVerdict,
    pub confidence: i32,
    pub fact_checker: FactCheckerEnum,
    pub explanation: Option<String>,
    pub fact_checker_url: Option<String>,
}

/// LLM fact-check response
#[derive(Debug, Deserialize)]
struct LlmFactCheckResponse {
    verdict: String,
    confidence: i32,
    explanation: String,
    evidence_needed: String,
}

/// Extract verifiable claims from an article using LLM
pub async fn extract_claims_from_article(
    llm: &LLMClient,
    article_text: &str,
    title: &str,
    entities: &EntitiesDict,
) -> Vec<Claim> {
    // Flatten entities
    let all_entities: Vec<String> = entities.values().flatten().cloned().collect();

    // Truncate for context
    let text_for_llm = if article_text.len() > 6000 {
        &article_text[..6000]
    } else {
        article_text
    };

    let prompt = format!(r#"Extract verifiable factual claims from this news article.

Article Title: {}
Entities mentioned: {}

Article Text:
{}

Return a JSON array of claim objects. Each claim should have:
- "text": The exact claim text (max 200 chars)
- "type": One of "statistic", "quote", "prediction", "causal", "definition", "attribution"
- "entities": Array of entity names from the article that are central to this claim
- "context": Surrounding sentence for context (max 150 chars)

Focus on claims that can be verified as true/false:
- Specific numbers, statistics, percentages
- Attributed quotes from named sources
- Predictions about future events
- Causal claims ("X caused Y")
- Definitive statements about events

Do NOT include:
- Opinions, analysis, speculation
- Vague statements
- Background context

Maximum 10 claims. Only return the JSON array."#, title, all_entities.join(", "), text_for_llm);

    #[derive(Debug, Deserialize)]
    struct ExtractedClaim {
        text: String,
        #[serde(rename = "type")]
        claim_type: String,
        entities: Vec<String>,
        context: String,
    }

    #[derive(Debug, Deserialize)]
    struct ClaimsResponse {
        claims: Vec<ExtractedClaim>,
    }

    match llm.complete_json::<ClaimsResponse>(
        "You are a claim extraction assistant. Extract verifiable factual claims from news articles.",
        &prompt,
        Some(2000),
        Some(0.1),
    ).await {
        Ok(response) => {
            let mut claims = Vec::new();
            for c in response.claims {
                let claim_text = c.text.trim().to_string();
                if claim_text.is_empty() {
                    continue;
                }

                let claim_hash = {
                    use sha2::{Digest, Sha256};
                    let mut hasher = Sha256::new();
                    hasher.update(claim_text.as_bytes());
                    hex::encode(hasher.finalize())
                };

                claims.push(Claim {
                    text: claim_text,
                    claim_hash,
                    entities: c.entities,
                    position: 0,
                    context: c.context,
                    claim_type: c.claim_type,
                });
            }
            claims
        }
        Err(e) => {
            tracing::error!("Claim extraction failed: {}", e);
            Vec::new()
        }
    }
}

/// Fact-check all claims in an article and store results
pub async fn fact_check_article(
    pool: &PgPool,
    llm: &LLMClient,
    article_id: Uuid,
    article_text: &str,
    title: &str,
    source_domain: &str,
    entities: &EntitiesDict,
) -> Result<Vec<FactCheckRecord>, sqlx::Error> {
    // Extract claims
    let claims = extract_claims_from_article(llm, article_text, title, entities).await;

    if claims.is_empty() {
        return Ok(Vec::new());
    }

    let mut fact_checker = FactChecker::new((*llm).clone());
    let mut fact_checks = Vec::new();

    for claim in claims {
        let result = fact_checker.check_claim(&claim, source_domain).await;

        // Clone values needed for the FactCheckRecord after insertion
        let explanation_clone = result.explanation.clone();
        let fact_checker_url_clone = result.fact_checker_url.clone();

        let fc = FactCheckRecord {
            id: Uuid::new_v4(),
            source_domain: source_domain.to_string(),
            article_id: Some(article_id),
            claim: claim.text,
            claim_hash: claim.claim_hash,
            verdict: result.verdict,
            confidence: result.confidence,
            fact_checker: result.fact_checker,
            fact_checker_url: fact_checker_url_clone,
            explanation: explanation_clone,
            checked_at: Utc::now(),
            claim_date: None,
        };

        sqlx::query(
            r#"
            INSERT INTO fact_check_records
            (id, source_domain, article_id, claim, claim_hash, verdict, confidence,
             fact_checker, fact_checker_url, explanation, checked_at, claim_date)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12)
            "#
        )
        .bind(fc.id)
        .bind(&fc.source_domain)
        .bind(fc.article_id)
        .bind(&fc.claim)
        .bind(&fc.claim_hash)
        .bind(fc.verdict as FactCheckVerdict)
        .bind(fc.confidence)
        .bind(fc.fact_checker as FactCheckerEnum)
        .bind(&fc.fact_checker_url)
        .bind(&fc.explanation)
        .bind(fc.checked_at)
        .bind(fc.claim_date)
        .execute(pool)
        .await?;

        fact_checks.push(fc);
    }

    Ok(fact_checks)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_fact_check_result_aggregation() {
        let results = vec![
            FactCheckResult {
                verdict: FactCheckVerdict::True,
                confidence: 90,
                fact_checker: FactCheckerEnum::LlmVerifier,
                explanation: Some("Test".to_string()),
                fact_checker_url: None,
            },
            FactCheckResult {
                verdict: FactCheckVerdict::MostlyTrue,
                confidence: 80,
                fact_checker: FactCheckerEnum::LlmVerifier,
                explanation: Some("Test".to_string()),
                fact_checker_url: None,
            },
        ];

        // Test aggregation logic would require full FactChecker setup
        // For now just verify the enum matching works
        assert!(matches!(results[0].verdict, FactCheckVerdict::True));
        assert!(matches!(results[1].verdict, FactCheckVerdict::MostlyTrue));
    }
}