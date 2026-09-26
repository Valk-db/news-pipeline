//! Pipeline-RS: News ingestion and verification pipeline in Rust

pub mod config;
pub mod database;
pub mod ingestion;
pub mod llm;
pub mod models;
pub mod reliability;
pub mod utils;
pub mod verification;