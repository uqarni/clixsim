import { useEffect, useRef, useState } from "react";
import type { FigureView, GameView } from "../api";

interface Props {
  view: GameView;
  selectedUid: number | null;
  onSelect: (uid: number | null) => void;
}

// World (inches) -> screen (css px) transform, letterboxed to preserve aspect.
interface Transform {
  scale: number; // css px per inch
  offX: number;
  offY: number;
}

const FELT_MARGIN = 24; // css px of felt border around the play area

const COLORS = {
  human: "#4a9de0",
  humanSoft: "rgba(74, 157, 224, 0.22)",
  llm: "#e07a4a",
  llmSoft: "rgba(224, 122, 74, 0.22)",
  felt: "#1b3b2f",
  feltEdge: "#122a22",
  feltLine: "rgba(120, 180, 150, 0.10)",
  select: "#7c9cff",
  text: "#e7ebf2",
  textDim: "#c3ccd8",
  good: "#5bd68a",
  bad: "#e05a5a",
  warn: "#e0c04a",
};

function computeTransform(
  cssW: number,
  cssH: number,
  boardW: number,
  boardH: number,
): Transform {
  const availW = cssW - FELT_MARGIN * 2;
  const availH = cssH - FELT_MARGIN * 2;
  const scale = Math.max(1, Math.min(availW / boardW, availH / boardH));
  const drawnW = boardW * scale;
  const drawnH = boardH * scale;
  const offX = (cssW - drawnW) / 2;
  const offY = (cssH - drawnH) / 2;
  return { scale, offX, offY };
}

function worldToScreen(t: Transform, x: number, y: number): [number, number] {
  return [t.offX + x * t.scale, t.offY + y * t.scale];
}

function screenToWorld(t: Transform, sx: number, sy: number): [number, number] {
  return [(sx - t.offX) / t.scale, (sy - t.offY) / t.scale];
}

function toRad(deg: number): number {
  return (deg * Math.PI) / 180;
}

function drawFigure(
  ctx: CanvasRenderingContext2D,
  t: Transform,
  f: FigureView,
  selected: boolean,
  hovered: boolean,
) {
  const [cx, cy] = worldToScreen(t, f.pos[0], f.pos[1]);
  const r = Math.max(6, f.base_radius * t.scale);
  const hue = f.owner === "human" ? COLORS.human : COLORS.llm;
  const soft = f.owner === "human" ? COLORS.humanSoft : COLORS.llmSoft;

  // Front-arc wedge (facing +/- arc_deg), radius a bit beyond the base.
  const wedgeR = r * 2.4;
  const start = toRad(f.facing_deg - f.arc_deg);
  const end = toRad(f.facing_deg + f.arc_deg);
  ctx.beginPath();
  ctx.moveTo(cx, cy);
  ctx.arc(cx, cy, wedgeR, start, end, false);
  ctx.closePath();
  ctx.fillStyle = soft;
  ctx.fill();

  // Base circle.
  ctx.beginPath();
  ctx.arc(cx, cy, r, 0, Math.PI * 2);
  ctx.fillStyle = hue;
  ctx.fill();
  ctx.lineWidth = 1.5;
  ctx.strokeStyle = "rgba(0,0,0,0.4)";
  ctx.stroke();

  // Facing tick (a short line from center toward facing).
  const fx = cx + Math.cos(toRad(f.facing_deg)) * r;
  const fy = cy + Math.sin(toRad(f.facing_deg)) * r;
  ctx.beginPath();
  ctx.moveTo(cx, cy);
  ctx.lineTo(fx, fy);
  ctx.strokeStyle = "rgba(255,255,255,0.85)";
  ctx.lineWidth = 1.5;
  ctx.stroke();

  // Health ring around the base.
  const ringR = r + 3;
  ctx.beginPath();
  ctx.arc(cx, cy, ringR, 0, Math.PI * 2);
  ctx.strokeStyle = "rgba(0,0,0,0.35)";
  ctx.lineWidth = 3;
  ctx.stroke();

  const frac = Math.max(0, Math.min(1, f.health_fraction));
  const healthColor = frac > 0.5 ? COLORS.good : frac > 0.25 ? COLORS.warn : COLORS.bad;
  ctx.beginPath();
  ctx.arc(cx, cy, ringR, -Math.PI / 2, -Math.PI / 2 + Math.PI * 2 * frac);
  ctx.strokeStyle = healthColor;
  ctx.lineWidth = 3;
  ctx.stroke();

  // Push-token pips (small dots along the top edge of the base).
  if (f.action_tokens > 0) {
    const n = f.action_tokens;
    const spread = Math.min(0.9, 0.28 * n);
    for (let i = 0; i < n; i++) {
      const a = -Math.PI / 2 + (i - (n - 1) / 2) * spread;
      const px = cx + Math.cos(a) * (r + 8);
      const py = cy + Math.sin(a) * (r + 8);
      ctx.beginPath();
      ctx.arc(px, py, 2.5, 0, Math.PI * 2);
      ctx.fillStyle = COLORS.warn;
      ctx.fill();
    }
  }

  // Selection / hover highlight ring.
  if (selected || hovered) {
    ctx.beginPath();
    ctx.arc(cx, cy, ringR + 4, 0, Math.PI * 2);
    ctx.strokeStyle = selected ? COLORS.select : "rgba(255,255,255,0.5)";
    ctx.lineWidth = selected ? 2 : 1.5;
    ctx.stroke();
  }

  // Label under the base — only for the selected/hovered figure, so touching
  // deployment clusters don't stack unreadable names (the rails list them all).
  if (selected || hovered) {
    const label = f.short_name;
    ctx.font = "500 12px system-ui, sans-serif";
    ctx.textAlign = "center";
    ctx.textBaseline = "top";
    const tw = ctx.measureText(label).width;
    const ly = cy + ringR + 6;
    ctx.fillStyle = "rgba(10,16,14,0.72)";
    ctx.fillRect(cx - tw / 2 - 4, ly - 2, tw + 8, 16);
    ctx.fillStyle = COLORS.text;
    ctx.fillText(label, cx, ly);
  }
}

