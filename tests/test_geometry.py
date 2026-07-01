import math

import pytest

from clixengine.geometry import (
    Vec,
    angle_to,
    distance,
    edge_distance,
    in_base_contact,
    in_front_arc,
    in_rear_arc,
    normalize_angle,
    path_crosses_base,
    segment_circle_intersects,
)


def test_distance_and_edges():
    a, b = Vec(0, 0), Vec(3, 4)
    assert distance(a, b) == pytest.approx(5.0)
    assert edge_distance(a, 1.0, b, 1.0) == pytest.approx(3.0)


def test_base_contact_touch_and_overlap():
    a, b = Vec(0, 0), Vec(1.1, 0)
    assert in_base_contact(a, 0.55, b, 0.55)  # edges just touching
    far = Vec(2.0, 0)
    assert not in_base_contact(a, 0.55, far, 0.55)
    overlap = Vec(0.5, 0)
    assert in_base_contact(a, 0.55, overlap, 0.55)


def test_normalize_angle():
    assert normalize_angle(3 * math.pi) == pytest.approx(math.pi)
    assert normalize_angle(-3 * math.pi) == pytest.approx(math.pi)
    assert normalize_angle(0.0) == pytest.approx(0.0)


def test_front_arc_90_half_angle_is_front_hemisphere():
    # arc half-angle = 90deg => front spans facing +/- 90deg (front hemisphere).
    origin = Vec(0, 0)
    facing = 0.0  # facing +x
    ha = math.radians(90)
    assert in_front_arc(origin, facing, Vec(5, 0), ha)  # dead ahead
    assert in_front_arc(origin, facing, Vec(0, 5), ha)  # 90deg left (boundary)
    assert in_front_arc(origin, facing, Vec(0, -5), ha)  # 90deg right (boundary)
    assert not in_front_arc(origin, facing, Vec(-5, 0.001), ha)  # behind
    assert in_rear_arc(origin, facing, Vec(-5, 0), ha)


def test_front_arc_180_is_all_around():
    origin = Vec(0, 0)
    ha = math.radians(180)
    for pt in (Vec(1, 0), Vec(-1, 0), Vec(0, -1), Vec(-1, -1)):
        assert in_front_arc(origin, 0.0, pt, ha)
        assert not in_rear_arc(origin, 0.0, pt, ha)


def test_line_of_fire_blocking():
    p0, p1 = Vec(0, 0), Vec(10, 0)
    # A base sitting on the line blocks.
    assert segment_circle_intersects(p0, p1, Vec(5, 0), 0.55)
    # A base off to the side does not.
    assert not segment_circle_intersects(p0, p1, Vec(5, 2), 0.55)
    # A base grazing within radius blocks.
    assert segment_circle_intersects(p0, p1, Vec(5, 0.5), 0.55)


def test_path_crossing_alias():
    assert path_crosses_base(Vec(0, 0), Vec(4, 0), Vec(2, 0), 0.55)
    assert not path_crosses_base(Vec(0, 0), Vec(4, 0), Vec(2, 3), 0.55)


def test_angle_to():
    assert angle_to(Vec(0, 0), Vec(1, 0)) == pytest.approx(0.0)
    assert angle_to(Vec(0, 0), Vec(0, 1)) == pytest.approx(math.pi / 2)
