mod config;
mod database;
mod llm;
mod models;

use config::Settings;
use database::{create_pool, test_connection};

#[tokio::main]
async fn main() -> Result<(), Box<dyn std::error::Error>> {
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

    // Close pool
    pool.close().await;

    println!("\n✓ T1 - Scaffolding + shared layer: COMPLETE");
    println!("  - config.rs: Settings with all env vars");
    println!("  - database.rs: PgPool with Supavisor session mode (port 5432)");
    println!("  - llm.rs: Groq + Cerebras OpenAI-compatible clients with budget/coalescing");
    println!("  - models.rs: All database models with sqlx::FromRow");
    println!("  - main.rs: Opens pool and runs SELECT 1");

    Ok(())
}