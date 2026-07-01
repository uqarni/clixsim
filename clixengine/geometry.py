"""Continuous-space geometry for the Mage Knight engine.

All positions are floats in inches; there is no grid (DP3). Facing is an angle in
radians measured counter-clockwise from the +x axis. Figures occupy circular
bases of ``base_radius`` inches centred on ``position`` (the dial's centre dot).

Every spatial predicate the rules need lives here so the engine — not the LLM —
owns geometry (DP2). Functions are pure and deterministic.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

# Tolerance for "close enough" base-contact / on-segment decisions
# (see PRD P4-R39 / §Etiquette rule 3). Distances within this many inches are
# treated as touching.
CONTACT_EPS = 1e-6


@dataclass(frozen=True)
class Vec:
    x: float
    y: float

    def __add__(self, o: "Vec") -> "Vec":
        return Vec(self.x + o.x, self.y + o.y)

    def __sub__(self, o: "Vec") -> "Vec":
        return Vec(self.x - o.x, self.y - o.y)

    def __mul__(self, s: float) -> "Vec":
        return Vec(self.x * s, self.y * s)

    __rmul__ = __mul__

    def dot(self, o: "Vec") -> float:
        return self.x * o.x + self.y * o.y

    def length(self) -> float:
        return math.hypot(self.x, self.y)

    def as_tuple(self) -> tuple[float, float]:
        return (self.x, self.y)


def distance(a: Vec, b: Vec) -> float:
    """Centre-to-centre distance in inches (§Measurements)."""
    return (a - b).length()


def edge_distance(a: Vec, ra: float, b: Vec, rb: float) -> float:
    """Gap between two circular base edges. Negative if they overlap."""
    return distance(a, b) - ra - rb


def in_base_contact(a: Vec, ra: float, b: Vec, rb: float, eps: float = CONTACT_EPS) -> bool:
    """True if two bases touch or overlap (edge gap <= eps)."""
    return edge_distance(a, ra, b, rb) <= eps


def normalize_angle(theta: float) -> float:
    """Wrap an angle to (-pi, pi]."""
    theta = math.fmod(theta, 2 * math.pi)
    if theta <= -math.pi:
        theta += 2 * math.pi
    elif theta > math.pi:
        theta -= 2 * math.pi
    return theta


def angle_to(origin: Vec, target: Vec) -> float:
    """Absolute bearing from ``origin`` to ``target`` in radians."""
    d = target - origin
    return math.atan2(d.y, d.x)


def in_front_arc(origin: Vec, facing: float, target: Vec, half_angle: float) -> bool:
    """Is ``target`` inside the front wedge of a figure at ``origin``?

    ``half_angle`` is the half-width of the front arc in radians: the front arc
    spans ``facing ± half_angle``. A target at the same point as the origin is
    considered in-arc (degenerate). See OQ-5 for the arc convention — the caller
    supplies the resolved half-angle.
    """
    d = target - origin
    if d.length() <= CONTACT_EPS:
        return True
    bearing = math.atan2(d.y, d.x)
    delta = abs(normalize_angle(bearing - facing))
    return delta <= half_angle + CONTACT_EPS


def in_rear_arc(origin: Vec, facing: float, target: Vec, half_angle: float) -> bool:
    """Is ``target`` inside the rear wedge (everything not in the front arc)?"""
    d = target - origin
    if d.length() <= CONTACT_EPS:
        return False
    return not in_front_arc(origin, facing, target, half_angle)


def segment_circle_intersects(
    p0: Vec, p1: Vec, centre: Vec, radius: float, eps: float = CONTACT_EPS
) -> bool:
    """Does the segment p0->p1 pass within ``radius`` of ``centre``?

    Used for line-of-fire blocking: a shot centre->centre is blocked if it grazes
    an intervening base. Endpoints exactly on the circle count as touching.
    """
    d = p1 - p0
    seg_len_sq = d.dot(d)
    if seg_len_sq <= eps * eps:
        # Degenerate segment: treat as a point.
        return distance(p0, centre) <= radius + eps
    # Project centre onto the segment, clamped to [0, 1].
    t = ((centre - p0).dot(d)) / seg_len_sq
    t = max(0.0, min(1.0, t))
    closest = p0 + d * t
    return distance(closest, centre) <= radius + eps


def path_crosses_base(
    p0: Vec, p1: Vec, centre: Vec, radius: float, eps: float = CONTACT_EPS
) -> bool:
    """Alias for movement-path blocking: a straight path from p0 to p1 may not
    cross a figure base (§Movement, P4-R6). Semantically identical to
    ``segment_circle_intersects`` but named for the movement rule."""
    return segment_circle_intersects(p0, p1, centre, radius, eps)


# --------------------------------------------------------------------------- #
# Polygon geometry (terrain features are convex/simple polygons in world space)
# --------------------------------------------------------------------------- #
def rotate_point(p: Vec, centre: Vec, angle: float) -> Vec:
    """Rotate ``p`` about ``centre`` by ``angle`` radians (CCW)."""
    dx, dy = p.x - centre.x, p.y - centre.y
    c, s = math.cos(angle), math.sin(angle)
    return Vec(centre.x + dx * c - dy * s, centre.y + dx * s + dy * c)


def rotate_polygon(poly: tuple[Vec, ...], centre: Vec, angle: float) -> tuple[Vec, ...]:
    """Rotate a polygon's vertices about ``centre`` (used to bake placement rotation)."""
    return tuple(rotate_point(v, centre, angle) for v in poly)


