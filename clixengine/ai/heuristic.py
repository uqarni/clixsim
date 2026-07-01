"""Heuristic opponent (policy prior + deterministic fallback).

Greedy, seed-reproducible, no API calls. Each action, it scores every candidate
across every figure that can still act and takes the global best; it stops
(ends the turn) when only resting/negative options remain. This doubles as the
fallback when the LLM opponent is unavailable or returns an invalid choice.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..candidates import Candidate, generate_candidates, generate_formation_candidates
from ..engine import Engine
from ..state import Figure
from .evaluation import score_candidate


@dataclass
class Decision:
    figure_uid: int
    candidate: Candidate
    score: float
    summary: str


class HeuristicAI:
    name = "heuristic"

    def best_decision(self, engine: Engine) -> Decision | None:
        best: Decision | None = None
        for fig in engine.actionable_figures():
            for cand in generate_candidates(engine, fig):
                s = score_candidate(engine, fig, cand)
                if best is None or s > best.score:
                    best = Decision(fig.uid, cand, s, cand.label)
        # Turn-level formation candidates (movement / ranged / close).
        for cand in generate_formation_candidates(engine, engine.state.active_player):
            primary = engine.state.figure(cand.annotation["primary"])
            s = score_candidate(engine, primary, cand)
            if best is None or s > best.score:
                best = Decision(primary.uid, cand, s, cand.label)
        return best

    def take_turn(self, engine: Engine) -> list[Decision]:
        """Play the active player's whole turn, then end it."""
        decisions: list[Decision] = []
        while engine.actionable_figures() and not engine.state.ended:
            best = self.best_decision(engine)
            if best is None or best.score <= 0.0:
                break  # only resting / no-value options remain
            result = engine.apply(best.candidate.intent)
            if not result.ok:
                # Should not happen (candidates are pre-validated); skip to avoid loop.
                engine.apply(_pass_of(best.candidate))
            decisions.append(best)
            if engine.state.ended:
                break
        if not engine.state.ended:
            engine.end_turn()
        return decisions


def _pass_of(cand: Candidate):
    from ..intents import PassIntent

    uid = getattr(cand.intent, "figure_uid", None) or getattr(cand.intent, "attacker_uid")
    return PassIntent(uid)
