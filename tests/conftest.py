"""Shared pytest fixtures.

The paper broker writes state to ~/.msts-trader/paper_state.json. Tests
monkeypatch that path to a tmp dir so they never touch the real user
state file and can run in parallel without contention.
"""
from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

import pytest

from msts_trader.models import Position, Target


@pytest.fixture(autouse=True)
def isolate_paper_state(monkeypatch, tmp_path):
    """Redirect paper broker state file to a per-test tmp path."""
    from msts_trader.brokers import paper

    state_path = tmp_path / "paper_state.json"
    monkeypatch.setattr(paper, "STATE_PATH", state_path)
    yield state_path


@pytest.fixture(autouse=True)
def isolate_fill_log(monkeypatch, tmp_path):
    """Redirect fill_log writes to a per-test tmp dir."""
    from msts_trader import fill_log

    monkeypatch.setattr(fill_log, "LOG_DIR", tmp_path / "fills")


@pytest.fixture
def basic_targets() -> list[Target]:
    return [
        Target(ticker="SPY", weight=Decimal("0.50")),
        Target(ticker="SHV", weight=Decimal("0.50")),
    ]


@pytest.fixture
def basic_quotes() -> dict[str, Decimal]:
    return {"SPY": Decimal("500"), "SHV": Decimal("110"), "GLD": Decimal("200")}


@pytest.fixture
def empty_positions() -> dict[str, Position]:
    return {}


@pytest.fixture
def basic_positions() -> dict[str, Position]:
    return {
        "SPY": Position(ticker="SPY", quantity=Decimal("10"), price=Decimal("500")),
        "GLD": Position(ticker="GLD", quantity=Decimal("5"), price=Decimal("200")),
    }


def write_paper_state(path: Path, *, cash: str = "50000", positions: dict | None = None, last_prices: dict | None = None):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "cash": cash,
                "positions": {k: str(v) for k, v in (positions or {}).items()},
                "last_prices": {k: str(v) for k, v in (last_prices or {}).items()},
            }
        )
    )
