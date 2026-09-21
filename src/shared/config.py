from functools import lru_cache
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # Database
    database_url: str = ""

    # LLM providers
    groq_api_key: str = ""
    groq_model: str = "openai/gpt-oss-20b"
    cerebras_api_key: str = ""
    cerebras_model: str = "gpt-oss-120b"

    # Reddit
    reddit_client_id: str = ""
    reddit_client_secret: str = ""
    reddit_user_agent: str = "news-pipeline/0.1 (by /u/valk_db)"

    # YouTube (optional)
    youtube_api_key: str = ""

    # Supabase (optional direct)
    supabase_url: str = ""
    supabase_anon_key: str = ""

    # Ingestion settings
    rss_fetch_timeout: int = 30
    max_articles_per_feed: int = 50
    gdelt_throttle_seconds: float = 5.0
    gdelt_circuit_breaker_threshold: int = 3
    article_cache_hours: int = 24
    gdelt_enabled: bool = True
    rss_max_retries: int = 3
    rss_retry_delay: float = 5.0
    gdelt_max_retries: int = 7
    gdelt_base_delay: float = 10.0

    # Curation UI auth
    curation_user: str = ""
    curation_password: str = ""
    curation_enabled: bool = True

    # Verification settings
    containment_threshold: float = 0.9
    min_reporting_units_per_story: int = 2
    top_n_entities: int = 3

    # Scheduling
    cron_schedule: str = "0 6,18 * * *"  # 6 AM and 6 PM UTC

    # Feature availability checks
    @property
    def has_database(self) -> bool:
        return bool(self.database_url and self.database_url.strip())

    @property
    def has_groq(self) -> bool:
        return bool(self.groq_api_key and self.groq_api_key.strip())

    @property
    def has_cerebras(self) -> bool:
        return bool(self.cerebras_api_key and self.cerebras_api_key.strip())

    @property
    def has_llm(self) -> bool:
        return self.has_groq or self.has_cerebras

    @property
    def has_reddit(self) -> bool:
        return bool(self.reddit_client_id and self.reddit_client_secret and
                    self.reddit_client_id.strip() and self.reddit_client_secret.strip())

    @property
    def has_youtube(self) -> bool:
        return bool(self.youtube_api_key and self.youtube_api_key.strip())

    @property
    def has_supabase(self) -> bool:
        return bool(self.supabase_url and self.supabase_anon_key and
                    self.supabase_url.strip() and self.supabase_anon_key.strip())

    @property
    def has_curation_auth(self) -> bool:
        return bool(self.curation_user and self.curation_user.strip() and
                    self.curation_password and self.curation_password.strip())

    @property
    def has_curation(self) -> bool:
        return self.curation_enabled and self.has_curation_auth

    def missing_required_for(self, feature: str) -> list[str]:
        """Return list of missing env vars for a given feature."""
        missing = []
        if feature == "database" and not self.has_database:
            missing.append("DATABASE_URL")
        elif feature == "llm" and not self.has_llm:
            missing.extend(["GROQ_API_KEY", "CEREBRAS_API_KEY"])
        elif feature == "reddit" and not self.has_reddit:
            missing.extend(["REDDIT_CLIENT_ID", "REDDIT_CLIENT_SECRET"])
        elif feature == "youtube" and not self.has_youtube:
            missing.append("YOUTUBE_API_KEY")
        elif feature == "supabase" and not self.has_supabase:
            missing.extend(["SUPABASE_URL", "SUPABASE_ANON_KEY"])
        return missing


@lru_cache
def get_settings() -> Settings:
    return Settings()