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


def test_pick_fallback_concentrates_factions_too(db):
    """ArmyBuilder.pick's heuristic fallback (heuristic opponent / no API key)
    applies the same formation-aware narrowing as heuristic_army — this is the
    draft path the server actually uses for the AI army."""
    from collections import Counter

    from clixengine.build import ArmyBuilder, _affordable, _fig_brief, _formation_capable

    for seed in (1, 4, 7, 12):
        b = ArmyBuilder.__new__(ArmyBuilder)
        b.available = False
        ids, used_uniques, remaining, brief = [], set(), 200, []
        for step in range(12):
            cands = _affordable(db, None, remaining, used_uniques, None)
            if not cands:
                break
            fig, _, used_llm = ArmyBuilder.pick(b, db, cands, brief, remaining, 200,
                                                seed=seed * 100 + step)
            assert not used_llm
            if fig is None:
                break
            ids.append(fig.id)
            remaining -= fig.points
            if fig.is_unique:
                used_uniques.add(fig.id)
            brief.append(_fig_brief(db, fig))
        capable = Counter(db.get(i).faction for i in ids if _formation_capable(db.get(i)))
        assert capable and max(capable.values()) >= 3, (
            f"seed {seed}: fallback draft lacks a formation block: "
            f"{[db.get(i).short_name for i in ids]}"
        )


def test_sealed_heuristic_army_spends_its_budget(db):
    """The faction lock must not strand sealed-pool budget: the top-up pass
    relaxes it and keeps buying (regression: avg spend fell 160 -> 131)."""
    from clixengine.build import heuristic_army, sample_sealed_pool

    spends = []
    for seed in range(8):
        pool = sample_sealed_pool(db, seed)
        a = heuristic_army(db, "human", 200, seed, candidate_ids=pool)
        spent = sum(db.get(i).points for i in a.figure_ids)
        assert spent <= 200
        spends.append(spent)
    assert min(spends) >= 160, f"sealed army badly under budget: {spends}"


def test_battle_prompt_close_formation_cohesion_is_correct(db):
    """P4-R29: close-formation members need not touch each other (only the
    target); the prompt must not teach a member-cohesion prerequisite."""
    from clixengine.ai.llm import _SYSTEM

    assert "not each other" in _SYSTEM


def test_draft_planning_pass_commits_to_a_formation_faction(db):
    """The up-front planning pass (user request): take stock, decide a primary
    faction with a real formation, and hand that to the pick loop."""
    from clixengine.build import ArmyBuilder, _planning_digest, sample_sealed_pool

    pool = [db.get(i) for i in sorted(set(sample_sealed_pool(db, 55)))]
    b = ArmyBuilder.__new__(ArmyBuilder)
    b.available = False
    b.plan = {}
    b.doctrine = "Gunline: massed shooters."
    plan = b.make_plan(db, pool, 200)
    assert plan.get("primary_faction")
    # The chosen faction must actually have >=3 formation-capable figures.
    digest = {d["faction"]: d for d in _planning_digest(db, pool)}
    assert digest[plan["primary_faction"]]["formation_capable"] >= 3


def test_plan_steers_the_heuristic_fallback_first_pick(db):
    """A plan's primary faction biases the fallback's opening pick even before
    any figure is drafted (empty army)."""
    from clixengine.build import ArmyBuilder, _affordable

    b = ArmyBuilder.__new__(ArmyBuilder)
    b.available = False
    b.plan = {"primary_faction": "Necropolis Sect"}
    b.doctrine = "x"
    cands = _affordable(db, None, 200, set(), None)
    if not any(c.faction == "Necropolis Sect" for c in cands):
        import pytest
        pytest.skip("faction not affordable in roster")
    fig, _reason, used_llm = b.pick(db, cands, [], 200, 200, seed=1)
    assert not used_llm and fig.faction == "Necropolis Sect"


def test_planning_digest_counts_flying_shooters_for_ranged_formations(db):
    """Ranged formations have no Flight bar — an all-flying ranged faction
    (Draconum) must report ranged_formation_capable > 0, and the heuristic plan
    must never name a MOVEMENT formation for it."""
    from clixengine.build import _planning_digest, _heuristic_plan

    # Rebellion Draconum only: the premise is an ALL-flying faction, and the
    # four grounded Lancers Draconum would (correctly) unlock movement formations.
    draconum = [f for f in db.all_figures()
                if f.faction == "Draconum" and f.expansion == "Rebellion"]
    digest = {d["faction"]: d for d in _planning_digest(db, draconum)}
    d = digest["Draconum"]
    assert d["ranged_formation_capable"] >= 3, "flying Draconum shooters miscounted"
    assert d["movement_formation_capable"] == 0, "Draconum all fly — no movement formation"
    plan = _heuristic_plan(db, draconum, "x")
    assert plan["primary_faction"] == "Draconum"
    assert "movement" not in plan["formation_plan"], plan["formation_plan"]


def test_make_plan_falls_back_on_non_dict_llm_output(db, monkeypatch):
    """A valid-JSON non-dict (or a keyless dict) from the model must route to the
    heuristic plan, never crash the pick loop with an AttributeError."""
    from clixengine.build import ArmyBuilder, sample_sealed_pool

    pool = [db.get(i) for i in sorted(set(sample_sealed_pool(db, 3)))]
    b = ArmyBuilder.__new__(ArmyBuilder)
    b.available = True
    b.plan = {}
    b.doctrine = "x"
    b.model = "m"
    b.effort = "low"
    b.last_error = ""

    class _Blk:
        type = "text"
        def __init__(self, t): self.text = t
    class _Resp:
        def __init__(self, t): self.content = [_Blk(t)]
    class _Msgs:
        def __init__(self, t): self._t = t
        def create(self, **kw): return _Resp(self._t)
    class _Client:
        def __init__(self, t): self.messages = _Msgs(t)

    for bad in ("[1, 2, 3]", "42", '{"unexpected": "keys"}'):
        b._client = _Client(bad)
        b.plan = {}
        plan = b.make_plan(db, pool, 200)
        assert isinstance(plan, dict)
        # A usable plan (heuristic fallback) or empty — never a list/scalar.
        assert plan == {} or plan.get("primary_faction")
        # And pick() must not raise on it.
        from clixengine.build import _affordable
        cands = _affordable(db, [f.id for f in pool], 200, set(),
                            {f.id: 1 for f in pool})
        b.available = False  # force the fallback pick path that reads self.plan
        fig, _r, _u = b.pick(db, cands, [], 200, 200, seed=1)
        assert fig is not None
        b.available = True
