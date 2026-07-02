// Client-side terrain geometry — mirrors clixengine/terrain.placement_reason so
// the placement ghost can show live green/red before the server confirms. The
// engine remains the source of truth; this is only for responsive feedback.

import type { TerrainPiece, TerrainTemplate } from "./api";

export type Pt = [number, number];

function rot(p: Pt, a: number): Pt {
  const c = Math.cos(a);
  const s = Math.sin(a);
  return [p[0] * c - p[1] * s, p[0] * s + p[1] * c];
}

// A template's polygon baked at a center with a rotation (world inches).
export function placedPolygon(tmpl: TerrainTemplate, center: Pt, rotation: number): Pt[] {
  return tmpl.polygon.map((v) => {
    const r = rot(v as Pt, rotation);
    return [r[0] + center[0], r[1] + center[1]] as Pt;
  });
}

export function placedAccessPoints(tmpl: TerrainTemplate, center: Pt, rotation: number): Pt[] {
  return tmpl.access_points.map((v) => {
    const r = rot(v as Pt, rotation);
    return [r[0] + center[0], r[1] + center[1]] as Pt;
  });
}

function pointInPoly(p: Pt, poly: Pt[]): boolean {
  let inside = false;
  for (let i = 0, j = poly.length - 1; i < poly.length; j = i++) {
    const [xi, yi] = poly[i];
    const [xj, yj] = poly[j];
    if (yi > p[1] !== yj > p[1]) {
      const x = ((xj - xi) * (p[1] - yi)) / (yj - yi) + xi;
      if (p[0] < x) inside = !inside;
    }
  }
  return inside;
}

function segDist(a0: Pt, a1: Pt, b0: Pt, b1: Pt): number {
  // segment-to-segment distance (0 if they intersect)
  const d = (u: Pt, v: Pt) => Math.hypot(u[0] - v[0], u[1] - v[1]);
  const ptSeg = (p: Pt, s0: Pt, s1: Pt) => {
    const vx = s1[0] - s0[0];
    const vy = s1[1] - s0[1];
    const l2 = vx * vx + vy * vy;
    if (l2 === 0) return d(p, s0);
    let t = ((p[0] - s0[0]) * vx + (p[1] - s0[1]) * vy) / l2;
    t = Math.max(0, Math.min(1, t));
    return d(p, [s0[0] + t * vx, s0[1] + t * vy]);
  };
  const inter = (p1: Pt, p2: Pt, p3: Pt, p4: Pt) => {
    const o = (a: Pt, b: Pt, c: Pt) => (b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0]);
    const d1 = o(p3, p4, p1);
    const d2 = o(p3, p4, p2);
    const d3 = o(p1, p2, p3);
    const d4 = o(p1, p2, p4);
    return d1 > 0 !== d2 > 0 && d3 > 0 !== d4 > 0;
  };
  if (inter(a0, a1, b0, b1)) return 0;
  return Math.min(ptSeg(a0, b0, b1), ptSeg(a1, b0, b1), ptSeg(b0, a0, a1), ptSeg(b1, a0, a1));
}

function polyDist(a: Pt[], b: Pt[]): number {
  if (a.some((v) => pointInPoly(v, b)) || b.some((v) => pointInPoly(v, a))) return 0;
  let best = Infinity;
  for (let i = 0; i < a.length; i++) {
    const a0 = a[i];
    const a1 = a[(i + 1) % a.length];
    for (let j = 0; j < b.length; j++) {
      best = Math.min(best, segDist(a0, a1, b[j], b[(j + 1) % b.length]));
    }
  }
  return best;
}

function segsIntersect(p1: Pt, p2: Pt, p3: Pt, p4: Pt): boolean {
  const o = (a: Pt, b: Pt, c: Pt) => (b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0]);
  const d1 = o(p3, p4, p1);
  const d2 = o(p3, p4, p2);
  const d3 = o(p1, p2, p3);
  const d4 = o(p1, p2, p4);
  return d1 > 0 !== d2 > 0 && d3 > 0 !== d4 > 0;
}

