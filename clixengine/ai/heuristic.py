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

    def best_decision(self, engine: Engine, exclude: frozenset[str] = frozenset()) -> Decision | None:
        """Global best candidate; ``exclude`` holds intent-reprs the engine already
        rejected this turn so a retry never re-picks a known-illegal action."""
        best: Decision | None = None
        for fig in engine.actionable_figures():
            for cand in generate_candidates(engine, fig):
                if exclude and repr(cand.intent) in exclude:
                    continue
                s = score_candidate(engine, fig, cand)
                if best is None or s > best.score:
                    best = Decision(fig.uid, cand, s, cand.label)
        # Turn-level formation candidates (movement / ranged / close).
        for cand in generate_formation_candidates(engine, engine.state.active_player):
            if exclude and repr(cand.intent) in exclude:
                continue
            primary = engine.state.figure(cand.annotation["primary"])
            s = score_candidate(engine, primary, cand)
            if best is None or s > best.score:
                best = Decision(primary.uid, cand, s, cand.label)
        return best

    def take_turn(self, engine: Engine) -> list[Decision]:
        """Play the active player's whole turn, then end it."""
        return [
            Decision(s["figure_uid"], s["candidate"], s["score"], s["summary"])
            for s in self.stream_turn(engine)
        ]

    def stream_turn(self, engine: Engine, table_talk: list[dict] | None = None,
                    memory: list[str] | None = None):
        """Yield one dict per action (summary, reasoning, events) as it resolves,
        then end the turn — the streaming form used by the live opponent view.
        ``table_talk`` is accepted for interface parity (the heuristic doesn't
        read the banter)."""
        rejected: set[str] = set()
        while engine.actionable_figures() and not engine.state.ended:
            best = self.best_decision(engine, frozenset(rejected))
            if best is None or best.score <= 0.0:
                break
            result = engine.apply(best.candidate.intent)
            if not result.ok:
                # A rejected pick must not end the turn (that reads as the
                # opponent freezing) — exclude it and choose again, bounded.
                rejected.add(repr(best.candidate.intent))
                if len(rejected) >= 12:
                    break
                continue
            rejected.clear()  # board changed; stale rejections no longer apply
            yield {
                "figure_uid": best.figure_uid, "candidate": best.candidate, "score": best.score,
                "summary": best.summary, "reasoning": _heuristic_reason(best.candidate),
                "events": result.events, "fallback": False,
            }
            if engine.state.ended:
                break
        if not engine.state.ended:
            engine.end_turn()


def _heuristic_reason(cand: Candidate) -> str:
    a = cand.annotation
    hit = a.get("hit_odds")
    if cand.kind in ("charge_strike", "bound_shot"):
        odds = f" ({round(hit * 100)}% hit)" if isinstance(hit, (int, float)) else ""
        return f"The free Charge/Bound follow-up — costs nothing{odds}."
    if cand.kind in ("close", "ranged", "weapon_master", "magic_blast", "flame_lightning",
                     "shockwave", "close_formation", "ranged_formation"):
        odds = f" ({round(hit * 100)}% hit)" if isinstance(hit, (int, float)) else ""
        return f"Best expected damage{odds}."
    if cand.kind == "heal":
        return "Patching up a wounded figure."
    if cand.kind in ("formation_move", "move"):
        return "Advancing to a stronger position."
    if cand.kind == "pass":
        return "Nothing worth pushing for — resting."
    return "Strongest available option."


def _pass_of(cand: Candidate):
    from ..intents import PassIntent

    uid = getattr(cand.intent, "figure_uid", None) or getattr(cand.intent, "attacker_uid")
    return PassIntent(uid)
