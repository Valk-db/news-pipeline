use crate::config::Settings;
use crate::models::SourceTier;
use chrono::{DateTime, Duration, Utc, Timelike};
use std::collections::HashMap;

/// Manages tiered ingestion schedules.
///
/// Tier-1: Hourly (verified editorial standards)
/// Tier-2: Every 4 hours (reputable national/regional)
/// Tier-3: Daily (social, forums)
/// Tier-4: Every 6 hours (niche, newsletters)
pub struct TieredScheduler {
    settings: Settings,
}

impl TieredScheduler {
    pub fn new(settings: Settings) -> Self {
        Self { settings }
    }

    /// Determine if a tier should run based on its schedule.
    pub fn should_run_tier(&self, tier: SourceTier, last_run: Option<DateTime<Utc>>) -> bool {
        if last_run.is_none() {
            return true;
        }

        let now = Utc::now();
        let elapsed_hours = (now - last_run.unwrap()).num_hours() as f64;

        match tier {
            SourceTier::Tier1 => elapsed_hours >= 1.0,  // Hourly
            SourceTier::Tier2 => elapsed_hours >= 4.0,  // Every 4 hours
            SourceTier::Tier3 => elapsed_hours >= 24.0, // Daily
            SourceTier::Tier4 => elapsed_hours >= 6.0,  // Every 6 hours
        }
    }

    /// Get the next scheduled run time for a tier.
    pub fn get_next_run_time(&self, tier: SourceTier, last_run: Option<DateTime<Utc>>) -> DateTime<Utc> {
        let now = Utc::now();

        if last_run.is_none() {
            return now;
        }

        match tier {
            SourceTier::Tier1 => {
                // Next hour boundary
                let mut next = now;
                next = next.with_minute(0).unwrap();
                next = next.with_second(0).unwrap();
                next = next.with_nanosecond(0).unwrap();
                next = next + Duration::hours(1);
                next
            }
            SourceTier::Tier2 => {
                // Next 4-hour boundary (0, 4, 8, 12, 16, 20)
                let current_hour = now.hour();
                let next_boundary = ((current_hour / 4) + 1) * 4;
                if next_boundary >= 24 {
                    let mut next_day = now;
                    next_day = next_day.with_hour(0).unwrap();
                    next_day = next_day.with_minute(0).unwrap();
                    next_day = next_day.with_second(0).unwrap();
                    next_day = next_day.with_nanosecond(0).unwrap();
                    next_day = next_day + Duration::days(1);
                    next_day = next_day.with_hour(next_boundary % 24).unwrap();
                    next_day
                } else {
                    let mut next = now;
                    next = next.with_hour(next_boundary).unwrap();
                    next = next.with_minute(0).unwrap();
                    next = next.with_second(0).unwrap();
                    next = next.with_nanosecond(0).unwrap();
                    next
                }
            }
            SourceTier::Tier3 => {
                // Next 6 AM UTC
                let mut next_run = now;
                next_run = next_run.with_hour(6).unwrap();
                next_run = next_run.with_minute(0).unwrap();
                next_run = next_run.with_second(0).unwrap();
                next_run = next_run.with_nanosecond(0).unwrap();
                if next_run <= now {
                    next_run + Duration::days(1)
                } else {
                    next_run
                }
            }
            SourceTier::Tier4 => {
                // Next 6-hour boundary (0, 6, 12, 18)
                let current_hour = now.hour();
                let next_boundary = ((current_hour / 6) + 1) * 6;
                if next_boundary >= 24 {
                    let mut next_day = now;
                    next_day = next_day.with_hour(0).unwrap();
                    next_day = next_day.with_minute(0).unwrap();
                    next_day = next_day.with_second(0).unwrap();
                    next_day = next_day.with_nanosecond(0).unwrap();
                    next_day = next_day + Duration::days(1);
                    next_day = next_day.with_hour(next_boundary % 24).unwrap();
                    next_day
                } else {
                    let mut next = now;
                    next = next.with_hour(next_boundary).unwrap();
                    next = next.with_minute(0).unwrap();
                    next = next.with_second(0).unwrap();
                    next = next.with_nanosecond(0).unwrap();
                    next
                }
            }
        }
    }

    /// Get the cron expression for a tier from settings.
    pub fn get_cron_expression(&self, tier: SourceTier) -> String {
        match tier {
            SourceTier::Tier1 => self.settings.tier1_schedule.clone(),
            SourceTier::Tier2 => self.settings.tier2_schedule.clone(),
            SourceTier::Tier3 => self.settings.tier3_schedule.clone(),
            SourceTier::Tier4 => self.settings.tier4_schedule.clone(),
        }
    }
}

/// Global scheduler instance
pub fn get_scheduler(settings: Settings) -> TieredScheduler {
    TieredScheduler::new(settings)
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::models::SourceTier;

    #[test]
    fn test_should_run_tier_first_run() {
        let settings = Settings::from_env().unwrap();
        let scheduler = TieredScheduler::new(settings);
        assert!(scheduler.should_run_tier(SourceTier::Tier1, None));
        assert!(scheduler.should_run_tier(SourceTier::Tier2, None));
        assert!(scheduler.should_run_tier(SourceTier::Tier3, None));
        assert!(scheduler.should_run_tier(SourceTier::Tier4, None));
    }

    #[test]
    fn test_tier1_hourly() {
        let settings = Settings::from_env().unwrap();
        let scheduler = TieredScheduler::new(settings);
        let now = Utc::now();
        let hour_ago = now - chrono::Duration::hours(1);
        let two_hours_ago = now - chrono::Duration::hours(2);

        // Should run after 1 hour
        assert!(scheduler.should_run_tier(SourceTier::Tier1, Some(hour_ago)));
        // Should not run after 30 minutes
        let half_hour_ago = now - chrono::Duration::minutes(30);
        assert!(!scheduler.should_run_tier(SourceTier::Tier1, Some(half_hour_ago)));
    }

    #[test]
    fn test_tier2_every_4_hours() {
        let settings = Settings::from_env().unwrap();
        let scheduler = TieredScheduler::new(settings);
        let now = Utc::now();
        let four_hours_ago = now - chrono::Duration::hours(4);
        let five_hours_ago = now - chrono::Duration::hours(5);

        assert!(scheduler.should_run_tier(SourceTier::Tier2, Some(four_hours_ago)));
        assert!(scheduler.should_run_tier(SourceTier::Tier2, Some(five_hours_ago)));
    }
}