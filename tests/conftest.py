"""Shared test fixtures & a controlled-scenario builder.

``build_engine`` places specific figures at specific positions/clicks so tests
can target individual rules deterministically (bypassing random deployment).
"""

from __future__ import annotations

import math

import pytest

from clixengine.data import load_db
from clixengine.engine import Engine
from clixengine.geometry import Vec
from clixengine.state import Board, Figure, GameState


@pytest.fixture(autouse=True)
def _isolated_history(monkeypatch, tmp_path):
    """NEVER let tests write into the user's real all-time game archive
    (~/.clixengine/history) — server tests start real games, which checkpoint."""
    monkeypatch.setenv("CLIX_HISTORY_DIR", str(tmp_path / "clix-history"))


@pytest.fixture(scope="session")
def db():
    return load_db()


def build_engine(
    db,
    specs,
    seed: int = 0,
    board: float = 36.0,
    active: str = "human",
    build_total: int = 200,
) -> Engine:
    """specs: list of (owner, id_or_shortname, (x,y), facing_rad, click)."""
    state = GameState(
        board=Board(board, board),
        build_total=build_total,
        active_player=active,
        first_player=active,
    )
    uid = 0
    for owner, ident, pos, facing, click in specs:
        fdef = db.get(ident) if isinstance(ident, int) else db.find(ident)[0]
        f = Figure(
            uid=uid,
            definition=fdef,
            owner=owner,
            position=Vec(*pos),
            facing=facing,
            current_click=click,
        )
        state.figures[uid] = f
        uid += 1
    engine = Engine(state, db=db, seed=seed)
    for f in state.figures.values():
        if f.owner == active:
            f.begin_owner_turn()
    return engine