function drawSelectionRings(
  ctx: CanvasRenderingContext2D,
  t: Transform,
  f: FigureView,
) {
  const [cx, cy] = worldToScreen(t, f.pos[0], f.pos[1]);
  // Dashed range ring if range > 0, else a reach ring at speed.
  const useRange = f.range > 0;
  const radiusIn = useRange ? f.range : f.speed;
  if (radiusIn <= 0) return;
  const rr = radiusIn * t.scale;
  ctx.save();
  ctx.setLineDash([6, 5]);
  ctx.beginPath();
  ctx.arc(cx, cy, rr, 0, Math.PI * 2);
  ctx.strokeStyle = useRange ? "rgba(124,156,255,0.65)" : "rgba(91,214,138,0.55)";
  ctx.lineWidth = 1.5;
  ctx.stroke();
  ctx.restore();
}

export default function BoardCanvas({ view, selectedUid, onSelect }: Props) {
  const canvasRef = useRef<HTMLCanvasElement | null>(null);
  const wrapRef = useRef<HTMLDivElement | null>(null);
  const [size, setSize] = useState<{ w: number; h: number }>({ w: 0, h: 0 });
  const [hoverUid, setHoverUid] = useState<number | null>(null);
  const transformRef = useRef<Transform | null>(null);

  // Observe container size for responsive, aspect-preserving fit.
  useEffect(() => {
    const wrap = wrapRef.current;
    if (!wrap) return;
    const ro = new ResizeObserver((entries) => {
      const cr = entries[0].contentRect;
      setSize({ w: Math.floor(cr.width), h: Math.floor(cr.height) });
    });
    ro.observe(wrap);
    return () => ro.disconnect();
  }, []);

  // Draw.
  useEffect(() => {
    const canvas = canvasRef.current;
    if (!canvas || size.w === 0 || size.h === 0) return;
    const ctx = canvas.getContext("2d");
    if (!ctx) return;

    const dpr = window.devicePixelRatio || 1;
    canvas.width = Math.round(size.w * dpr);
    canvas.height = Math.round(size.h * dpr);
    canvas.style.width = `${size.w}px`;
    canvas.style.height = `${size.h}px`;
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);

    const { width: bw, height: bh } = view.meta.board;
    const t = computeTransform(size.w, size.h, bw, bh);
    transformRef.current = t;

    // Background.
    ctx.fillStyle = COLORS.feltEdge;
    ctx.fillRect(0, 0, size.w, size.h);

    // Felt play area.
    const [px0, py0] = worldToScreen(t, 0, 0);
    const pw = bw * t.scale;
    const ph = bh * t.scale;
    ctx.fillStyle = COLORS.felt;
    ctx.fillRect(px0, py0, pw, ph);

    // Subtle felt guide lines every 6 inches.
    ctx.strokeStyle = COLORS.feltLine;
    ctx.lineWidth = 1;
    for (let gx = 6; gx < bw; gx += 6) {
      const [sx] = worldToScreen(t, gx, 0);
      ctx.beginPath();
      ctx.moveTo(sx, py0);
      ctx.lineTo(sx, py0 + ph);
      ctx.stroke();
    }
    for (let gy = 6; gy < bh; gy += 6) {
      const [, sy] = worldToScreen(t, 0, gy);
      ctx.beginPath();
      ctx.moveTo(px0, sy);
      ctx.lineTo(px0 + pw, sy);
      ctx.stroke();
    }

    // Play-area border.
    ctx.strokeStyle = "rgba(0,0,0,0.5)";
    ctx.lineWidth = 2;
    ctx.strokeRect(px0, py0, pw, ph);

    const live = view.figures.filter((f) => !f.eliminated);
    const selected = live.find((f) => f.uid === selectedUid) ?? null;

    // Selection rings (drawn beneath figures).
    if (selected) drawSelectionRings(ctx, t, selected);

    // Line of fire: friendly selected + hovering an enemy.
    if (selected && hoverUid != null && hoverUid !== selected.uid) {
      const hovered = live.find((f) => f.uid === hoverUid);
      if (hovered && hovered.owner !== selected.owner) {
        const [ax, ay] = worldToScreen(t, selected.pos[0], selected.pos[1]);
        const [bx, by] = worldToScreen(t, hovered.pos[0], hovered.pos[1]);
        ctx.save();
        ctx.setLineDash([2, 4]);
        ctx.beginPath();
        ctx.moveTo(ax, ay);
        ctx.lineTo(bx, by);
        ctx.strokeStyle = "rgba(224,90,90,0.9)";
        ctx.lineWidth = 1.5;
        ctx.stroke();
        ctx.restore();
      }
    }

    for (const f of live) {
      drawFigure(ctx, t, f, f.uid === selectedUid, f.uid === hoverUid);
    }
  }, [view, size, selectedUid, hoverUid]);

  function hitTest(clientX: number, clientY: number): number | null {
    const canvas = canvasRef.current;
    const t = transformRef.current;
    if (!canvas || !t) return null;
    const rect = canvas.getBoundingClientRect();
    const sx = clientX - rect.left;
    const sy = clientY - rect.top;
    const [wx, wy] = screenToWorld(t, sx, sy);
    // Closest figure whose base contains the point.
    let best: { uid: number; d: number } | null = null;
    for (const f of view.figures) {
      if (f.eliminated) continue;
      const dx = wx - f.pos[0];
      const dy = wy - f.pos[1];
      const d = Math.hypot(dx, dy);
      if (d <= f.base_radius && (best === null || d < best.d)) {
        best = { uid: f.uid, d };
      }
    }
    return best?.uid ?? null;
  }

  return (
    <div className="board-wrap" ref={wrapRef}>
      <canvas
        ref={canvasRef}
        onClick={(e) => onSelect(hitTest(e.clientX, e.clientY))}
        onMouseMove={(e) => setHoverUid(hitTest(e.clientX, e.clientY))}
        onMouseLeave={() => setHoverUid(null)}
      />
    </div>
  );
}
