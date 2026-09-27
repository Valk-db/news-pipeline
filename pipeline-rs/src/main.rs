use pipeline_rs::config::Settings;
use pipeline_rs::database::{create_pool, test_connection};
use pipeline_rs::ingestion::run_ingestion;
use pipeline_rs::verification::{units::build_reporting_units, stories::build_stories, tiers::apply_dynamic_gate};
use sqlx::Row;

#[tokio::main]
async fn main() -> Result<(), Box<dyn std::error::Error + Send + Sync>> {
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

    // ===== W1: Run the full pipeline end-to-end =====
    println!("\n=== Running Full Ingestion Pipeline ===");

    // Run ingestion for all tiers (dry_run first to test)
    println!("\n--- Phase 1: Dry run ingestion ---");
    let dry_run_result = run_ingestion(&pool, &settings, true, None).await?;
    println!("Dry run result: {}", serde_json::to_string_pretty(&dry_run_result)?);

    // Run ingestion for real (tier-1 only to start)
    println!("\n--- Phase 1: Real ingestion (tier-1) ---");
    let ingestion_result = run_ingestion(&pool, &settings, false, Some(vec![pipeline_rs::models::SourceTier::Tier1])).await?;
    println!("Ingestion result: {}", serde_json::to_string_pretty(&ingestion_result)?);

    // Build reporting units
    println!("\n--- Phase 2: Build reporting units ---");
    let units_created = build_reporting_units(&pool).await?;
    println!("Reporting units created: {}", units_created);

    // Build stories
    println!("\n--- Phase 3: Build stories ---");
    let modified_story_ids = build_stories(&pool).await?;
    println!("Stories created/modified: {}", modified_story_ids.len());
    for id in &modified_story_ids {
        println!("  Story ID: {}", id);
    }

    // Apply dynamic gate
    println!("\n--- Phase 4: Apply dynamic gate ---");
    let gate_result = apply_dynamic_gate(&pool, Some(modified_story_ids)).await?;
    println!("Gate result: {}", serde_json::to_string_pretty(&gate_result)?);

    // Verify results in database
    println!("\n--- Verification: Query database ---");
    let ru_count: (i64,) = sqlx::query_as("SELECT COUNT(*) FROM reporting_units")
        .fetch_one(&pool)
        .await?;
    println!("Total reporting_units in DB: {}", ru_count.0);

    let story_count: (i64,) = sqlx::query_as("SELECT COUNT(*) FROM stories")
        .fetch_one(&pool)
        .await?;
    println!("Total stories in DB: {}", story_count.0);

    let queued_count: (i64,) = sqlx::query_as("SELECT COUNT(*) FROM stories WHERE status = 'queued'")
        .fetch_one(&pool)
        .await?;
    println!("Queued stories: {}", queued_count.0);

    let blocked_count: (i64,) = sqlx::query_as("SELECT COUNT(*) FROM stories WHERE status = 'blocked'")
        .fetch_one(&pool)
        .await?;
    println!("Blocked stories: {}", blocked_count.0);

    // Show blocked story reasons
    let blocked_stories = sqlx::query("SELECT id, gate_reason FROM stories WHERE status = 'blocked'")
        .fetch_all(&pool)
        .await?;
    for row in blocked_stories {
        let id: uuid::Uuid = row.get("id");
        let reason: Option<String> = row.get("gate_reason");
        println!("  Blocked story {}: {:?}", id, reason);
    }

    // Close pool
    pool.close().await;

    println!("\n✓ W1 - Full pipeline wired and executed: COMPLETE");
    println!("  - run_ingestion() called with real DB");
    println!("  - build_reporting_units() called");
    println!("  - build_stories() called");
    println!("  - apply_dynamic_gate() called");
    println!("  - Row counts and IDs printed above");

    Ok(())
}