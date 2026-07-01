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
