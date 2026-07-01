"""Game setup: instantiate a GameState + Engine from two armies (Phase 3).

Figures deploy in a 3"-deep band along each owner's edge (P3-R5), spread along the
board width, facing the centre. With ``with_terrain`` the game opens in a terrain
setup phase (players alternate placing pieces) before the first battle turn.
"""

from __future__ import annotations

import math

from .army import Army
from .data import FigureDB, load_db
from .engine import Engine
from .geometry import Vec
from .state import STANDARD_BASE_RADIUS, Board, Figure, GameState


def _deploy_positions(n: int, board: Board, edge: str) -> list[Vec]:
    """Deploy n figures touching in a centred line within the 3" starting band.

    Adjacent figures start in base contact (edge gap 0) so same-faction armies
    begin cohesive and can use movement/ranged formations immediately (players
    legitimately deploy a formation together). If they don't all fit touching,
    fall back to an even spread."""
    band = 1.5  # centre line of the 3"-deep starting band
    y = band if edge == "bottom" else board.height - band
    if n == 1:
        return [Vec(board.width / 2, y)]
    spacing = 2 * STANDARD_BASE_RADIUS  # exact touch => in base contact
    total = spacing * (n - 1)
    if total <= board.width - 2 * spacing:
        x0 = board.width / 2 - total / 2
        xs = [x0 + spacing * i for i in range(n)]
    else:
        margin = 3.0
        span = board.width - 2 * margin
        xs = [margin + span * i / (n - 1) for i in range(n)]
    return [Vec(x, y) for x in xs]


def build_game(
    human_army: Army,
    llm_army: Army,
    build_total: int = 200,
    seed: int = 0,
    board_size: float = 36.0,
    db: FigureDB | None = None,
    with_terrain: bool = False,
    terrain_per_player: int = 3,
    with_deploy: bool = False,
) -> Engine:
    db = db or load_db()
    board = Board(board_size, board_size)
    state = GameState(board=board, build_total=build_total)

    uid = 0
    for army, edge, facing in (
        (human_army, "bottom", math.pi / 2),  # face up (+y)
        (llm_army, "top", -math.pi / 2),  # face down (-y)
    ):
        positions = _deploy_positions(len(army.figure_ids), board, edge)
        for fid, pos in zip(army.figure_ids, positions):
            fdef = db.get(fid)
            fig = Figure(
                uid=uid,
                definition=fdef,
                owner=army.owner,
                position=pos,
                facing=facing,
                base_radius=STANDARD_BASE_RADIUS,
            )
            state.figures[uid] = fig
            uid += 1

    # First player: opposed 2d6 (P3-R2), re-roll ties.
    engine = Engine(state, db=db, seed=seed)
    while True:
        _, _, h = engine.rng.roll_2d6("initiative", "human")
        _, _, l = engine.rng.roll_2d6("initiative", "llm")
        if h != l:
            first = "human" if h > l else "llm"
            break
    state.first_player = first
    state.active_player = first
    engine.log.emit("setup", first_player=first, build_total=build_total,
                    board=[board.width, board.height])
    if with_terrain and terrain_per_player > 0:
        # Setup phase: the initiative winner places terrain first, then players
        # alternate. Figure deployment (if requested) follows; the first battle turn
        # begins only once all setup completes.
        state.phase = "terrain"
        state.terrain_budget = {"human": terrain_per_player, "llm": terrain_per_player}
        state.terrain_turn = first
        state.pending_deploy = with_deploy
        engine.log.emit("terrain_setup", first_placer=first, per_player=terrain_per_player)
    elif with_deploy:
        state.phase = "deploy"
        engine.log.emit("deploy_setup", first=first)
    else:
        engine._begin_player_turn(first)
        engine.log.emit("begin_turn", player=first, turn=1)
    return engine
