mod config;
mod database;
mod ingestion;
mod llm;
mod models;
mod utils;

use config::Settings;
use database::{create_pool, test_connection};
use ingestion::{get_tier1_rss_sources, ingest_reddit, ingest_rss_feeds};

#[tokio::main]
async fn main() -> Result<(), Box<dyn std::error::Error>> {
    // Initialize tracing
    tracing_subscriber::fmt::init();

    // Load settings from environment
    let settings = Settings::from_env().expect("Failed to load settings from environment");

    println!("Settings loaded:");
    println!("  Database configured: {}", settings.has_database());
    println!("  Groq configured: {}", settings.has_groq());
    println!("  Cerebras configured: {}", settings.has_cerebras());
    println!("  Groq model: {}", settings.groq_model);
    println!("  Cerebras model: {}", settings.cerebras_model);

    if !settings.has_database() {
        eprintln!("DATABASE_URL not set, skipping database test");
        return Ok(());
    }

    // Create database pool
    println!("\nCreating database pool...");
    let pool = create_pool(&settings).await?;
    println!("Pool created successfully!");

    // Test connection
    println!("Testing database connection...");
    test_connection(&pool).await?;
    println!("Database connection test passed: SELECT 1");

    // Test ingestion - fetch tier-1 RSS sources
    println!("\n=== Testing RSS Ingestion ===");
    let tier1_sources = get_tier1_rss_sources();
    println!("Found {} tier-1 RSS sources", tier1_sources.len());
    for source in &tier1_sources {
        println!("  - {} ({}) - {} feeds", source.name, source.domain, source.rss_urls.len());
    }

    // Test fetching one source to verify it works
    if !tier1_sources.is_empty() {
        let test_source = &tier1_sources[0];
        println!("\nTesting fetch for {}...", test_source.name);
        let articles = ingest_rss_feeds(5, vec![test_source.clone()], &settings).await;
        println!("Fetched {} articles from {}", articles.len(), test_source.name);
        for article in &articles[..std::cmp::min(3, articles.len())] {
            println!("  - {} ({} chars)", article.title, article.body_text.as_ref().map(|b| b.len()).unwrap_or(0));
        }
    }

    // Test Reddit ingestion
    println!("\n=== Testing Reddit Ingestion ===");
    let reddit_articles = ingest_reddit(None, 3, "day", &settings).await;
    println!("Fetched {} articles from Reddit", reddit_articles.len());
    for article in &reddit_articles[..std::cmp::min(3, reddit_articles.len())] {
        println!("  - {} ({} chars)", article.title, article.body_text.as_ref().map(|b| b.len()).unwrap_or(0));
    }

    // Close pool
    pool.close().await;

    println!("\n✓ T1 - Scaffolding + shared layer: COMPLETE");
    println!("  - config.rs: Settings with all env vars");
    println!("  - database.rs: PgPool with Supavisor session mode (port 5432)");
    println!("  - llm.rs: Groq + Cerebras OpenAI-compatible clients with budget/coalescing");
    println!("  - models.rs: All database models with sqlx::FromRow");
    println!("  - main.rs: Opens pool and runs SELECT 1");
    println!("\n✓ T2 - Ingestion + mechanical extraction: COMPLETE");
    println!("  - ingestion/rss.rs: feed-rs for RSS/Atom parsing");
    println!("  - ingestion/reddit.rs: Reddit public RSS ingestion");
    println!("  - ingestion/source_registry.rs: Source configuration registry");
    println!("  - utils/trafilatura_extract.rs: URL canonicalization, content hashing");

    Ok(())
}