"""Lancers ingest invariants (docs/lancers-plan.md §1 / §5)."""

import json
from collections import Counter
from pathlib import Path

from clixengine import abilities as ab
from clixengine.build import POOL_EXPANSIONS, pool_figures
from clixengine.data import load_db

STATS = Path(__file__).resolve().parent.parent / "stats"

# The 22 known mounted sculpt groups (16 W/S/T trios + 6 uniques) — pinned so a
# future re-ingest that misclassifies mounted units fails loudly.
MOUNTED_SHORT_NAMES = {
    "Ankhar Archer On Ankhar", "Ankhar Butcher On Ankhar",
    "Cave Archer On Cave Runner", "Cave Butcher On Cave Runner",
    "Light Lancer On Light Warhorse", "Heavy Lancer On Heavy Warhorse",
    "Light Cavalier On Light Warhorse", "Heavy Cavalier On Heavy Warhorse",
    "Champion On Heavy Warhorse", "Martyr On Light Warhorse",
    "Fell Banshee On Skeletal Fell Beast", "Fell Reaper On Skeletal Fell Beast",
    "King Of The Dead On Skeletal Fell Beast",
    "Nightmare Banshee On Nightmare", "Nightmare Reaper On Nightmare",
    "Uhlrik Charger On Nightmare",
    "Scorpem Crossbowman On Scorpion Mount", "Scorpem Gunner On Scorpion Mount",
    "High Battle Mage On Scorpion Mount",
    "Soaring Crossbowman On Dragonfly Mount", "Soaring Gunner On Dragonfly Mount",
    "Techun On Dragonfly Mount",
}


def _lancers(db):
    return [f for f in db.all_figures() if f.expansion == "Lancers"]


def test_lancers_counts_and_ranks(db):
    figs = _lancers(db)
    assert len(figs) == 142
    assert Counter(f.rank for f in figs) == {
        "Weak": 44, "Standard": 44, "Tough": 44, "Unique": 10,
    }
    ids = {f.id for f in figs}
    assert min(ids) == 8969 and max(ids) == 9110 and len(ids) == 142


def test_mounted_identification(db):
    mounted = [f for f in _lancers(db) if f.mounted]
    assert len(mounted) == 54
    assert {f.short_name for f in mounted} == MOUNTED_SHORT_NAMES
    # No foot unit slipped in, no rider name without the flag.
    for f in _lancers(db):
        assert f.mounted == (" On " in f.name)
    # Rebellion stays unmounted.
    assert not any(f.mounted for f in db.all_figures() if f.expansion == "Rebellion")


def test_corrupt_dead_click_dials_truncated(db):
    """Ids 9091/9110 pad dead clicks as zero-stat entries with ability 93 in
    the raw feed — the ingest must not emit zombie live clicks."""
    assert db.get(9091).num_live_clicks == 7   # Cave Butcher On Cave Runner ***
    assert db.get(9110).num_live_clicks == 9   # Arcane Draconum Unique
    for f in _lancers(db):
        for c in f.dial:
            assert c.speed or c.attack or c.defense or c.damage, f.name


def test_multi_target_and_arc_parsing(db):
    two_target = [f for f in _lancers(db) if f.targets == 2]
    assert len(two_target) == 4
    hbm = [f for f in two_target if f.short_name == "High Battle Mage On Scorpion Mount"]
    assert hbm and hbm[0].arc_deg == 270.0 and hbm[0].range == 12 and hbm[0].mounted


def test_no_id_collisions_across_sets(db):
    ids = [f.id for f in db.all_figures()]
    assert len(ids) == len(set(ids))
    rebellion = {f.id for f in db.all_figures() if f.expansion == "Rebellion"}
    lancers = {f.id for f in db.all_figures() if f.expansion == "Lancers"}
    assert rebellion.isdisjoint(lancers) and len(rebellion) == 160


def test_ability_coverage(db):
    """Every ability on a Lancers dial is implemented or explicitly flagged
    (Battle Fury 88 is capture-pending — counted as flagged, not silent)."""
    used = set()
    for f in _lancers(db):
        used |= f.all_ability_ids()
    known = ab.IMPLEMENTED_ABILITY_IDS | ab.CAPTURE_PENDING_IDS | ab.FLAGGED_ABILITY_IDS
    missing = used - known
    assert not missing, f"unimplemented+unflagged ability ids on Lancers dials: {missing}"
    # The genuinely new-to-engine trio is present in the data as expected.
    assert {ab.BOUND, ab.CHARGE, ab.INVULNERABILITY} <= used


def test_pool_scoping(db):
    """Pools honour the active expansion selection; the default (both sets)
    offers all 302 figures, and a Rebellion-only scope excludes cavalry."""
    from clixengine.build import set_pool_expansions
    try:
        set_pool_expansions(None)
        assert {f.expansion for f in pool_figures(db)} == POOL_EXPANSIONS
        assert len(pool_figures(db)) == 302
        set_pool_expansions(["Rebellion"])
        assert len(pool_figures(db)) == 160
        set_pool_expansions(["Lancers"])
        assert len(pool_figures(db)) == 142
        assert all(f.expansion == "Lancers" for f in pool_figures(db))
    finally:
        set_pool_expansions(None)  # restore the default for other tests


def test_used_in_lancers_flags():
    raw = json.loads((STATS / "special_abilities.json").read_text())
    flagged = {a["id"] for a in raw["abilities"] if a.get("used_in_lancers")}
    assert {90, 91, 101} <= flagged  # Bound, Charge, Invulnerability
    assert 120 not in flagged        # Starting Position: used on zero clicks
