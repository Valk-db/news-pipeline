"""Tiered ingestion scheduler for multi-source pipeline."""

from datetime import datetime, timezone, timedelta
from typing import Optional
from src.shared.config import get_settings
from src.ingestion.source_registry import (
    get_enabled_sources_by_tier,
    SourceConfig,
    SourceTier,
)


class TieredScheduler:
    """
    Manages tiered ingestion schedules.

    Tier-1: Hourly (verified editorial standards)
    Tier-2: Every 4 hours (reputable national/regional)
    Tier-3: Daily (social, forums)
    Tier-4: Every 6 hours (niche, newsletters)
    """

    def __init__(self):
        self.settings = get_settings()

    def should_run_tier(self, tier: SourceTier, last_run: Optional[datetime] = None) -> bool:
        """
        Determine if a tier should run based on its schedule.

        Args:
            tier: The source tier to check
            last_run: Last run time for this tier (None = never run)

        Returns:
            True if the tier should run now
        """
        if last_run is None:
            return True

        now = datetime.now(timezone.utc)
        elapsed_hours = (now - last_run).total_seconds() / 3600

        if tier == SourceTier.TIER1:
            return elapsed_hours >= 1  # Hourly
        elif tier == SourceTier.TIER2:
            return elapsed_hours >= 4  # Every 4 hours
        elif tier == SourceTier.TIER3:
            return elapsed_hours >= 24  # Daily
        elif tier == SourceTier.TIER4:
            return elapsed_hours >= 6  # Every 6 hours

        return False

    def get_sources_for_tier(self, tier: SourceTier) -> dict[str, SourceConfig]:
        """Get enabled sources for a specific tier."""
        return get_enabled_sources_by_tier(tier)

    def get_next_run_time(self, tier: SourceTier, last_run: Optional[datetime] = None) -> datetime:
        """Get the next scheduled run time for a tier."""
        now = datetime.now(timezone.utc)

        if tier == SourceTier.TIER1:
            # Next hour boundary
            return now.replace(minute=0, second=0, microsecond=0) + \
                (timedelta(hours=1) if last_run is None else timedelta(hours=1))
        elif tier == SourceTier.TIER2:
            # Next 4-hour boundary (0, 4, 8, 12, 16, 20)
            current_hour = now.hour
            next_boundary = ((current_hour // 4) + 1) * 4
            if next_boundary >= 24:
                next_boundary = 0
                next_day = now.replace(hour=0, minute=0, second=0, microsecond=0) + \
                    timedelta(days=1)
                return next_day.replace(hour=next_boundary)
            return now.replace(hour=next_boundary, minute=0, second=0, microsecond=0)
        elif tier == SourceTier.TIER3:
            # Next 6 AM UTC
            next_run = now.replace(hour=6, minute=0, second=0, microsecond=0)
            if next_run <= now:
                next_run += timedelta(days=1)
            return next_run
        elif tier == SourceTier.TIER4:
            # Next 6-hour boundary (0, 6, 12, 18)
            current_hour = now.hour
            next_boundary = ((current_hour // 6) + 1) * 6
            if next_boundary >= 24:
                next_boundary = 0
                next_day = now.replace(hour=0, minute=0, second=0, microsecond=0) + \
                    timedelta(days=1)
                return next_day.replace(hour=next_boundary)
            return now.replace(hour=next_boundary, minute=0, second=0, microsecond=0)

        return now

    def get_cron_expression(self, tier: SourceTier) -> str:
        """Get the cron expression for a tier."""
        if tier == SourceTier.TIER1:
            return self.settings.tier1_schedule
        elif tier == SourceTier.TIER2:
            return self.settings.tier2_schedule
        elif tier == SourceTier.TIER3:
            return self.settings.tier3_schedule
        elif tier == SourceTier.TIER4:
            return self.settings.tier4_schedule
        return "0 * * * *"  # Default hourly


# Global scheduler instance
SCHEDULER = TieredScheduler()


def get_scheduler() -> TieredScheduler:
    """Get the global scheduler instance."""
    return SCHEDULER