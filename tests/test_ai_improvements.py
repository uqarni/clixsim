"""Regression tests for the AI improvement plan (docs/ai-improvement-plan.md).

Each test pins one audited failure mode: threat-blind movement, the vanished
formation candidate, the unreachable healer, push-into-Demoralized, the farmed
free spin, wrong-half terrain, inert Stealth, and the snapshot bugs.
"""

import math

import pytest

from clixengine import abilities as ab
from clixengine.ai.evaluation import _push_cost, score_candidate, side_hopeless
from clixengine.candidates import generate_candidates, generate_formation_candidates
from clixengine.data import load_db
from clixengine.geometry import Vec
from clixengine.state import DEMORALIZED_ABILITY_ID
from clixengine.threat import clicks_to_demoralized, expected_incoming_clicks

from .conftest import build_engine


def _find(cands, kind=None, hint=None, label_has=None):
    for c in cands:
        if kind and c.kind != kind:
            continue
        if hint and c.annotation.get("intent_hint") != hint:
            continue
        if label_has and label_has not in c.label:
            continue
        return c
    return None


# --- threat model ------------------------------------------------------------
def test_incoming_threat_weights_now_vs_soon(db):
    e = build_engine(db, [
        ("human", "Troll Artillerist", (10, 10), math.pi / 2, 0),   # range 12
        ("llm", "Werebear", (10, 18), -math.pi / 2, 0),             # inside range
        ("llm", "Werebear", (10, 30), -math.pi / 2, 0),             # outside, reachable later
    ], active="llm")
    near, far = e.state.figure(1), e.state.figure(2)
    imm_near, _ = expected_incoming_clicks(e, near, near.position)
    imm_far, soon_far = expected_incoming_clicks(e, far, far.position)
    assert imm_near > 0          # shooter hits it from where it stands
    assert imm_far == 0          # out of range right now
    assert soon_far >= 0         # at most future pressure


def test_moves_annotated_with_danger_and_retreat_is_safer(db):
    e = build_engine(db, [
        ("human", "Troll Artillerist", (10, 10), math.pi / 2, 0),
        ("llm", "Werebear", (10, 20), -math.pi / 2, 0),
    ], active="llm")
    cs = generate_candidates(e, e.state.figure(1))
    approach = _find(cs, kind="move", hint="approach")
    retreat = _find(cs, kind="move", hint="retreat")
    assert approach and "incoming_clicks_at_dest" in approach.annotation
    assert retreat and retreat.annotation["incoming_clicks_at_dest"] \
        < retreat.annotation["incoming_clicks_here"]


# --- push cliff (plan 1.3) ----------------------------------------------------
def _figure_with_demoralized_dial(db):
    for f in db.all_figures():
        for i, cs in enumerate(f.dial[: f.num_live_clicks]):
            if DEMORALIZED_ABILITY_ID in cs.ability_ids() and i >= 2:
                return f, i
    pytest.skip("no demoralizing dial in db")


def test_push_cost_escalates_at_the_demoralized_cliff(db):
    fdef, demo_click = _figure_with_demoralized_dial(db)
    e = build_engine(db, [
        ("llm", fdef.id, (10, 10), 0.0, demo_click - 1),
        ("human", "Werebear", (30, 30), 0.0, 0),
    ], active="llm")
    f = e.state.figure(0)
    f.action_tokens = 1
    assert clicks_to_demoralized(f) == 1
    assert _push_cost(f) == pytest.approx(0.7 * f.points)
    cands = generate_candidates(e, f)
    pushed = [c for c in cands if c.annotation.get("pushes")]
    assert pushed and all(c.annotation.get("push_would_demoralize") for c in pushed)


# --- unreachable support pieces (plan 1.4) -------------------------------------
def test_healer_gets_an_approach_even_when_far(db):
    # Four closer bodies + a healer far behind them: the old top-3 cutoff made
    # the healer unreachable by construction for 60 turns.
    e = build_engine(db, [
        ("human", "Werebear", (10, 12), math.pi / 2, 0),
        ("human", "Werebear", (12, 12), math.pi / 2, 0),
        ("human", "Werebear", (14, 12), math.pi / 2, 0),
        ("human", "Werebear", (16, 12), math.pi / 2, 0),
        ("human", "Mending Priestess", (13, 4), math.pi / 2, 0),
        ("llm", "Seething Knight", (13, 26), -math.pi / 2, 0),
    ], active="llm")
    cs = generate_candidates(e, e.state.figure(5))
    hunt = [c for c in cs if c.annotation.get("target") == 4 and c.kind == "move"]
    assert hunt, "no approach candidate toward the healer"
    assert any("priority_target" in c.annotation for c in hunt)


