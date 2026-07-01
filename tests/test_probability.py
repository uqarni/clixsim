import pytest

from clixengine.probability import (
    crit_hit_probability,
    expected_clicks,
    hit_probability,
    outcome,
)


def test_hit_probability_bounds():
    # Impossible-to-beat defense still hits on a natural 12 (1/36).
    assert hit_probability(0, 100) == pytest.approx(1 / 36)
    # Trivially low defense still misses on a natural 2 (35/36 hit).
    assert hit_probability(100, 0) == pytest.approx(35 / 36)


def test_hit_probability_need_7():
    # need = defense - attack = 7 => rolls 7..12 hit (21 ways) => but 2 excluded,
    # 12 auto-hit already counted. 7:6,8:5,9:4,10:3,11:2,12:1 = 21/36.
    assert hit_probability(3, 10) == pytest.approx(21 / 36)


def test_crit_probability():
    assert crit_hit_probability() == pytest.approx(1 / 36)


def test_expected_clicks_uses_crit_bonus():
    # attack 3 vs defense 10 (need 7), damage 2.
    p = hit_probability(3, 10)
    pc = crit_hit_probability()
    exp = (p - pc) * 2 + pc * 3
    assert expected_clicks(3, 10, 2) == pytest.approx(exp)


@pytest.mark.parametrize(
    "d1,d2,attack,defense,expected",
    [
        (1, 1, 10, 5, "crit_miss"),  # natural 2 always misses
        (6, 6, 0, 100, "crit_hit"),  # natural 12 always hits
        (3, 3, 4, 10, "hit"),  # 6 + 4 = 10 >= 10
        (3, 3, 3, 10, "miss"),  # 6 + 3 = 9 < 10
    ],
)
def test_outcome(d1, d2, attack, defense, expected):
    assert outcome(d1, d2, attack, defense) == expected
