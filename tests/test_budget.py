import asyncio
from types import SimpleNamespace

import pytest

from src.budget import BudgetExceeded, TokenBudget


class FakeDb:
    def __init__(self, used: int) -> None:
        self.used = used

    async def _one(self, sql: str, params: tuple[object, ...] = ()) -> dict[str, int]:
        return {"used": self.used}


def _budget(used: int, limit: int = 100) -> TokenBudget:
    settings = SimpleNamespace(daily_token_budget=limit, token_warn_ratio=0.8)
    return TokenBudget(settings, FakeDb(used))  # type: ignore[arg-type]


def test_budget_warns_at_threshold() -> None:
    async def run() -> None:
        budget = _budget(80)
        status = await budget.status()
        assert status.warning is True
        assert status.exhausted is False
        assert await budget.maybe_warn() is True
        assert await budget.maybe_warn() is False

    asyncio.run(run())


def test_budget_blocks_when_exhausted() -> None:
    async def run() -> None:
        budget = _budget(100)
        with pytest.raises(BudgetExceeded):
            await budget.check()

    asyncio.run(run())
