"""Operator-visible alerts from the checkpoint signer: append-only, insert-only.

An alert is the signer's refusal to do something, recorded rather than returned
and forgotten. The cron route returns its result to the HTTP caller, but the
caller is a scheduler nobody reads; the row is what /healthz/details shows, and
what a person sees months later when they ask why the log has no new
checkpoint.

Append-only for the same reason as the log itself: an alert that can be deleted
is an alert that can be hidden. The table has no update path in the model, and
the migration adds the same mutation trigger the log and checkpoint tables have,
plus INSERT-only grants to the least-privilege signer role.

On TransparencyBase, like the rest of the transparency package, so these tables
stay out of scripts/check_schema.py's drift comparison.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import Column, DateTime, Index, String
from sqlalchemy.dialects.postgresql import JSONB, UUID

from src.transparency.log import TransparencyBase


class TransparencyAlert(TransparencyBase):
    """One recorded alert. Never updated, never deleted."""

    __tablename__ = "transparency_alerts"
    __table_args__ = (
        Index("ix_transparency_alerts_kind_created", "kind", "created_at"),
    )

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    kind = Column(String(64), nullable=False)  # e.g. "inconsistent_history"
    detail = Column(JSONB, nullable=False, default=dict)  # structured, never a secret
    created_at = Column(DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc))


__all__ = ["TransparencyAlert"]
