from clixengine.army import Army, validate_army
from clixengine.demo import demo_armies


def test_over_budget_rejected(db):
    figs = sorted(db.all_figures(), key=lambda f: -f.points)[:5]
    army = Army("big", "human", [f.id for f in figs])
    v = validate_army(army, db, build_total=100)
    assert not v.ok
    assert any("over" in e for e in v.errors)


def test_unique_appears_once(db):
    uniques = [f for f in db.all_figures() if f.is_unique]
    assert uniques
    u = uniques[0]
    army = Army("dup", "human", [u.id, u.id])
    v = validate_army(army, db, build_total=1000)
    assert not v.ok
    assert any("more than once" in e for e in v.errors)


def test_same_unique_allowed_in_both_armies(db):
    u = next(f for f in db.all_figures() if f.is_unique)
    a = Army("a", "human", [u.id])
    b = Army("b", "llm", [u.id])
    assert validate_army(a, db, 1000).ok
    assert validate_army(b, db, 1000).ok


def test_empty_army_rejected(db):
    v = validate_army(Army("x", "human", []), db, 100)
    assert not v.ok


def test_demo_armies_are_legal(db):
    for pts in (100, 200, 300):
        h, l = demo_armies(pts, seed=3, db=db)
        assert validate_army(h, db, pts).ok
        assert validate_army(l, db, pts).ok
        assert h.total_points(db) <= pts
        assert l.total_points(db) <= pts


def test_army_builder_doctrine_varies_and_knows_the_rules(db):
    from clixengine.build import DOCTRINES, ArmyBuilder

    doctrines = {ArmyBuilder(seed=s).doctrine for s in range(10)}
    assert len(doctrines) >= 3, f"doctrines barely vary across seeds: {doctrines}"
    assert doctrines <= set(DOCTRINES)
    b = ArmyBuilder(seed=3)
    sysprompt = b.system_prompt(db)
    assert b.doctrine in sysprompt                 # the per-game directive
    assert "Special abilities" in sysprompt        # official card text
    assert "Formations" in sysprompt or "formation" in sysprompt  # rules digest


def test_heuristic_army_concentrates_factions_for_formations(db):
    """The heuristic drafter builds same-faction, formation-capable blocks so
    movement formations (3-5 same-faction, P4-R11..12) are actually possible."""
    from collections import Counter

    from clixengine.build import _formation_capable, heuristic_army

    for seed in (5, 11, 42, 101, 303):
        army = heuristic_army(db, "llm", 200, seed)
        figs = [db.get(i) for i in army.figure_ids]
        capable = Counter(f.faction for f in figs if _formation_capable(f))
        assert capable and max(capable.values()) >= 3, (
            f"seed {seed}: no 3+ formation-capable same-faction block: "
            f"{[(f.short_name, f.faction) for f in figs]}"
        )


def test_deploy_line_groups_factions_adjacently(db):
    """build_game orders each deploy line by faction so faction-mates start in
    base contact and can move as a formation on turn one."""
    from clixengine.army import Army
    from clixengine.candidates import generate_formation_candidates
    from clixengine.setup import build_game

    # Deliberately interleave two factions in draft order.
    utem = [f for f in db.all_figures()
            if f.faction == "Atlantis Guild" and not f.is_unique][:3]
    orcs = [f for f in db.all_figures()
            if f.faction == "Orc Raiders" and not f.is_unique][:2]
    assert len(utem) == 3 and len(orcs) == 2
    ids = [utem[0].id, orcs[0].id, utem[1].id, orcs[1].id, utem[2].id]
    llm = Army("llm-army", "llm", ids)
    human = Army("h-army", "human", [utem[0].id])
    eng = build_game(human, llm, 400, seed=7)

    # Faction-mates occupy contiguous x positions in the line.
    line = sorted((f for f in eng.state.figures.values() if f.owner == "llm"),
                  key=lambda f: f.position.x)
    factions = [f.definition.faction for f in line]
    for fac in set(factions):
        idxs = [i for i, x in enumerate(factions) if x == fac]
        assert idxs == list(range(idxs[0], idxs[-1] + 1)), f"{fac} split: {factions}"

    # And the 3-strong Atlantis block yields a turn-one formation move.
    eng.state.active_player = "llm"
    eng._begin_player_turn("llm")
    kinds = {c.kind for c in generate_formation_candidates(eng, "llm")}
    assert "formation_move" in kinds


def test_drafter_and_battle_prompts_teach_formations(db):
    from clixengine.ai.llm import _SYSTEM as battle_system
    from clixengine.build import _SYSTEM as draft_system

    assert "SAME-FACTION" in draft_system          # draft for formations
    assert "formation_move" in battle_system       # value the candidate in play
    assert "ranged_formation" in battle_system
