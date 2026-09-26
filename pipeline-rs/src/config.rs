use serde::Deserialize;
use dotenvy::dotenv;

#[derive(Debug, Deserialize, Clone)]
pub struct Settings {
    // Database
    pub database_url: String,

    // LLM providers
    pub groq_api_key: String,
    #[serde(default = "default_groq_model")]
    pub groq_model: String,
    pub cerebras_api_key: String,
    #[serde(default = "default_cerebras_model")]
    pub cerebras_model: String,

    // Reddit (public RSS, no API credentials needed)
    #[serde(default = "default_reddit_user_agent")]
    pub reddit_user_agent: String,

    // YouTube (optional)
    #[serde(default)]
    pub youtube_api_key: String,

    // Supabase (optional direct)
    #[serde(default)]
    pub supabase_url: String,
    #[serde(default)]
    pub supabase_anon_key: String,

    // Ingestion settings
    #[serde(default = "default_rss_fetch_timeout")]
    pub rss_fetch_timeout: i64,
    #[serde(default = "default_max_articles_per_feed")]
    pub max_articles_per_feed: i64,
    #[serde(default = "default_gdelt_throttle_seconds")]
    pub gdelt_throttle_seconds: f64,
    #[serde(default = "default_gdelt_circuit_breaker_threshold")]
    pub gdelt_circuit_breaker_threshold: i64,
    #[serde(default = "default_article_cache_hours")]
    pub article_cache_hours: i64,
    #[serde(default = "default_gdelt_enabled")]
    pub gdelt_enabled: bool,
    #[serde(default = "default_rss_max_retries")]
    pub rss_max_retries: i64,
    #[serde(default = "default_rss_retry_delay")]
    pub rss_retry_delay: f64,
    #[serde(default = "default_gdelt_max_retries")]
    pub gdelt_max_retries: i64,
    #[serde(default = "default_gdelt_base_delay")]
    pub gdelt_base_delay: f64,

    // LLM budget (sized under provider's published cap for headroom)
    #[serde(default = "default_groq_daily_request_budget")]
    pub groq_daily_request_budget: i64,

    // Tiered ingestion schedules (cron expressions)
    #[serde(default = "default_tier1_schedule")]
    pub tier1_schedule: String,
    #[serde(default = "default_tier2_schedule")]
    pub tier2_schedule: String,
    #[serde(default = "default_tier3_schedule")]
    pub tier3_schedule: String,
    #[serde(default = "default_tier4_schedule")]
    pub tier4_schedule: String,

    // Curation UI auth
    #[serde(default)]
    pub curation_user: String,
    #[serde(default)]
    pub curation_password: String,

    // Verification settings
    #[serde(default = "default_containment_threshold")]
    pub containment_threshold: f64,
    #[serde(default = "default_min_reporting_units_per_story")]
    pub min_reporting_units_per_story: i64,
    #[serde(default = "default_top_n_entities")]
    pub top_n_entities: i64,

    // Scheduling
    #[serde(default = "default_cron_schedule")]
    pub cron_schedule: String,

    // Dynamic gate (P3-C) - feature flag, default off for shadow mode
    #[serde(default)]
    pub dynamic_gate_enabled: bool,
}

fn default_groq_model() -> String {
    "openai/gpt-oss-20b".to_string()
}

fn default_cerebras_model() -> String {
    "gpt-oss-120b".to_string()
}

fn default_reddit_user_agent() -> String {
    "news-pipeline/0.1 (by /u/valk_db)".to_string()
}

fn default_rss_fetch_timeout() -> i64 {
    30
}

fn default_max_articles_per_feed() -> i64 {
    50
}

fn default_gdelt_throttle_seconds() -> f64 {
    5.0
}

fn default_gdelt_circuit_breaker_threshold() -> i64 {
    3
}

fn default_article_cache_hours() -> i64 {
    24
}

fn default_gdelt_enabled() -> bool {
    true
}

fn default_rss_max_retries() -> i64 {
    3
}

fn default_rss_retry_delay() -> f64 {
    5.0
}

fn default_gdelt_max_retries() -> i64 {
    7
}

fn default_gdelt_base_delay() -> f64 {
    10.0
}

fn default_groq_daily_request_budget() -> i64 {
    900
}

fn default_tier1_schedule() -> String {
    "0 * * * *".to_string()
}

fn default_tier2_schedule() -> String {
    "0 */4 * * *".to_string()
}

fn default_tier3_schedule() -> String {
    "0 6 * * *".to_string()
}

