from clixengine.data import load_db


def test_roster_counts(db):
    figs = db.all_figures()
    assert len(figs) == 160
    factions = db.factions()
    assert len(factions) == 8


def test_abilities_loaded(db):
    abils = db.all_abilities()
    assert len(abils) == 42  # 41 real + id 85 blank
    tough = db.ability(123)
    assert tough is not None and tough.name == "Toughness"


def test_used_in_rebellion_flag_matches_dials(db):
    flagged = {a.id for a in db.all_abilities() if a.used_in_rebellion}
    used = set()
    for f in db.all_figures():
        used |= f.all_ability_ids()
    assert used == flagged
    assert len(used) == 24


def test_points_range(db):
    pts = [f.points for f in db.all_figures()]
    assert min(pts) == 5
    assert max(pts) == 145


def test_ranged_targets_parse(db):
    ranged = db.filter(ranged=True)
    assert len(ranged) == 78
    multi = [f for f in db.all_figures() if f.targets > 1]
    assert len(multi) == 7


def test_load_db_cached():
    assert load_db() is load_db()  # lru_cache