// A simple (non-self-intersecting) ring of >=3 vertices — mirrors the engine check.
export function polygonSimple(poly: Pt[]): boolean {
  const n = poly.length;
  if (n < 3) return false;
  for (let i = 0; i < n; i++) {
    for (let j = i + 1; j < n; j++) {
      if (j === i || (i + 1) % n === j || (j + 1) % n === i) continue;
      if (segsIntersect(poly[i], poly[(i + 1) % n], poly[j], poly[(j + 1) % n])) return false;
    }
  }
  return true;
}

// --- movement-rule mirrors (clixengine/terrain.py predicates) ---------------
// The engine stays authoritative; these give the drag ghost live truth.
export function blocksMove(t: TerrainPiece): boolean {
  return t.kind === "blocking" || t.water === "deep";
}
function hindersMove(t: TerrainPiece): boolean {
  return !blocksMove(t) && (t.kind === "hindering" || t.water === "shallow" || t.low_wall);
}
function halvesSpeed(t: TerrainPiece): boolean {
  return hindersMove(t) && !t.low_wall;
}

function circleTouchesPoly(c: Pt, r: number, poly: Pt[]): boolean {
  if (pointInPoly(c, poly)) return true;
  for (let i = 0; i < poly.length; i++) {
    const a = poly[i];
    const b = poly[(i + 1) % poly.length];
    if (segDist(c, c, a, b) <= r + 1e-6) return true;
  }
  return false;
}

function capsuleCrossesPoly(p0: Pt, p1: Pt, r: number, poly: Pt[]): boolean {
  if (circleTouchesPoly(p0, r, poly) || circleTouchesPoly(p1, r, poly)) return true;
  if (pointInPoly(p0, poly) || pointInPoly(p1, poly)) return true;
  for (let i = 0; i < poly.length; i++) {
    const a = poly[i];
    const b = poly[(i + 1) % poly.length];
    if (segDist(p0, p1, a, b) <= r + 1e-6) return true;
  }
  return false;
}

// Speed for the turn: halved (round up) when starting in speed-halving hindering.
export function effectiveSpeed(speed: number, pos: Pt, radius: number, terrain: TerrainPiece[]): number {
  const slowed = terrain.some((t) => halvesSpeed(t) && circleTouchesPoly(pos, radius, t.polygon as Pt[]));
  return slowed ? Math.max(1, Math.ceil(speed / 2)) : speed;
}

// Why the engine would reject this move's geometry, or null — mirrors
// _validate_move's terrain rules (endpoint blocking for everyone; path blocking
// and the hindering entry-stop for non-fliers; a stuck start may walk out).
export function moveBlockReason(
  from: Pt,
  dest: Pt,
  radius: number,
  terrain: TerrainPiece[],
  flies: boolean,
): string | null {
  for (const t of terrain) {
    if (blocksMove(t) && circleTouchesPoly(dest, radius, t.polygon as Pt[])) {
      return t.water === "deep" ? "can't end in deep water" : "can't end in blocking terrain";
    }
  }
  if (flies) return null;
  const stuck = terrain.some((t) => blocksMove(t) && circleTouchesPoly(from, radius, t.polygon as Pt[]));
  if (stuck) return null; // escaping an illegal overlap — endpoint check only
  const moving = Math.hypot(dest[0] - from[0], dest[1] - from[1]) > 1e-9;
  if (!moving) return null;
  for (const t of terrain) {
    if (blocksMove(t) && capsuleCrossesPoly(from, dest, radius, t.polygon as Pt[])) {
      return t.water === "deep" ? "path crosses deep water" : "path crosses blocking terrain";
    }
  }
  for (const t of terrain) {
    if (!hindersMove(t)) continue;
    if (circleTouchesPoly(from, radius, t.polygon as Pt[])) continue; // started in it
    if (
      capsuleCrossesPoly(from, dest, radius, t.polygon as Pt[]) &&
      !circleTouchesPoly(dest, radius, t.polygon as Pt[])
    ) {
      return t.low_wall ? "stop at the low wall" : "entering hindering ends the move — stop inside";
    }
  }
  return null;
}

