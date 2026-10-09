"""The composite's call log, for the console's Activity page.

One row per partner query: when, which use case, which partner, the HTTP status
and outcome, the duration and each source's status. Never the subject, the
consent or any data. The Audit Manager still gets its events (it is the record
for forensics); this table only exists because the Audit Manager has no read API.

Off when no database is configured. Writes are fire-and-forget (a background
task per row) and never fail or slow down a query; rows older than the retention
period are deleted.
"""

import asyncio
import logging
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, List, Optional, Tuple

from sqlalchemy import JSON, DateTime, Integer, String, delete, func, or_, select
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

_logger = logging.getLogger("agri_composite.activity")

MAX_IN_FLIGHT = 1000
OUTCOMES = ("success", "denied", "failure")


class _Base(DeclarativeBase):
    pass


class ActivityRow(_Base):
    __tablename__ = "composite_activity"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    request_id: Mapped[str] = mapped_column(String(64))
    use_case: Mapped[Optional[str]] = mapped_column(String(200), index=True)
    partner_id: Mapped[Optional[str]] = mapped_column(String(200), index=True)
    http_status: Mapped[int] = mapped_column(Integer)
    outcome: Mapped[str] = mapped_column(String(20), index=True)
    reason: Mapped[Optional[str]] = mapped_column(String(200))
    duration_ms: Mapped[int] = mapped_column(Integer)
    sources: Mapped[Dict[str, str]] = mapped_column(JSON, default=dict)


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


class ActivityStore:
    """``session_maker``: an async sessionmaker, or None (recording off)."""

    def __init__(self, session_maker: Optional[Callable] = None, engine=None, retention_days: int = 90):
        self._session_maker = session_maker
        self._engine = engine
        self.retention_days = retention_days
        self._tasks: set = set()

    @property
    def enabled(self) -> bool:
        return self._session_maker is not None

    async def create_tables(self) -> None:
        if self._engine is None:
            return
        async with self._engine.begin() as conn:
            await conn.run_sync(_Base.metadata.create_all)

    # ── writing ──────────────────────────────────────────────────────────────

    def record(
        self, *, request_id: str, use_case: Optional[str], partner_id: Optional[str], http_status: int,
        outcome: str, reason: Optional[str], duration_ms: int, sources: Dict[str, str],
    ) -> None:
        if not self.enabled:
            return
        try:
            if len(self._tasks) >= MAX_IN_FLIGHT:
                _logger.warning("Activity backlog full (%d in flight); dropping a row", len(self._tasks))
                return
            row = ActivityRow(
                id=str(uuid.uuid4()), at=datetime.now(timezone.utc), request_id=request_id,
                use_case=use_case, partner_id=partner_id, http_status=http_status, outcome=outcome,
                reason=reason, duration_ms=duration_ms, sources=dict(sources),
            )
            task = asyncio.get_running_loop().create_task(self._insert(row))
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)
        except Exception:
            _logger.warning("Failed to queue an activity row", exc_info=True)

    async def _insert(self, row: ActivityRow) -> None:
        try:
            async with self._session_maker() as session:
                session.add(row)
                await session.commit()
        except Exception as e:
            _logger.warning("Activity row not written: %r", e)

    async def purge(self) -> int:
        if not self.enabled or self.retention_days <= 0:
            return 0
        cutoff = datetime.now(timezone.utc) - timedelta(days=self.retention_days)
        async with self._session_maker() as session:
            result = await session.execute(delete(ActivityRow).where(ActivityRow.at < cutoff))
            await session.commit()
            return result.rowcount or 0

    async def drain(self) -> None:
        if self._tasks:
            await asyncio.gather(*list(self._tasks), return_exceptions=True)

    # ── reading ──────────────────────────────────────────────────────────────

    async def query(
        self, *, limit: int = 50, offset: int = 0, partner: Optional[str] = None,
        use_case: Optional[str] = None, outcome: Optional[str] = None,
    ) -> Tuple[int, List[Dict[str, Any]]]:
        conditions = []
        if partner:
            conditions.append(ActivityRow.partner_id == partner)
        if use_case:
            # "loan-profile" matches every major; "loan-profile@1" one.
            conditions.append(ActivityRow.use_case == use_case if "@" in use_case
                              else or_(ActivityRow.use_case == use_case, ActivityRow.use_case.like(f"{use_case}@%")))
        if outcome:
            conditions.append(ActivityRow.outcome == outcome)
        async with self._session_maker() as session:
            total = (await session.execute(
                select(func.count()).select_from(ActivityRow).where(*conditions))).scalar() or 0
            rows = (await session.execute(
                select(ActivityRow).where(*conditions).order_by(ActivityRow.at.desc()).offset(offset).limit(limit)
            )).scalars().all()
        return total, [
            {"at": _iso(r.at), "request_id": r.request_id, "use_case": r.use_case, "partner_id": r.partner_id,
             "http_status": r.http_status, "outcome": r.outcome, "reason": r.reason, "duration_ms": r.duration_ms,
             "sources": r.sources or {}}
            for r in rows
        ]

    async def summary(self, hours: int = 24) -> Dict[str, int]:
        since = datetime.now(timezone.utc) - timedelta(hours=hours)
        async with self._session_maker() as session:
            calls = (await session.execute(
                select(func.count()).select_from(ActivityRow).where(ActivityRow.at >= since))).scalar() or 0
            failed = (await session.execute(
                select(func.count()).select_from(ActivityRow)
                .where(ActivityRow.at >= since, ActivityRow.outcome != "success"))).scalar() or 0
        return {"calls": calls, "failed": failed}
