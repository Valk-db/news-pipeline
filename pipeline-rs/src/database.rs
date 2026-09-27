use sqlx::{postgres::PgPoolOptions, PgPool as SqlxPgPool, Postgres};
use crate::config::Settings;

/// Type alias for the connection pool
pub type PgPool = sqlx::Pool<Postgres>;

/// Create a database connection pool configured for Supabase via Supavisor session mode
pub async fn create_pool(settings: &Settings) -> Result<PgPool, sqlx::Error> {
    // Use the DATABASE_URL directly - it should be a Supavisor session mode URL (port 5432)
    // The URL should be something like: postgresql://user:pass@aws-0-region.pooler.supabase.com:5432/postgres
    let pool = PgPoolOptions::new()
        .max_connections(3)  // Keep low for GitHub Actions batch process
        .acquire_timeout(std::time::Duration::from_secs(30))
        .idle_timeout(std::time::Duration::from_secs(300))
        .max_lifetime(std::time::Duration::from_secs(600))
        .connect(&settings.database_url)
        .await?;

    // Test the connection
    sqlx::query("SELECT 1").execute(&pool).await?;

    Ok(pool)
}

/// Test database connectivity
pub async fn test_connection(pool: &PgPool) -> Result<(), sqlx::Error> {
    sqlx::query("SELECT 1").execute(pool).await?;
    Ok(())
}