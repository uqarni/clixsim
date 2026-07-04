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
    fall back to an even spread.

    The line sits at 1.75" from the owner's edge: deep enough that a MOUNTED
    figure facing the enemy keeps its whole trailing rear circle inside the
    band (front dot must be >= 3r = 1.65 from the edge, P5-R11) while any
    figure's front circle still clears the band's far side (<= 2.45)."""
    band = 1.75
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
        # Group faction-mates adjacently in the line (stable within a faction)
        # so 3+ same-faction figures start cohesive and can form up turn one.
        by_faction = sorted(army.figure_ids, key=lambda fid: db.get(fid).faction)
        positions = _deploy_positions(len(by_faction), board, edge)
        for fid, pos in zip(by_faction, positions):
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

    # Every constructed placement must be legal for its whole footprint — a
    # regression in the row math above would otherwise silently start a game
    # with a mounted rear circle off the board (deploy-less games never pass
    # through deploy_figure's checks).
    for fig in state.figures.values():
        if not board.contains_circles(fig.circles()):
            raise ValueError(
                f"illegal deploy: {fig.short_name} (uid {fig.uid}) footprint off-board"
            )

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
