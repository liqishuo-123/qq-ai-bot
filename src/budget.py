from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta, timezone
from typing import Any

from .db import Database
from .settings import Settings

CN_TZ = timezone(timedelta(hours=8))


class BudgetExceeded(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class BudgetStatus:
    limit: int
    used: int
    remaining: int
    percent: float
    exhausted: bool
    warning: bool

    def as_dict(self) -> dict[str, Any]:
        return {
            "limit": self.limit,
            "used": self.used,
            "remaining": self.remaining,
            "percent": round(self.percent, 1),
            "exhausted": self.exhausted,
            "warning": self.warning,
            "enabled": self.limit > 0,
        }


def today_start_utc() -> str:
    start = datetime.now(CN_TZ).replace(hour=0, minute=0, second=0, microsecond=0)
    return start.astimezone(UTC).isoformat(timespec="seconds")


class TokenBudget:
    def __init__(self, settings: Settings, db: Database) -> None:
        self.settings = settings
        self.db = db
        self._alert_day = ""

    async def status(self) -> BudgetStatus:
        limit = max(0, int(self.settings.daily_token_budget))
        if limit <= 0:
            return BudgetStatus(0, 0, 0, 0.0, False, False)
        row = await self.db._one(
            """
            SELECT COALESCE(SUM(input_tokens + output_tokens), 0) AS used
            FROM usage_logs
            WHERE created_at >= ?
            """,
            (today_start_utc(),),
        )
        used = int((row["used"] if row else 0) or 0)
        remaining = max(0, limit - used)
        percent = min(100.0, used / limit * 100)
        warning = used >= limit * min(1.0, max(0.0, self.settings.token_warn_ratio))
        return BudgetStatus(limit, used, remaining, percent, used >= limit, warning)

    async def check(self) -> BudgetStatus:
        status = await self.status()
        if status.exhausted:
            raise BudgetExceeded(f"今日 token 预算已用完：{status.used}/{status.limit}")
        return status

    async def maybe_warn(self) -> bool:
        status = await self.status()
        today = datetime.now(CN_TZ).date().isoformat()
        if status.warning and not status.exhausted and self._alert_day != today:
            self._alert_day = today
            return True
        return False
