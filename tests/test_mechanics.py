"""Optional-ability cancel (P4-R34) + attack modifier breakdown (explain_attack)."""

import math

import pytest

import clixengine.abilities as ab
from clixengine.intents import ToggleAbilityIntent

from .conftest import build_engine


# --- optional-ability cancel ------------------------------------------------
def _optional_on_click(f):
    return [a for a in f.definition.dial[f.current_click].abilities if a.optional]


def test_toggle_cancels_and_restores_optional_ability(db):
    e = build_engine(db, [
        ("human", "Magus Draconum", (10, 10), 0.0, 0),
        ("llm", "Werebear", (20, 20), 0.0, 0),
    ], active="human")
    f = e.state.figure(0)
    opt = _optional_on_click(f)
    if not opt:
        pytest.skip("no optional ability on the starting click")
    aid = opt[0].id
    assert aid in f.active_ability_ids()

    r = e.apply(ToggleAbilityIntent(0, aid, off=True))
    assert r.ok
    assert aid not in f.active_ability_ids()   # cancelled
    assert 0 not in e._acted_uids              # not an action — no token, no acted-mark
    assert e._actions_remaining() == e.state.actions_per_turn()

    r2 = e.apply(ToggleAbilityIntent(0, aid, off=False))
    assert r2.ok and aid in f.active_ability_ids()


def test_toggle_cleared_at_owner_turn_start(db):
    e = build_engine(db, [
        ("human", "Magus Draconum", (10, 10), 0.0, 0),
        ("llm", "Werebear", (20, 20), 0.0, 0),
    ], active="human")
    f = e.state.figure(0)
    opt = _optional_on_click(f)
    if not opt:
        pytest.skip("no optional ability")
    aid = opt[0].id
    e.apply(ToggleAbilityIntent(0, aid, off=True))
    assert aid in f.disabled_ability_ids
    f.begin_owner_turn()  # cancellations last only until end of turn (P4-R34)
    assert aid not in f.disabled_ability_ids


def test_toggle_rejects_ability_not_on_click(db):
    e = build_engine(db, [("human", "Werebear", (10, 10), 0.0, 0)], active="human")
    r = e.apply(ToggleAbilityIntent(0, ab.MAGIC_BLAST, off=True))  # Werebear has no Magic Blast
    assert not r.ok and r.reason in ("no_ability", "not_optional")


# --- attack modifier breakdown ---------------------------------------------
def test_explain_shows_toughness_reduction(db):
    e = build_engine(db, [
        ("human", "Grave Robber", (10, 10), 0.0, 0),   # damage 1
        ("llm", "Troll Brawler", (11.1, 10), math.pi, 0),  # Toughness
    ], active="human")
    x = e.explain_attack(0, 1, "close")
    assert x["damage"]["base"] == 1
    assert x["damage"]["toughness"] == -1
    assert x["damage"]["per_hit"] == 0  # 1 - 1, floored at 0


def test_explain_shows_defend_share(db):
    e = build_engine(db, [
        ("human", "Crystal Bladesman", (18.9, 20.0), 0.0, 0),
        ("llm", "Werewolf", (20.0, 20.0), 0.0, 0),        # printed def 12
        ("llm", "Elemental Priest", (20.0, 21.1), 0.0, 0),  # Defend, def 18 (base contact)
    ], active="human")
    x = e.explain_attack(0, 1, "close")
    assert x["defense"]["base"] == 12
    assert x["defense"]["defend"] > 0
    assert x["defense"]["effective"] == 18


def test_explain_shows_battle_armor_vs_ranged(db):
    e = build_engine(db, [
        ("human", "Utem Crossbowman", (10, 10), math.pi / 2, 0),
        ("llm", "Storm Golem", (10, 16), -math.pi / 2, 0),  # Battle Armor
    ], active="human")
    assert ab.BATTLE_ARMOR in e.state.figure(1).active_ability_ids()
    x = e.explain_attack(0, 1, "ranged")
    assert x["defense"]["battle_armor"] == 2
    assert x["defense"]["effective"] == x["defense"]["base"] + 2


def test_explain_shows_magic_enhancement(db):
    e = build_engine(db, [
        ("human", "Utem Crossbowman", (0, 0), 0.0, 0),
        ("human", "Shaman", (0, 1.1), 0.0, 0),          # Magic Enhancement, in contact
        ("llm", "Utem Crossbowman", (5, 0), math.pi, 0),
    ], active="human")
    x = e.explain_attack(0, 2, "ranged")
    assert x["damage"]["enhancement"] == 1
    assert x["damage"]["per_hit"] == x["damage"]["base"] + 1