def polygon_centroid(poly: tuple[Vec, ...]) -> Vec:
    n = len(poly)
    return Vec(sum(v.x for v in poly) / n, sum(v.y for v in poly) / n)


def point_in_polygon(p: Vec, poly: tuple[Vec, ...]) -> bool:
    """Ray-casting point-in-(simple)-polygon test. Boundary counts as inside-ish
    within CONTACT_EPS is NOT guaranteed; use for interior classification."""
    inside = False
    n = len(poly)
    j = n - 1
    for i in range(n):
        pi, pj = poly[i], poly[j]
        if (pi.y > p.y) != (pj.y > p.y):
            x_int = (pj.x - pi.x) * (p.y - pi.y) / (pj.y - pi.y) + pi.x
            if p.x < x_int:
                inside = not inside
        j = i
    return inside


def _orient(a: Vec, b: Vec, c: Vec) -> float:
    return (b.x - a.x) * (c.y - a.y) - (b.y - a.y) * (c.x - a.x)


def _on_segment(a: Vec, b: Vec, p: Vec, eps: float = CONTACT_EPS) -> bool:
    if abs(_orient(a, b, p)) > eps * max(1.0, distance(a, b)):
        return False
    return (min(a.x, b.x) - eps <= p.x <= max(a.x, b.x) + eps and
            min(a.y, b.y) - eps <= p.y <= max(a.y, b.y) + eps)


def segments_intersect(p1: Vec, p2: Vec, p3: Vec, p4: Vec, eps: float = CONTACT_EPS) -> bool:
    """True if segments p1-p2 and p3-p4 intersect (proper or touching)."""
    d1, d2 = _orient(p3, p4, p1), _orient(p3, p4, p2)
    d3, d4 = _orient(p1, p2, p3), _orient(p1, p2, p4)
    if ((d1 > eps) != (d2 > eps)) and ((d3 > eps) != (d4 > eps)):
        # strictly opposite sides both ways => proper crossing
        if (d1 > 0) != (d2 > 0) and (d3 > 0) != (d4 > 0):
            return True
    return (_on_segment(p3, p4, p1, eps) or _on_segment(p3, p4, p2, eps)
            or _on_segment(p1, p2, p3, eps) or _on_segment(p1, p2, p4, eps))