# --- flank + kite (plans 2.1 / 1.2c) -------------------------------------------
def test_flank_candidate_on_oblique_approach(db):
    # Attacker beside/behind the target's facing: the rear contact point is
    # reachable without crossing the target's base.
    e = build_engine(db, [
        ("human", "Werebear", (10, 10), math.pi / 2, 0),   # faces +y
        ("llm", "Seething Knight", (10, 6), math.pi / 2, 0),  # approaching from behind
    ], active="llm")
    cs = generate_candidates(e, e.state.figure(1))
    flank = _find(cs, kind="move", hint="flank")
    assert flank is not None and flank.annotation.get("rear") is True


def test_kite_candidate_for_threatened_shooter(db):
    e = build_engine(db, [
        ("human", "Werebear", (10, 14), math.pi / 2, 0),        # speed 8 melee
        ("llm", "Utem Crossbowman", (10, 20), -math.pi / 2, 0),  # shooter
    ], active="llm")
    cs = generate_candidates(e, e.state.figure(1))
    kite = _find(cs, kind="move", hint="kite")
    assert kite is not None
    assert kite.annotation["incoming_clicks_at_dest"] <= kite.annotation["incoming_clicks_here"]


# --- formation-move robustness (plan 1.1) ---------------------------------------
def test_formation_move_survives_a_wall_by_switching_targets(db):
    from clixengine import terrain as terr
    e = build_engine(db, [
        ("human", "Chaos Mage", (10, 10), math.pi / 2, 0),
        ("human", "Chaos Mage", (11.1, 10), math.pi / 2, 0),
        ("human", "Chaos Mage", (12.2, 10), math.pi / 2, 0),
        ("llm", "Werebear", (11.1, 20), -math.pi / 2, 0),   # nearest — behind a wall
        ("llm", "Werebear", (30, 24), -math.pi / 2, 0),     # farther — open lane
    ])
    # Wall between the trio and the nearest enemy (the Anvil-game exploit).
    e.state.terrain.append(terr.piece_from_polygon(
        "blocking", (Vec(6, 14.5), Vec(16, 14.5), Vec(16, 16), Vec(6, 16)), 0, "human"))
    fcs = generate_formation_candidates(e, "human")
    fm = _find(fcs, kind="formation_move")
    assert fm is not None, "one wall deleted the formation candidate again"


# --- free spin faces the real threat (plan 1.6) ---------------------------------
def test_free_spin_faces_the_dangerous_enemy_not_the_bait(db):
    from clixengine.server import _best_spin_facing
    e = build_engine(db, [
        ("llm", "Werebear", (10, 10), math.pi / 2, 0),
        ("human", "Woodland Scout", (10, 11.1), -math.pi / 2, 0),   # cheap bait (dmg 1)
        ("human", "Living Elemental", (10, 8.9), math.pi / 2, 0),   # the killer
    ], active="human")
    facing = _best_spin_facing(e, e.state.figure(0))
    # Should face the Elemental (below, -y) — not the Scout the human just moved.
    assert math.sin(facing) < 0


# --- terrain orientation (plan 1.7) ----------------------------------------------
def test_terrain_candidates_are_owner_relative(db):
    from clixengine.army import Army
    from clixengine.setup import build_game
    utem = [f for f in db.all_figures() if not f.is_unique][:2]
    eng = build_game(Army("h", "human", [utem[0].id]), Army("l", "llm", [utem[1].id]),
                     200, seed=3, with_terrain=True)
    h = eng.state.board.height
    cands = eng.terrain_placement_candidates("llm")
    assert cands
    own_half = [c for c in cands if c["center"][1] > h / 2]
    assert len(own_half) >= len(cands) * 0.6, [c["center"] for c in cands]
    top = Vec(5, h - 6)
    assert "your side" in eng._where_label(top, "llm")
    assert "ENEMY" in eng._where_label(top, "human")


# --- Stealth is live (plan 3.4) ---------------------------------------------------
def test_stealth_blocks_lof_through_hindering(db):
    from clixengine import terrain as terr
    stealthy = next(f for f in db.all_figures()
                    if ab.STEALTH in f.all_ability_ids())
    shooter = next(f for f in db.all_figures() if f.range >= 10)
    e = build_engine(db, [
        ("human", shooter.id, (10, 4), math.pi / 2, 0),
        ("llm", stealthy.id, (10, 12), -math.pi / 2, 0),
    ], active="human")
    clear_before, _ = e.line_of_fire(0, 1)
    e.state.terrain.append(terr.piece_from_polygon(
        "hindering", (Vec(8, 7), Vec(12, 7), Vec(12, 9), Vec(8, 9)), 0, "human"))
    clear_after, reason = e.line_of_fire(0, 1)
    if ab.STEALTH in e.state.figure(1).active_ability_ids():
        assert clear_before and not clear_after and "Stealth" in reason
    else:
        pytest.skip("stealth not on starting click")


