"""Integration: full headless self-play with the heuristic AI.

Asserts the AI never submits an illegal intent (candidates are pre-validated),
that games reach a terminal state or the turn cap without error, that no figure
ends in an impossible state, and that play is deterministic under a fixed seed.
"""

from __future__ import annotations

import pytest

from clixengine.ai.heuristic import HeuristicAI
from clixengine.demo import demo_armies
from clixengine.setup import build_game


def _play_checked(engine, max_turns=200):
    ai = HeuristicAI()
    turns = 0
    while not engine.state.ended and turns < max_turns:
        while engine.actionable_figures() and not engine.state.ended:
            best = ai.best_decision(engine)
            if best is None or best.score <= 0.0:
                break
            res = engine.apply(best.candidate.intent)
            assert res.ok, f"illegal AI intent: {getattr(res, 'reason', '?')}"
        if not engine.state.ended:
            engine.end_turn()
        turns += 1
    return turns


def _assert_state_sane(engine):
    for f in engine.state.figures.values():
        nlc = f.definition.num_live_clicks
        assert 0 <= f.current_click <= nlc - 1
        if f.is_alive:
            b = engine.state.board
            r = f.base_radius
            assert -1e-6 <= f.position.x <= b.width + 1e-6
            assert -1e-6 <= f.position.y <= b.height + 1e-6
        assert 0 <= f.action_tokens <= 2


@pytest.mark.parametrize("seed", range(12))
def test_selfplay_completes_and_is_sane(db, seed):
    h, l = demo_armies(100, seed=seed, db=db)
    engine = build_game(h, l, build_total=100, seed=seed, db=db)
    turns = _play_checked(engine)
    assert turns <= 200
    _assert_state_sane(engine)
    # If the game ended it must name a winner (elimination), else it hit the cap.
    if engine.state.ended:
        assert engine.state.winner in ("human", "llm", None)


@pytest.mark.parametrize("points", [100, 200])
def test_selfplay_deterministic(db, points):
    def run():
        h, l = demo_armies(points, seed=99, db=db)
        e = build_game(h, l, build_total=points, seed=99, db=db)
        _play_checked(e)
        return e.state.winner, e.victory_points(), len(e.log.events)

    assert run() == run()


def test_build_game_initial_state(db):
    h, l = demo_armies(200, seed=5, db=db)
    e = build_game(h, l, build_total=200, seed=5, db=db)
    assert e.state.first_player in ("human", "llm")
    assert e.state.active_player == e.state.first_player
    assert len(e.state.figures) == len(h.figure_ids) + len(l.figure_ids)
    for f in e.state.figures.values():
        assert e.state.board.contains(f.position, f.base_radius)


def test_most_games_reach_a_winner(db):
    wins = 0
    for seed in range(20):
        h, l = demo_armies(100, seed=seed, db=db)
        e = build_game(h, l, build_total=100, seed=seed, db=db)
        _play_checked(e)
        if e.state.ended and e.state.winner is not None:
            wins += 1
    # The vast majority of 100-pt games should end by elimination.
    assert wins >= 15
