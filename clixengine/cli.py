"""Command-line front end: ASCII renderer + self-play and interactive play.

    python -m clixengine.cli selfplay --points 100 --seed 1
    python -m clixengine.cli selfplay --ai llm --points 100 --seed 1
    python -m clixengine.cli play --points 100 --seed 1        # human vs Sonnet 5

The renderer is a top-down 2D board (DP3 continuous space rendered to a grid).
"""

from __future__ import annotations

import argparse
import math

from .ai.heuristic import HeuristicAI
from .ai.llm import LLMOpponent
from .candidates import generate_candidates
from .demo import demo_armies
from .engine import Engine
from .setup import build_game

GRID_W, GRID_H = 56, 26


def render_board(engine: Engine) -> str:
    st = engine.state
    grid = [[" "] * GRID_W for _ in range(GRID_H)]
    # Border.
    for x in range(GRID_W):
        grid[0][x] = grid[GRID_H - 1][x] = "-"
    for y in range(GRID_H):
        grid[y][0] = "|"
        grid[y][GRID_W - 1] = "|"

    for f in st.living():
        cx = int((f.position.x / st.board.width) * (GRID_W - 3)) + 1
        cy = int(((st.board.height - f.position.y) / st.board.height) * (GRID_H - 3)) + 1
        cx = max(1, min(GRID_W - 2, cx))
        cy = max(1, min(GRID_H - 2, cy))
        tag = str(f.uid)
        mark = tag if f.owner == "human" else tag.lower()
        # Human uppercase-ish, llm lowercase-ish: use () vs [] to disambiguate.
        cell = f"({mark})" if f.owner == "human" else f"[{mark}]"
        for i, ch in enumerate(cell):
            if 0 < cx - 1 + i < GRID_W - 1:
                grid[cy][cx - 1 + i] = ch

    lines = ["".join(row) for row in grid]
    header = (
        f"Turn {st.turn_number} | to act: {st.active_player} | "
        f"actions/turn: {st.actions_per_turn()}"
    )
    legend = ["", "Figures:  (n)=human  [n]=llm"]
    for f in sorted(st.figures.values(), key=lambda x: x.uid):
        status = "DEAD" if f.eliminated else f"click {f.current_click} hp {f.health_fraction():.0%}"
        legend.append(
            f"  {f.uid:>2} {f.owner:<5} {f.short_name:<22} "
            f"S{f.speed} A{f.attack} D{f.defense} DM{f.damage} R{f.range} [{status}]"
        )
    return header + "\n" + "\n".join(lines) + "\n" + "\n".join(legend)


def _result_line(engine: Engine) -> str:
    if not engine.state.ended:
        return "Turn limit reached; no elimination winner."
    if engine.state.winner is None:
        return "Result: DRAW (mutual elimination)"
    return f"WINNER: {engine.state.winner}"


def _print_decisions(decisions, engine: Engine) -> None:
    for d in decisions:
        print(f"    - {d.summary}")


def run_selfplay(args) -> int:
    human_army, llm_army = demo_armies(args.points, seed=args.seed)
    engine = build_game(human_army, llm_army, build_total=args.points, seed=args.seed,
                        board_size=args.board)
    cov = engine.ability_coverage()
    if cov["unimplemented"]:
        names = ", ".join(a["name"] for a in cov["unimplemented"])
        print(f"[ability coverage] flagged (not yet implemented, treated inert): {names}")

    if args.ai == "llm":
        llm = LLMOpponent(verbose=args.verbose)
        if not llm.available:
            print(f"[llm] unavailable ({llm.last_error}); llm side uses heuristic fallback")
        controllers = {"llm": llm, "human": HeuristicAI()}
    else:
        controllers = {"llm": HeuristicAI(), "human": HeuristicAI()}

    print(render_board(engine))
    turns = 0
    while not engine.state.ended and turns < args.max_turns:
        active = engine.state.active_player
        ai = controllers[active]
        decisions = ai.take_turn(engine)
        turns += 1
        print(f"\n=== {active} turn {turns} ({getattr(ai,'name','?')}) ===")
        _print_decisions(decisions, engine)
        print(render_board(engine))

    print("\n" + "=" * 40)
    print(_result_line(engine))
    print(f"Victory points: {engine.victory_points()}")
    if args.ai == "llm" and isinstance(controllers['llm'], LLMOpponent):
        llm = controllers["llm"]
        print(f"LLM calls: {llm.calls}, fallbacks: {llm.fallbacks}, last_error: {llm.last_error!r}")
    if args.log:
        engine.log.save(args.log)
        print(f"Game log saved to {args.log}")
    return 0


def run_play(args) -> int:
    human_army, llm_army = demo_armies(args.points, seed=args.seed)
    engine = build_game(human_army, llm_army, build_total=args.points, seed=args.seed,
                        board_size=args.board)
    llm = LLMOpponent()
    if not llm.available:
        print(f"[llm] unavailable ({llm.last_error}); opponent uses heuristic fallback")
    controllers = {"llm": llm if llm.available else HeuristicAI()}

    print("You are the (n) side. Enter a candidate number to act, or 'e' to end your turn.")
    turns = 0
    while not engine.state.ended and turns < args.max_turns:
        active = engine.state.active_player
        print(render_board(engine))
        if active == "human":
            _human_turn(engine)
        else:
            decisions = controllers["llm"].take_turn(engine)
            print(f"\n=== {active} (Sonnet 5) ===")
            _print_decisions(decisions, engine)
        turns += 1

    print("\n" + "=" * 40)
    print(_result_line(engine))
    print(f"Victory points: {engine.victory_points()}")
    return 0


def _human_turn(engine: Engine) -> None:
    while engine.actionable_figures() and not engine.state.ended:
        options = []
        for fig in engine.actionable_figures():
            for cand in generate_candidates(engine, fig):
                options.append((fig, cand))
        print(f"\n[your turn] actions remaining: {engine._actions_remaining()}")
        for i, (fig, cand) in enumerate(options):
            fact = cand.annotation
            hint = ""
            if "hit_odds" in fact and isinstance(fact["hit_odds"], (int, float)):
                hint = f"  (hit {fact['hit_odds']:.0%}, ~{fact.get('expected_clicks','?')} clicks)"
            print(f"  {i:>2}: {fig.short_name}: {cand.label}{hint}")
        choice = input("> choose #, or 'e' to end turn: ").strip()
        if choice.lower() in ("e", "end", ""):
            engine.end_turn()
            return
        try:
            idx = int(choice)
            fig, cand = options[idx]
        except (ValueError, IndexError):
            print("  invalid choice")
            continue
        result = engine.apply(cand.intent)
        if not result.ok:
            print(f"  rejected: {result.reason} ({result.detail})")
        else:
            print(f"  {result.summary}")
    if not engine.state.ended:
        engine.end_turn()


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="Clix Engine — Mage Knight vs LLM")
    sub = p.add_subparsers(dest="cmd", required=True)

    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--points", type=int, default=100, help="build total (mult of 100)")
    common.add_argument("--seed", type=int, default=1)
    common.add_argument("--board", type=float, default=36.0)
    common.add_argument("--max-turns", type=int, default=60)
    common.add_argument("--verbose", action="store_true")

    sp = sub.add_parser("selfplay", parents=[common])
    sp.add_argument("--ai", choices=["heuristic", "llm"], default="heuristic")
    sp.add_argument("--log", type=str, default=None)
    sp.set_defaults(func=run_selfplay)

    pl = sub.add_parser("play", parents=[common])
    pl.set_defaults(func=run_play)

    args = p.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