# --- snapshot correctness (plan 1.8) ----------------------------------------------
def test_snapshot_actions_remaining_after_formation_move(db):
    from clixengine.snapshot import board_snapshot
    e = build_engine(db, [
        ("human", "Chaos Mage", (10, 10), math.pi / 2, 0),
        ("human", "Chaos Mage", (11.1, 10), math.pi / 2, 0),
        ("human", "Chaos Mage", (12.2, 10), math.pi / 2, 0),
        ("llm", "Werebear", (11, 30), -math.pi / 2, 0),
    ], build_total=200)
    fm = _find(generate_formation_candidates(e, "human"), kind="formation_move")
    assert fm and e.apply(fm.intent).ok
    snap = board_snapshot(e)
    assert snap["actions_remaining"] == 1  # the old math reported -2 here
    f0 = snap["figures"][0]
    assert "remaining_clicks" in f0 and "next_clicks" in f0
    active = [f for f in snap["figures"] if f["owner"] == "human"]
    assert all("threats" in f for f in active)


# --- hopeless sides fight, they don't run (sportsmanship in scoring) ---------------
def test_hopeless_side_devalues_kiting(db):
    e = build_engine(db, [
        ("human", "Troll Artillerist", (10, 10), math.pi / 2, 0),
        ("human", "Troll Artillerist", (12, 10), math.pi / 2, 0),
        ("human", "Living Elemental", (14, 10), math.pi / 2, 0),
        ("llm", "Woodland Scout", (11, 18), -math.pi / 2, 2),   # battered remnant
    ], active="llm")
    assert side_hopeless(e, "llm") and not side_hopeless(e, "human")
    scout = e.state.figure(3)
    cs = generate_candidates(e, scout)
    kite = _find(cs, kind="move", hint="kite") or _find(cs, kind="move", hint="retreat")
    if kite is not None:
        assert score_candidate(e, scout, kite) < 1.0


# --- one-ply reply deltas (plan 3.1, bounded) ---------------------------------------
def test_reply_delta_annotated_on_top_moves(db):
    from clixengine.ai.llm import LLMOpponent
    e = build_engine(db, [
        ("llm", "Werebear", (10, 20), -math.pi / 2, 0),
        ("human", "Troll Artillerist", (10, 10), math.pi / 2, 0),
    ], active="llm")
    ranked = LLMOpponent()._ranked_candidates(e)
    moves = [c for _, _, c in ranked if c.kind == "move"]
    assert any("enemy_best_reply_after" in c.annotation for c in moves)
    assert all("heuristic_rank" in c.annotation for _, _, c in ranked)


# --- auto-deployment (plan 3.3) ------------------------------------------------------
def test_llm_auto_deploy_puts_melee_in_front(db):
    from clixengine.army import Army
    from clixengine.server import _auto_deploy_llm
    from clixengine.setup import build_game
    melee = next(f for f in db.all_figures() if f.range == 0 and not f.is_unique)
    shooter = next(f for f in db.all_figures() if f.range >= 8 and not f.is_unique)
    eng = build_game(
        Army("h", "human", [melee.id]),
        Army("l", "llm", [melee.id, melee.id, shooter.id, shooter.id]),
        400, seed=5, with_deploy=True)
    assert eng.state.phase == "deploy"
    _auto_deploy_llm(eng)
    assert getattr(eng, "llm_deployed", False)
    llm_figs = [f for f in eng.state.figures.values() if f.owner == "llm"]
    front_melee = [f for f in llm_figs if f.range == 0]
    back_shooters = [f for f in llm_figs if f.range > 0]
    # Front (toward the human) = smaller y for the top-edge deployer.
    assert max(f.position.y for f in front_melee) <= min(f.position.y for f in back_shooters) + 1e-6


# --- eval harness smoke ---------------------------------------------------------------
def test_eval_harness_plays_a_game(db):
    from clixengine.evalharness import play_game
    r = play_game(db, seed=4242, points=100, max_turns=40)
    assert r["human"]["actions"] > 0 and r["llm"]["actions"] > 0
    assert "avg_chosen_odds" in r["human"]