// --- base-contact snapping ---------------------------------------------------
// Shared by the battle drag and the deployment drag: pull a point onto the EXACT
// contact ring of the nearest base when within `window` inches of it. Exactness
// matters — engine contact tolerance is 0.02", so eyeballed gaps don't count.
export function snapToContactRing(
  moverRadius: number,
  dest: Pt,
  targets: { pos: Pt; radius: number; uid: number }[],
  window = 0.9,
): { point: Pt; uid: number } | null {
  let best: { point: Pt; uid: number } | null = null;
  let bestErr = Infinity;
  for (const t of targets) {
    const dx = dest[0] - t.pos[0];
    const dy = dest[1] - t.pos[1];
    const dlen = Math.hypot(dx, dy) || 1e-9;
    const gap = moverRadius + t.radius;
    const err = Math.abs(dlen - gap);
    if (err > window || err >= bestErr) continue;
    const cp: Pt = [t.pos[0] + (dx / dlen) * gap, t.pos[1] + (dy / dlen) * gap];
    const overlaps = targets.some(
      (o) => o.uid !== t.uid && Math.hypot(cp[0] - o.pos[0], cp[1] - o.pos[1]) < moverRadius + o.radius - 0.02,
    );
    if (!overlaps) {
      best = { point: cp, uid: t.uid };
      bestErr = err;
    }
  }
  return best;
}

// --- line-of-fire mirror (clixengine/engine.line_of_fire) --------------------
// Display-only: lets the board show WHERE and WHY a shot is blocked while
// hovering. The engine stays authoritative for what's actually legal.
function segmentCrossesPoly(p0: Pt, p1: Pt, poly: Pt[]): boolean {
  if (pointInPoly(p0, poly) || pointInPoly(p1, poly)) return true;
  for (let i = 0; i < poly.length; i++) {
    if (segsIntersect(p0, p1, poly[i], poly[(i + 1) % poly.length])) return true;
  }
  return false;
}

export interface LofFigure {
  uid: number;
  pos: Pt | [number, number];
  base_radius: number;
  owner: string;
  elevation: number;
  eliminated: boolean;
  short_name: string;
}

export interface LofVerdict {
  clear: boolean;
  reason?: string;
  terrainId?: number; // blocking terrain piece to highlight
  figUid?: number; // blocking/screening figure to highlight
}

export function lofBlocker(
  a: LofFigure & { facing_deg: number; arc_deg: number; range: number },
  t: LofFigure,
  figures: LofFigure[],
  terrain: TerrainPiece[],
): LofVerdict {
  const ap = a.pos as Pt;
  const tp = t.pos as Pt;
  const dist = Math.hypot(tp[0] - ap[0], tp[1] - ap[1]);
  // Front arc (facing_deg is a world +y-up angle; arc_deg is the HALF-angle).
  const bearing = Math.atan2(tp[1] - ap[1], tp[0] - ap[0]);
  const facing = (a.facing_deg * Math.PI) / 180;
  let delta = Math.abs(bearing - facing) % (2 * Math.PI);
  if (delta > Math.PI) delta = 2 * Math.PI - delta;
  if (delta > (a.arc_deg * Math.PI) / 180 + 1e-9) {
    return { clear: false, reason: "not in your front arc — re-face" };
  }
  if (a.range > 0 && dist > a.range + 1e-9) {
    return { clear: false, reason: `beyond range (${dist.toFixed(1)}″ of ${a.range}″)` };
  }
  const bothElev = a.elevation === 1 && t.elevation === 1;
  for (const piece of terrain) {
    const poly = piece.polygon as Pt[];
    if (!segmentCrossesPoly(ap, tp, poly)) continue;
    if (piece.kind === "blocking" && piece.water === null) {
      return { clear: false, reason: "blocked by blocking terrain", terrainId: piece.id };
    }
    if (piece.elevated) {
      if (bothElev) continue; // both up high — see over the feature
      const standsOn =
        (a.elevation === 1 && pointInPoly(ap, poly)) || (t.elevation === 1 && pointInPoly(tp, poly));
      if (standsOn) continue; // your own hill never blocks your shot
      return { clear: false, reason: "blocked by elevated terrain", terrainId: piece.id };
    }
  }
  for (const o of figures) {
    if (o.eliminated || o.uid === a.uid || o.uid === t.uid) continue;
    const op = o.pos as Pt;
    if (bothElev && o.elevation === 0) continue; // shot passes over ground bases
    if (segDist(ap, tp, op, op) <= o.base_radius + 1e-6) {
      return { clear: false, reason: `blocked by ${o.short_name}'s base`, figUid: o.uid };
    }
  }
  for (const o of figures) {
    if (o.eliminated || o.owner !== a.owner || o.uid === a.uid) continue;
    const op = o.pos as Pt;
    const gap = Math.hypot(tp[0] - op[0], tp[1] - op[1]) - (t.base_radius + o.base_radius);
    if (gap <= 1e-6) {
      return { clear: false, reason: `${o.short_name} screens the target (P4-R25)`, figUid: o.uid };
    }
  }
  return { clear: true, reason: `clear · ${dist.toFixed(1)}″` };
}