fn default_tier4_schedule() -> String {
    "0 */6 * * *".to_string()
}

fn default_containment_threshold() -> f64 {
    0.9
}

fn default_min_reporting_units_per_story() -> i64 {
    2
}

fn default_top_n_entities() -> i64 {
    3
}

fn default_cron_schedule() -> String {
    "0 6,18 * * *".to_string()
}

impl Settings {
    pub fn from_env() -> Result<Self, config::ConfigError> {
        // Load .env file if it exists
        dotenv().ok();

        // Use config crate to load from environment
        let mut builder = config::Config::builder()
            .add_source(config::Environment::default().separator("__"));

        builder.build()?.try_deserialize()
    }

    pub fn has_database(&self) -> bool {
        !self.database_url.trim().is_empty()
    }

    pub fn has_groq(&self) -> bool {
        !self.groq_api_key.trim().is_empty()
    }

    pub fn has_cerebras(&self) -> bool {
        !self.cerebras_api_key.trim().is_empty()
    }

    pub fn has_llm(&self) -> bool {
        self.has_groq() || self.has_cerebras()
    }

    pub fn has_youtube(&self) -> bool {
        !self.youtube_api_key.trim().is_empty()
    }

    pub fn has_supabase(&self) -> bool {
        !self.supabase_url.trim().is_empty()
            && !self.supabase_anon_key.trim().is_empty()
            && !self.supabase_url.trim().is_empty()
            && !self.supabase_anon_key.trim().is_empty()
    }

    pub fn has_curation_auth(&self) -> bool {
        !self.curation_user.trim().is_empty()
            && !self.curation_password.trim().is_empty()
            && !self.curation_user.trim().is_empty()
            && !self.curation_password.trim().is_empty()
    }

    pub fn missing_required_for(&self, feature: &str) -> Vec<String> {
        let mut missing = Vec::new();
        match feature {
            "database" if !self.has_database() => {
                missing.push("DATABASE_URL".to_string());
            }
            "llm" if !self.has_llm() => {
                missing.push("GROQ_API_KEY".to_string());
                missing.push("CEREBRAS_API_KEY".to_string());
            }
            "youtube" if !self.has_youtube() => {
                missing.push("YOUTUBE_API_KEY".to_string());
            }
            "supabase" if !self.has_supabase() => {
                missing.push("SUPABASE_URL".to_string());
                missing.push("SUPABASE_ANON_KEY".to_string());
            }
            _ => {}
        }
        missing
    }
}

// Need to add config crate
#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_defaults() {
        let settings = Settings {
            database_url: "postgresql://test".to_string(),
            groq_api_key: "test".to_string(),
            groq_model: default_groq_model(),
            cerebras_api_key: "test".to_string(),
            cerebras_model: default_cerebras_model(),
            reddit_user_agent: default_reddit_user_agent(),
            youtube_api_key: String::new(),
            supabase_url: String::new(),
            supabase_anon_key: String::new(),
            rss_fetch_timeout: default_rss_fetch_timeout(),
            max_articles_per_feed: default_max_articles_per_feed(),
            gdelt_throttle_seconds: default_gdelt_throttle_seconds(),
            gdelt_circuit_breaker_threshold: default_gdelt_circuit_breaker_threshold(),
            article_cache_hours: default_article_cache_hours(),
            gdelt_enabled: default_gdelt_enabled(),
            rss_max_retries: default_rss_max_retries(),
            rss_retry_delay: default_rss_retry_delay(),
            gdelt_max_retries: default_gdelt_max_retries(),
            gdelt_base_delay: default_gdelt_base_delay(),
            groq_daily_request_budget: default_groq_daily_request_budget(),
            tier1_schedule: default_tier1_schedule(),
            tier2_schedule: default_tier2_schedule(),
            tier3_schedule: default_tier3_schedule(),
            tier4_schedule: default_tier4_schedule(),
            curation_user: String::new(),
            curation_password: String::new(),
            containment_threshold: default_containment_threshold(),
            min_reporting_units_per_story: default_min_reporting_units_per_story(),
            top_n_entities: default_top_n_entities(),
            cron_schedule: default_cron_schedule(),
            dynamic_gate_enabled: false,
        };

        assert!(settings.has_database());
        assert!(settings.has_groq());
        assert!(settings.has_cerebras());
        assert!(settings.has_llm());
        assert!(!settings.has_youtube());
        assert!(!settings.has_supabase());
        assert!(!settings.has_curation_auth());
    }
}