# --- adversarial-review fixes -----------------------------------------------------
def test_threat_score_never_mutates_positions(db):
    """The threat model must be PURE: an earlier version temporarily mutated
    figure.position — concurrent HTTP requests observed phantom positions and
    restore-races could strand them permanently."""
    from clixengine.threat import threat_score
    e = build_engine(db, [
        ("human", "Troll Artillerist", (10, 10), math.pi / 2, 0),
        ("llm", "Werebear", (10, 18), -math.pi / 2, 0),
    ], active="llm")
    wb = e.state.figure(1)
    before = (wb.position.x, wb.position.y)
    src = open("clixengine/threat.py").read()
    assert ".position =" not in src.replace("mover.position, at", ""), \
        "threat.py writes figure positions again"
    threat_score(e, wb, Vec(5, 5))
    assert (wb.position.x, wb.position.y) == before


def test_toggle_ability_is_not_a_push(db):
    """The engine treats toggles as non-actions (no budget, no token) — they
    must not carry push facts or pay push cost."""
    grounded = [f for f in db.all_figures()
                if f.faction == "Black Powder Rebels" and not f.is_unique
                and not (f.all_ability_ids() & {ab.FLIGHT, ab.AQUATIC, ab.QUICKNESS})][:2]
    flyer = next((f for f in db.all_figures()
                  if f.faction == "Black Powder Rebels"
                  and any(a.id in (ab.FLIGHT, ab.QUICKNESS) and a.optional
                          for a in f.dial[f.starting_click].abilities)), None)
    if flyer is None or len(grounded) < 2:
        pytest.skip("no togglable faction trio in db")
    e = build_engine(db, [
        ("llm", flyer.id, (10, 10), 0.0, 0),
        ("llm", grounded[0].id, (11.1, 10), 0.0, 0),
        ("llm", grounded[1].id, (12.2, 10), 0.0, 0),
        ("human", "Werebear", (30, 30), 0.0, 0),
    ], active="llm")
    fig = e.state.figure(0)
    fig.action_tokens = 1  # tokened — actions would push, toggles must not
    toggles = [c for c in generate_candidates(e, fig) if c.kind == "toggle_ability"]
    assert toggles, "toggle candidate missing"
    t = toggles[0]
    assert "pushes" not in t.annotation and "push_would_demoralize" not in t.annotation
    assert score_candidate(e, fig, t) > 0  # not charged a phantom push cost


def test_demoralized_cannot_be_toggled_off(db):
    from clixengine.intents import ToggleAbilityIntent
    fdef, demo_click = _figure_with_demoralized_dial(db)
    e = build_engine(db, [
        ("llm", fdef.id, (10, 10), 0.0, demo_click),
        ("human", "Werebear", (30, 30), 0.0, 0),
    ], active="llm")
    r = e.apply(ToggleAbilityIntent(0, DEMORALIZED_ABILITY_ID, off=True))
    assert not r.ok and r.reason == "not_optional"


def test_magic_blast_ignores_terrain_defense(db):
    """Card: 'no terrain modifiers are applied' — hindering must not add +1
    against a Magic Blast (resolver AND candidate odds)."""
    from clixengine import terrain as terr
    blaster = next(f for f in db.all_figures()
                   if ab.MAGIC_BLAST in f.all_ability_ids() and f.range >= 8)
    e = build_engine(db, [
        ("human", blaster.id, (10, 4), math.pi / 2, 0),
        ("llm", "Werebear", (10, 11), -math.pi / 2, 0),
    ], active="human")
    f = e.state.figure(0)
    if ab.MAGIC_BLAST not in f.active_ability_ids():
        pytest.skip("blast not on starting click")
    def blast_odds():
        cs = generate_candidates(e, f)
        c = _find(cs, kind="magic_blast")
        return c.annotation["hit_odds"] if c else None
    clear = blast_odds()
    e.state.terrain.append(terr.piece_from_polygon(
        "hindering", (Vec(8, 6.5), Vec(12, 6.5), Vec(12, 8.5), Vec(8, 8.5)), 0, "human"))
    through = blast_odds()
    assert clear is not None and through == clear, (clear, through)


def test_cover_candidate_only_when_reachable(db):
    from clixengine import terrain as terr
    e = build_engine(db, [
        ("human", "Troll Artillerist", (10, 10), math.pi / 2, 0),
        ("llm", "Werebear", (10, 18), -math.pi / 2, 0),
    ], active="llm")
    # Hill 20" away — unreachable this turn: no cover candidate may claim +1.
    e.state.terrain.append(terr.piece_from_polygon(
        "elevated", (Vec(28, 30), Vec(34, 30), Vec(34, 35), Vec(28, 35)), 0, "llm"))
    cs = generate_candidates(e, e.state.figure(1))
    covers = [c for c in cs if c.annotation.get("intent_hint") == "cover"]
    assert not covers


def test_selfplay_standoffs_terminate(db):
    """Reviewer reproduction: seed 1000 kited forever. It must now resolve."""
    from clixengine.evalharness import play_game
    r = play_game(db, 1000, max_turns=150)
    assert r["winner"] is not None