// Drawn-terrain size caps — mirrors clixengine/terrain.py MAX_POLYGON_* so the
// drawing UI can show live legality before the server confirms.
export const MAX_POLYGON_AREA = 24; // in²
export const MIN_POLYGON_AREA = 0.5; // in²
export const MAX_POLYGON_EXTENT = 8; // longest vertex-to-vertex span, inches

// Unsigned shoelace area, in square inches.
export function polygonArea(poly: Pt[]): number {
  const n = poly.length;
  if (n < 3) return 0;
  let s = 0;
  for (let i = 0; i < n; i++) {
    const [ax, ay] = poly[i];
    const [bx, by] = poly[(i + 1) % n];
    s += ax * by - bx * ay;
  }
  return Math.abs(s) / 2;
}

// Longest vertex-to-vertex span (the shape's "diameter"), in inches.
export function polygonExtent(poly: Pt[]): number {
  let best = 0;
  for (let i = 0; i < poly.length; i++) {
    for (let j = i + 1; j < poly.length; j++) {
      best = Math.max(best, Math.hypot(poly[i][0] - poly[j][0], poly[i][1] - poly[j][1]));
    }
  }
  return best;
}

// Size verdict for a drawn shape, or null if within the caps.
export function sizeReason(poly: Pt[]): string | null {
  if (poly.length < 3) return null;
  const area = polygonArea(poly);
  const span = polygonExtent(poly);
  if (area > MAX_POLYGON_AREA) return `too big — ${area.toFixed(0)} in² (max ${MAX_POLYGON_AREA} in²)`;
  if (span > MAX_POLYGON_EXTENT) return `too long — ${span.toFixed(1)}" across (max ${MAX_POLYGON_EXTENT}")`;
  if (area < MIN_POLYGON_AREA) return "too thin to be a real terrain piece";
  return null;
}

// Why a candidate polygon may NOT be placed, or null if legal. Mirrors the engine
// (edge margin 1", starting bands 3" deep at both ends, >=2" from other pieces).
export function placementReason(
  poly: Pt[],
  existing: TerrainPiece[],
  boardW: number,
  boardH: number,
  edgeMargin = 1.0,
  startBand = 3.0,
  minGap = 2.0,
): string | null {
  for (const [x, y] of poly) {
    if (x < edgeMargin || x > boardW - edgeMargin) return "off the board";
    if (y < startBand || y > boardH - startBand) return "in a starting area";
  }
  for (const t of existing) {
    if (polyDist(poly, t.polygon as Pt[]) < minGap - 1e-9) return "too close to another piece";
  }
  return null;
}
