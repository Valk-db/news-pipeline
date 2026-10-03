from functools import lru_cache
from pydantic import Field
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

    # Reddit (public RSS, no API credentials needed)
    reddit_user_agent: str = "news-pipeline/0.1 (by /u/valk_db)"

    # YouTube (optional)
    youtube_api_key: str = ""

    # Supabase (optional direct)
    supabase_url: str = ""
    supabase_anon_key: str = ""

    # Ingestion settings
    # RSS_FETCH_CONCURRENCY bounds the feed-fetch semaphore in src/ingestion/rss.py.
    # ge=1 because Semaphore(0) would deadlock every fetch instead of failing loudly.
    rss_fetch_concurrency: int = Field(default=10, ge=1)
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

    # LLM budget (sized under provider's published cap for headroom). Counted in the
    # database (src/shared/budget.py), so this is a daily cap for the pipeline rather
    # than for one process: two ingest runs a day share one budget.
    groq_daily_request_budget: int = 900

    # Token caps for the same free tier, denominated in the unit that actually binds.
    #
    # Groq's free plan publishes 200,000 tokens/day for the whole key, and that allowance
    # is SHARED by caption/classification, translation and Phase 2. A request cap cannot
    # see it: the measured failure was a day whose `groq_requests` row read 28 -- 97%
    # "unspent" -- while the day's tokens were entirely gone, the request that crossed the
    # line being a 429 reading "TPD: Limit 200000, Used 199337, Requested 4388". So the
    # request caps above stay (they are real limits and other stages spend against them)
    # and these are enforced alongside them.
    #
    # The three are sized as shares of the one 200,000 allowance rather than
    # independently, because independent caps are how three stages each believe they have
    # the whole tier: 60,000 (30%) caption/classification + 20,000 (10%) translation +
    # 40,000 (20%) Phase 2 = 120,000, leaving 80,000 (40%) of headroom. At the measured
    # ~1,200 tokens per caption call, 60,000 is ~50 calls, which is why the 900-request cap
    # is unreachable on this tier rather than merely generous.
    groq_daily_token_budget: int = 60_000
    groq_translation_daily_request_budget: int = 300
    groq_translation_daily_token_budget: int = 20_000
    mymemory_daily_char_budget: int = 45_000

    # Phase 2 (claim extraction on gate-passed PENDING stories) budget and pacing.
    #
    # The cap is in tokens/day because tokens/day is what binds: Groq's free plan
    # for openai/gpt-oss-20b publishes TPM 8,000 / TPD 200,000 against a 1,000
    # requests/day, and a measured claim extraction costs 4,962 total tokens at
    # 10 units (1,790 prompt + 647 completion at the 5-unit setting Phase 2 runs).
    # 40,000 tokens/day is 20% of the published daily allowance, deliberately a
    # slice rather than the whole thing: captions, classification and translation
    # draw on the same free tier, and a cap sized to the provider's limit would
    # starve them instead of protecting them. At the measured 2,437 tokens per
    # story that is ~16 stories a day, which covers the daily inflow of gate-passed
    # stories with room to work the PENDING backlog down. Raise it only with a
    # measurement of what the other counters actually spent that day.
    phase2_daily_token_cap: int = 40_000

    # Units folded into one claim-extraction prompt. 5, not the 10 the stage was
    # originally written with: at 10 units a single call measured 4,962 tokens,
    # which is 62% of the per-minute token allowance for one story, so a handful
    # of consecutive stories would trip the TPM limit rather than the daily cap.
    # At 5 units a call measured 2,437 tokens, so the pacing below keeps a run
    # inside the 8,000 TPM window on purpose instead of by luck.
    phase2_units_per_story: int = Field(default=5, ge=2, le=10)

    # Minimum wall-clock gap between two Phase 2 LLM calls. 2,437 tokens per call
    # against TPM 8,000 allows three calls a minute; 25s allows 2.4, i.e. ~5,850
    # tokens/minute, which leaves headroom for the counting error in the estimate
    # above and for a run that shares the key with another job.
    phase2_min_seconds_between_calls: float = Field(default=25.0, ge=0.0)

    # How long to wait when the provider answers 429/402 with a retry-after, and
    # how many times to honour it. One retry, never a widening sleep: the daily
    # cap is the real limiter, and a request that is still throttled after the
    # window the provider asked for is a story to skip and record, not a run to
    # fail and not a reason to hold the job open.
    phase2_retry_after_seconds: float = Field(default=60.0, ge=0.0)
    phase2_max_rate_limit_retries: int = Field(default=1, ge=0, le=3)

    # How far back one Phase 2 run looks, and how many stories it may take. 48h is
    # the same window the map and the story-grouping use, so a run sees everything
    # ingested since the last two runs. 16 is not a guess: at the measured 2,437
    # tokens per story the 40,000 token cap spends out at exactly 16, so raising it
    # without raising the cap would only move the refusal earlier in the queue.
    phase2_hours_back: int = Field(default=48, ge=1)
    phase2_max_stories_per_run: int = Field(default=16, ge=1, le=200)

    # Tiered ingestion schedules (cron expressions)
    tier1_schedule: str = "0 * * * *"      # Hourly
    tier2_schedule: str = "0 */4 * * *"    # Every 4 hours
    tier3_schedule: str = "0 6 * * *"      # Daily at 6 AM
    tier4_schedule: str = "0 */6 * * *"    # Every 6 hours

    # Curation UI auth
    curation_user: str = ""
    curation_password: str = ""
    # Proxies whose X-Forwarded-For the auth rate limiter may trust, as
    # comma-separated IPs or CIDRs. Empty (the default) means trust nothing and
    # key the limiter on the direct peer address, because a client can send any
    # X-Forwarded-For it likes.
    curation_trusted_proxies: str = ""

    # Verification settings
    containment_threshold: float = 0.9
    min_reporting_units_per_story: int = 2
    top_n_entities: int = Field(default=3, ge=1)

    # Transparency: operator-published public keys the /proof/{id} page verifies
    # checkpoint signatures against. JSON: {"<key_id>": {"algorithm": "ed25519",
    # "public_key": "<64 hex chars>"}}. Empty (the default) means no key is
    # published, and every proof honestly renders "signature unverified" rather
    # than claiming a signed log. See src/transparency/keys.py.
    transparency_trusted_keys: str = ""

    # Bearer token for GET /api/cron/checkpoint. Empty (the default) means the
    # cron route refuses every caller, which is the safe state: an unset token
    # must not mean "no auth required". Compared with secrets.compare_digest and
    # throttled per client by curation_ui/security.FailureLimiter.
    transparency_cron_token: str = ""

    # Seed material for the production Ed25519 signing key. Deliberately a SEED
    # rather than a raw 32-byte key: it is hashed down to 32 bytes by
    # generate_ed25519_signer, so it can come from a password manager entry, and
    # nothing in the repo ever sees the key itself. Empty means the cron route
    # reports that signing is not configured instead of signing with a dev key.
    transparency_signing_key: str = ""

    # The last checkpoint published, recorded OUTSIDE this database, as
    # "<tree_size>:<checkpoint_digest_hex>" (signing._published_head_value).
    # This is the only input to the signer an attacker with database write
    # access cannot move, so it is what makes a rollback, truncation, or deletion
    # of published checkpoints detectable rather than invisible.
    #
    # It is a floor, not a witness. An attacker who can also rewrite the deploy's
    # environment edits this value and the log together, and it buys nothing
    # there. A real witness -- OpenTimestamps, a co-signer we do not control, an
    # archived copy -- is Phase 2 (src/transparency/anchoring.py, an interface
    # and a deliberate stub). Empty means "nothing published yet", which is only
    # acceptable with transparency_genesis_confirmed; the signer refuses
    # otherwise rather than reading an empty checkpoint table as genesis.
    transparency_signed_head: str = ""

    # The operator has explicitly asserted that this log's very first checkpoint
    # is legitimate. Until this is set, a log with no checkpoints refuses:
    # RLS hiding every row, a truncated table, and a genuine first run are
    # indistinguishable from inside the database, and treating all three as
    # "genesis" is what let a rewritten history be re-signed from the start.
    # This is a deliberate, one-time, human decision -- never set it from code
    # that runs on the signing path.
    transparency_genesis_confirmed: bool = False

    # Which log a v2 checkpoint claims to be. The C2SP spec wants a unique,
    # schema-less log identity; it must match the signer's key name.
    transparency_origin: str = "procmon.dev/transparency"

    # How old the newest published checkpoint may get before the watchdog calls
    # the deployment unhealthy. A little over the cron interval so one skipped
    # fire is not an incident, but far under a day so a log that quietly stops
    # being checkpointed is noticed on the next check.
    transparency_max_checkpoint_interval_hours: float = 26.0

    # Optional least-privilege DSN for the signer only (see
    # supabase/migrations/20261002200000_transparency_signer_v2.sql for the role
    # and 20261002230000_transparency_signer_rbac.sql for the grants and policies
    # that make it usable). When set, the cron route uses this instead of
    # DATABASE_URL, so the signing path holds a role with SELECT on the log and
    # on the two transparency tables plus INSERT on those two, and nothing else.
    # Empty falls back to DATABASE_URL, which works but is over-privileged.
    transparency_signer_database_url: str = ""

    # Scheduling
    cron_schedule: str = "0 6,18 * * *"  # 6 AM and 6 PM UTC

    # Dynamic gate (P3-C) - feature flag, default off for shadow mode
    dynamic_gate_enabled: bool = False

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

    
    def missing_required_for(self, feature: str) -> list[str]:
        """Return list of missing env vars for a given feature."""
        missing = []
        if feature == "database" and not self.has_database:
            missing.append("DATABASE_URL")
        elif feature == "llm" and not self.has_llm:
            missing.extend(["GROQ_API_KEY", "CEREBRAS_API_KEY"])
        elif feature == "youtube" and not self.has_youtube:
            missing.append("YOUTUBE_API_KEY")
        elif feature == "supabase" and not self.has_supabase:
            missing.extend(["SUPABASE_URL", "SUPABASE_ANON_KEY"])
        return missing


@lru_cache
def get_settings() -> Settings:
    return Settings()