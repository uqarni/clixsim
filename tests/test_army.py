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
