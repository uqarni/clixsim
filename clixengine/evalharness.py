"""Deterministic self-play eval harness (plan 1.0).

Runs seeded heuristic-vs-heuristic games (no API calls; the heuristic uses the
same candidate generation + scoring the LLM picker ranks with, so scoring and
candidate changes are measurable here) and reports the audit's metrics:

    win rate / points differential / turns
    clicks dealt vs received, % self-inflicted (pushes)
    formation moves, rear attacks, passes, attacks-per-action
    average CHOSEN-attack hit odds (decision-time, from candidate annotations)

Usage:
    .venv/bin/python -m clixengine.evalharness --games 20 --points 200
"""

from __future__ import annotations

import argparse
import json
from collections import Counter

from .ai.heuristic import HeuristicAI
from .build import heuristic_army
from .data import load_db
from .setup import build_game


def play_game(db, seed: int, points: int = 200, max_turns: int = 80) -> dict:
    """One full seeded self-play game; returns per-side metric dict."""
    a = heuristic_army(db, "human", points, seed * 2 + 1)
    b = heuristic_army(db, "llm", points, seed * 2 + 2)
    eng = build_game(a, b, points, seed=seed)
    ai = HeuristicAI()
    stats = {s: Counter() for s in ("human", "llm")}
    odds_sum = {s: 0.0 for s in ("human", "llm")}

    while not eng.state.ended and eng.state.turn_number <= max_turns:
        side = eng.state.active_player
        for step in ai.stream_turn(eng):
            c = step["candidate"]
            stats[side]["actions"] += 1
            stats[side][c.kind] += 1
            odds = c.annotation.get("hit_odds")
            if odds is not None and c.kind in (
                    "close", "ranged", "weapon_master", "magic_blast",
                    "close_formation", "ranged_formation"):
                stats[side]["attacks"] += 1
                odds_sum[side] += odds
                if c.annotation.get("rear"):
                    stats[side]["rear_attacks"] += 1
            for e in step["events"]:
                if e.get("type") in ("close_attack", "ranged_attack", "shockwave",
                                     "flame_lightning", "close_formation",
                                     "ranged_formation", "magic_blast"):
                    stats[side]["clicks_dealt"] += e.get("clicks", 0)
                elif e.get("type") == "push_damage":
                    stats[side]["self_clicks"] += e.get("clicks", 1)

    surviving = {s: sum(f.points for f in eng.state.living(s)) for s in ("human", "llm")}
    out = {
        "seed": seed,
        "winner": eng.state.winner,
        "turns": eng.state.turn_number,
        "points_left": surviving,
    }
    for s in ("human", "llm"):
        c = stats[s]
        out[s] = {
            "actions": c["actions"],
            "attacks": c["attacks"],
            "attacks_per_action": round(c["attacks"] / c["actions"], 2) if c["actions"] else 0,
            "avg_chosen_odds": round(odds_sum[s] / c["attacks"], 2) if c["attacks"] else None,
            "clicks_dealt": c["clicks_dealt"],
            "self_clicks": c["self_clicks"],
            "formation_moves": c["formation_move"],
            "rear_attacks": c["rear_attacks"],
            "passes": c["pass"],
        }
    return out


def run(games: int = 20, points: int = 200, seed0: int = 1000) -> dict:
    db = load_db()
    rows = [play_game(db, seed0 + i, points) for i in range(games)]
    agg: dict = {"games": games, "draws": 0, "wins": Counter(), "avg_turns": 0.0}
    tot = {s: Counter() for s in ("human", "llm")}
    for r in rows:
        if r["winner"]:
            agg["wins"][r["winner"]] += 1
        else:
            agg["draws"] += 1
        agg["avg_turns"] += r["turns"] / games
        for s in ("human", "llm"):
            for k, v in r[s].items():
                if isinstance(v, (int, float)) and v is not None:
                    tot[s][k] += v
    agg["wins"] = dict(agg["wins"])
    agg["avg_turns"] = round(agg["avg_turns"], 1)
    for s in ("human", "llm"):
        agg[s] = {k: round(v / games, 2) for k, v in tot[s].items()}
    agg["rows"] = rows
    return agg


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--games", type=int, default=20)
    ap.add_argument("--points", type=int, default=200)
    ap.add_argument("--seed0", type=int, default=1000)
    ap.add_argument("--full", action="store_true", help="include per-game rows")
    args = ap.parse_args()
    result = run(args.games, args.points, args.seed0)
    if not args.full:
        result.pop("rows")
    print(json.dumps(result, indent=1))
