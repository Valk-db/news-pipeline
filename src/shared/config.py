from functools import lru_cache
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # Database
    database_url: str

    # LLM providers
    groq_api_key: str
    groq_model: str = "llama-3.3-70b-versatile"
    cerebras_api_key: str = ""
    cerebras_model: str = "gpt-oss-120b"

    # Reddit
    reddit_client_id: str
    reddit_client_secret: str
    reddit_user_agent: str = "news-pipeline/0.1"

    # YouTube (optional)
    youtube_api_key: str = ""

    # Supabase (optional direct)
    supabase_url: str = ""
    supabase_anon_key: str = ""

    # Ingestion settings
    rss_fetch_timeout: int = 30
    max_articles_per_feed: int = 50
    gdelt_throttle_seconds: float = 5.0
    article_cache_hours: int = 24

    # Verification settings
    containment_threshold: float = 0.9
    min_reporting_units_per_story: int = 2
    top_n_entities: int = 3

    # Scheduling
    cron_schedule: str = "0 6,18 * * *"  # 6 AM and 6 PM UTC


@lru_cache
def get_settings() -> Settings:
    return Settings()