def polygon_edges(poly: tuple[Vec, ...]):
    n = len(poly)
    return [(poly[i], poly[(i + 1) % n]) for i in range(n)]


def segment_crosses_polygon(p0: Vec, p1: Vec, poly: tuple[Vec, ...]) -> bool:
    """True if the segment p0->p1 enters or crosses the polygon (either endpoint
    inside, or the segment intersects a boundary edge). Used for line-of-fire
    blocking against blocking terrain."""
    if point_in_polygon(p0, poly) or point_in_polygon(p1, poly):
        return True
    return any(segments_intersect(p0, p1, a, b) for a, b in polygon_edges(poly))


def point_segment_distance(p: Vec, a: Vec, b: Vec) -> float:
    ab = b - a
    l2 = ab.dot(ab)
    if l2 <= 0:
        return distance(p, a)
    t = max(0.0, min(1.0, (p - a).dot(ab) / l2))
    return distance(p, a + ab * t)


def segment_segment_distance(a0: Vec, a1: Vec, b0: Vec, b1: Vec) -> float:
    if segments_intersect(a0, a1, b0, b1):
        return 0.0
    return min(
        point_segment_distance(a0, b0, b1), point_segment_distance(a1, b0, b1),
        point_segment_distance(b0, a0, a1), point_segment_distance(b1, a0, a1),
    )


def circle_intersects_polygon(c: Vec, r: float, poly: tuple[Vec, ...], eps: float = CONTACT_EPS) -> bool:
    """A base of radius r centred at c overlaps the polygon (touches or is inside)."""
    if point_in_polygon(c, poly):
        return True
    return any(point_segment_distance(c, a, b) <= r + eps for a, b in polygon_edges(poly))


def circle_in_polygon(c: Vec, r: float, poly: tuple[Vec, ...], eps: float = CONTACT_EPS) -> bool:
    """The whole base of radius r centred at c lies inside the polygon."""
    if not point_in_polygon(c, poly):
        return False
    return all(point_segment_distance(c, a, b) >= r - eps for a, b in polygon_edges(poly))


def swept_base_crosses_polygon(p0: Vec, p1: Vec, r: float, poly: tuple[Vec, ...], eps: float = CONTACT_EPS) -> bool:
    """A base of radius r sweeping from p0 to p1 touches the polygon at any point
    (capsule vs polygon). Used to forbid a move that would enter blocking terrain."""
    if circle_intersects_polygon(p0, r, poly, eps) or circle_intersects_polygon(p1, r, poly, eps):
        return True
    if segment_crosses_polygon(p0, p1, poly):
        return True
    return any(segment_segment_distance(p0, p1, a, b) <= r + eps for a, b in polygon_edges(poly))


def polygon_is_simple(poly: tuple[Vec, ...]) -> bool:
    """True if the polygon is a simple (non-self-intersecting) ring of >=3 vertices.
    Adjacent edges (sharing a vertex) may touch; any other edge crossing => not simple.
    Used to validate a hand-drawn terrain polygon before accepting it."""
    n = len(poly)
    if n < 3:
        return False
    edges = polygon_edges(poly)
    for i in range(n):
        a0, a1 = edges[i]
        for j in range(i + 1, n):
            if j == i or (i + 1) % n == j or (j + 1) % n == i:
                continue  # adjacent edges share a vertex — skip
            b0, b1 = edges[j]
            if segments_intersect(a0, a1, b0, b1):
                return False
    return True


def polygon_polygon_distance(a: tuple[Vec, ...], b: tuple[Vec, ...]) -> float:
    """Minimum gap between two polygons (0 if they overlap). Used for the >=2\"
    terrain-placement spacing rule."""
    if any(point_in_polygon(v, b) for v in a) or any(point_in_polygon(v, a) for v in b):
        return 0.0
    best = float("inf")
    for ea in polygon_edges(a):
        for eb in polygon_edges(b):
            best = min(best, segment_segment_distance(ea[0], ea[1], eb[0], eb[1]))
    return best
