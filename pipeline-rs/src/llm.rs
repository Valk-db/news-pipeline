use crate::config::Settings;
use reqwest::Client;
use serde::{Deserialize, Serialize};
use std::collections::HashMap;
use std::sync::Arc;
use tokio::sync::Mutex;
use chrono::Utc;

/// LLM-specific errors
#[derive(Debug, thiserror::Error)]
pub enum LLMError {
    #[error("HTTP request failed: {0}")]
    RequestFailed(#[from] reqwest::Error),
    #[error("API error: {0}")]
    ApiError(String),
    #[error("Budget exhausted: {0}")]
    BudgetExhausted(String),
    #[error("JSON parsing failed: {0}")]
    JsonParseError(#[from] serde_json::Error),
    #[error("No LLM provider available")]
    NoProviderAvailable,
    #[error("Invalid response format")]
    InvalidResponse,
}

// Manual Clone implementation for LLMError - needed for budget coalescing
impl Clone for LLMError {
    fn clone(&self) -> Self {
        match self {
            LLMError::RequestFailed(e) => LLMError::ApiError(e.to_string()),
            LLMError::ApiError(s) => LLMError::ApiError(s.clone()),
            LLMError::BudgetExhausted(s) => LLMError::BudgetExhausted(s.clone()),
            LLMError::JsonParseError(e) => LLMError::ApiError(e.to_string()),
            LLMError::NoProviderAvailable => LLMError::NoProviderAvailable,
            LLMError::InvalidResponse => LLMError::InvalidResponse,
        }
    }
}

/// Chat message format compatible with OpenAI API
#[derive(Debug, Clone, Hash, Serialize, Deserialize)]
pub struct ChatMessage {
    pub role: String,
    pub content: String,
}

/// Chat completion request
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ChatCompletionRequest {
    pub model: String,
    pub messages: Vec<ChatMessage>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub max_tokens: Option<u32>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub temperature: Option<f32>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub response_format: Option<ResponseFormat>,
}

/// Response format for structured output
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ResponseFormat {
    #[serde(rename = "type")]
    pub format_type: String,
}

/// Chat completion response
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ChatCompletionResponse {
    pub choices: Vec<Choice>,
    pub usage: Option<Usage>,
}

/// Choice in chat completion
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Choice {
    pub message: ChatMessage,
    pub index: u32,
    pub finish_reason: Option<String>,
}

/// Token usage
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Usage {
    pub prompt_tokens: u32,
    pub completion_tokens: u32,
    pub total_tokens: u32,
}

/// Daily request budget with in-flight coalescing
pub struct RequestBudget {
    daily_limit: u32,
    count: u32,
    day: chrono::NaiveDate,
    inflight: Arc<Mutex<HashMap<String, Arc<tokio::sync::Notify>>>>,
    results: Arc<Mutex<HashMap<String, Result<ChatCompletionResponse, LLMError>>>>,
}

impl RequestBudget {
    pub fn new(daily_limit: u32) -> Self {
        Self {
            daily_limit,
            count: 0,
            day: Utc::now().date_naive(),
            inflight: Arc::new(Mutex::new(HashMap::new())),
            results: Arc::new(Mutex::new(HashMap::new())),
        }
    }

    fn roll_if_new_day(&mut self) {
        let today = Utc::now().date_naive();
        if today != self.day {
            self.day = today;
            self.count = 0;
        }
    }

    pub fn status(&mut self) -> BudgetStatus {
        self.roll_if_new_day();
        let remaining = self.daily_limit.saturating_sub(self.count);
        BudgetStatus {
            used_today: self.count,
            limit: self.daily_limit,
            remaining,
            exhausted: remaining == 0,
        }
    }

    fn coalesce_key(messages: &[ChatMessage], model: &str) -> String {
        use std::hash::{Hash, Hasher};
        let mut hasher = std::collections::hash_map::DefaultHasher::new();
        messages.hash(&mut hasher);
        model.hash(&mut hasher);
        format!("{:x}", hasher.finish())
    }

    /// Check if we can make a request, and if there's an identical in-flight request,
    /// wait for its result instead of making a duplicate request.
    pub async fn try_acquire(
        &mut self,
        messages: &[ChatMessage],
        model: &str,
    ) -> Result<Option<ChatCompletionResponse>, LLMError> {
        self.roll_if_new_day();
        let key = Self::coalesce_key(messages, model);

        // Check for in-flight duplicate
        let mut inflight = self.inflight.lock().await;
        if let Some(notify) = inflight.get(&key) {
            // Wait for the in-flight request to complete
            let notify = notify.clone();
            drop(inflight);
            notify.notified().await;

            // Get the result
            let mut results = self.results.lock().await;
            if let Some(result) = results.remove(&key) {
                return Ok(Some(result?));
            }
            return Err(LLMError::InvalidResponse);
        }

        // Check budget
        if self.count >= self.daily_limit {
            return Err(LLMError::BudgetExhausted(format!(
                "Groq daily budget exhausted: {}/{}",
                self.count, self.daily_limit
            )));
        }

        // Register this request as in-flight
        let notify = Arc::new(tokio::sync::Notify::new());
        inflight.insert(key.clone(), notify.clone());
        drop(inflight);

        Ok(None)
    }

    /// Complete a request, store result, and notify waiters
    pub async fn complete(
        &mut self,
        messages: &[ChatMessage],
        model: &str,
        result: Result<ChatCompletionResponse, LLMError>,
    ) {
        self.roll_if_new_day();
        let key = Self::coalesce_key(messages, model);

        // Increment count on success
        if result.is_ok() {
            self.count += 1;
        }

        // Store result and notify
        let mut results = self.results.lock().await;
        results.insert(key.clone(), result);

        let mut inflight = self.inflight.lock().await;
        if let Some(notify) = inflight.remove(&key) {
            notify.notify_waiters();
        }
    }
}

/// Budget status
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct BudgetStatus {
    pub used_today: u32,
    pub limit: u32,
    pub remaining: u32,
    pub exhausted: bool,
}

/// LLM Client with Groq primary, Cerebras fallback
#[derive(Clone)]
pub struct LLMClient {
    settings: Arc<Settings>,
    client: Client,
    groq_base_url: String,
    cerebras_base_url: String,
    budget: Arc<Mutex<RequestBudget>>,
}

impl LLMClient {
    pub fn new(settings: Settings) -> Self {
        let settings = Arc::new(settings);
        Self {
            settings: Arc::clone(&settings),
            client: Client::new(),
            groq_base_url: "https://api.groq.com/openai/v1".to_string(),
            cerebras_base_url: "https://api.cerebras.ai/v1".to_string(),
            budget: Arc::new(Mutex::new(RequestBudget::new(
                settings.groq_daily_request_budget as u32
            ))),
        }
    }

    /// Get budget status
    pub async fn budget_status(&self) -> BudgetStatus {
        self.budget.lock().await.status()
    }

    /// Make a chat completion request to Groq
    async fn chat_completion_groq(
        &self,
        request: ChatCompletionRequest,
    ) -> Result<ChatCompletionResponse, LLMError> {
        if !self.settings.has_groq() {
            return Err(LLMError::ApiError("Groq API key not configured".to_string()));
        }

        let url = format!("{}/chat/completions", self.groq_base_url);
        let response = self
            .client
            .post(&url)
            .bearer_auth(&self.settings.groq_api_key)
            .json(&request)
            .send()
            .await?;

        if !response.status().is_success() {
            let status = response.status();
            let error_text = response.text().await.unwrap_or_default();
            return Err(LLMError::ApiError(format!(
                "Groq API error {}: {}",
                status, error_text
            )));
        }

        let chat_response: ChatCompletionResponse = response.json().await?;
        Ok(chat_response)
    }

    /// Make a chat completion request to Cerebras
    async fn chat_completion_cerebras(
        &self,
        request: ChatCompletionRequest,
    ) -> Result<ChatCompletionResponse, LLMError> {
        if !self.settings.has_cerebras() {
            return Err(LLMError::ApiError("Cerebras API key not configured".to_string()));
        }

        let url = format!("{}/chat/completions", self.cerebras_base_url);
        let response = self
            .client
            .post(&url)
            .bearer_auth(&self.settings.cerebras_api_key)
            .json(&request)
            .send()
            .await?;

        if !response.status().is_success() {
            let status = response.status();
            let error_text = response.text().await.unwrap_or_default();
            return Err(LLMError::ApiError(format!(
                "Cerebras API error {}: {}",
                status, error_text
            )));
        }

        let chat_response: ChatCompletionResponse = response.json().await?;
        Ok(chat_response)
    }

    /// General chat completion with budget management and provider fallback
    pub async fn chat_completion(
        &self,
        messages: Vec<ChatMessage>,
        max_tokens: Option<u32>,
        temperature: Option<f32>,
        response_format: Option<ResponseFormat>,
    ) -> Result<ChatCompletionResponse, LLMError> {
        let model = self.settings.groq_model.clone();

        // Try to acquire budget slot (handles coalescing)
        let mut budget = self.budget.lock().await;
        match budget.try_acquire(&messages, &model).await? {
            Some(response) => return Ok(response), // Got result from coalesced request
            None => {} // Need to make the request
        }
        drop(budget);

        let request = ChatCompletionRequest {
            model: model.clone(),
            messages: messages.clone(),
            max_tokens,
            temperature,
            response_format,
        };

        // Try Groq first
        let groq_result = if self.settings.has_groq() {
            self.chat_completion_groq(request.clone()).await
        } else {
            Err(LLMError::ApiError("Groq not configured".to_string()))
        };

        let result = match groq_result {
            Ok(response) => Ok(response),
            Err(e) => {
                // Check if it's a budget error
                if matches!(e, LLMError::BudgetExhausted(_)) {
                    Err(e)
                } else {
                    // Log warning and try Cerebras
                    eprintln!("Groq failed: {}, trying Cerebras fallback", e);
                    self.chat_completion_cerebras(request).await
                }
            }
        };

        // Complete budget tracking
        let mut budget = self.budget.lock().await;
        budget.complete(&messages, &model, result.clone()).await;

        result
    }

    /// Convenience method for simple text completion
    pub async fn complete(
        &self,
        system_prompt: &str,
        user_prompt: &str,
        max_tokens: Option<u32>,
        temperature: Option<f32>,
    ) -> Result<String, LLMError> {
        let messages = vec![
            ChatMessage {
                role: "system".to_string(),
                content: system_prompt.to_string(),
            },
            ChatMessage {
                role: "user".to_string(),
                content: user_prompt.to_string(),
            },
        ];

        let response = self
            .chat_completion(messages, max_tokens, temperature, None)
            .await?;

        response
            .choices
            .first()
            .map(|c| c.message.content.clone())
            .ok_or(LLMError::InvalidResponse)
    }

    /// Generate JSON-structured completion
    pub async fn complete_json<T: for<'de> Deserialize<'de>>(
        &self,
        system_prompt: &str,
        user_prompt: &str,
        max_tokens: Option<u32>,
        temperature: Option<f32>,
    ) -> Result<T, LLMError> {
        let messages = vec![
            ChatMessage {
                role: "system".to_string(),
                content: system_prompt.to_string(),
            },
            ChatMessage {
                role: "user".to_string(),
                content: user_prompt.to_string(),
            },
        ];

        let response = self
            .chat_completion(
                messages,
                max_tokens,
                temperature,
                Some(ResponseFormat {
                    format_type: "json_object".to_string(),
                }),
            )
            .await?;

        let content = response
            .choices
            .first()
            .map(|c| c.message.content.clone())
            .ok_or(LLMError::InvalidResponse)?;

        // Parse JSON, handling potential code fences
        let json_str = if content.trim().starts_with("```") {
            content
                .split("```")
                .nth(1)
                .and_then(|s| s.strip_prefix("json"))
                .map(|s| s.trim())
                .unwrap_or(&content)
        } else {
            &content
        };

        serde_json::from_str(json_str).map_err(LLMError::JsonParseError)
    }
}

/// Platform character limits for caption validation
pub const PLATFORM_LIMITS: &[(&str, usize)] = &[
    ("twitter", 280),
    ("x", 280),
    ("bluesky", 300),
    ("threads", 500),
    ("instagram", 2200),
    ("linkedin", 3000),
    ("facebook", 63206),
];

/// Extract word n-grams from text
fn extract_ngrams(text: &str, n: usize) -> std::collections::HashSet<String> {
    let lower = text.to_lowercase();
    let words: Vec<&str> = lower.split_whitespace().collect();
    if words.len() < n {
        return std::collections::HashSet::new();
    }
    words
        .windows(n)
        .map(|w| w.join(" "))
        .collect()
}

/// Validate a generated caption
pub fn validate_caption(
    caption: &str,
    platform: &str,
    source_texts: &[String],
    min_ngram_overlap: usize,
) -> Result<(), String> {
    if caption.trim().is_empty() {
        return Err("Caption is empty".to_string());
    }

    let caption = caption.trim();

    // Check character limit
    let limit = PLATFORM_LIMITS
        .iter()
        .find(|(p, _)| *p == platform.to_lowercase())
        .map(|(_, l)| *l)
        .unwrap_or(280);

    if caption.len() > limit {
        return Err(format!(
            "Caption exceeds {} limit of {} characters ({})",
            platform, limit, caption.len()
        ));
    }

    // Check paraphrase constraint - n-gram overlap with source texts
    let caption_ngrams = extract_ngrams(caption, min_ngram_overlap);
    if !caption_ngrams.is_empty() {
        for source_text in source_texts {
            if source_text.is_empty() {
                continue;
            }
            let source_ngrams = extract_ngrams(source_text, min_ngram_overlap);
            if !source_ngrams.is_empty() {
                let overlap: Vec<&String> = caption_ngrams.intersection(&source_ngrams).collect();
                if !overlap.is_empty() {
                    return Err(format!(
                        "Caption shares {} n-gram(s) with source text (min: {} words): {}",
                        overlap.len(),
                        min_ngram_overlap,
                        overlap.iter().take(3).map(|s| s.as_str()).collect::<Vec<_>>().join(", ")
                    ));
                }
            }
        }
    }

    Ok(())
}