import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  getTerrainTypes,
  placeTerrainPolygon,
  skipTerrain,
  terrainPlacementStreamUrl,
  type GameView,
  type TerrainStreamEvent,
  type TerrainType,
} from "../api";
import BoardCanvas, { type DrawGhost } from "./BoardCanvas";
import {
  MAX_POLYGON_AREA,
  MAX_POLYGON_EXTENT,
  placementReason,
  polygonArea,
  polygonExtent,
  polygonSimple,
  sizeReason,
  type Pt,
} from "../terrainGeom";

interface Props {
  initialView: GameView;
  onDone: (v: GameView) => void; // hand off once terrain setup ends (to deploy or battle)
  onCancel: () => void;
}

const KIND_TAG: Record<string, string> = { blocking: "Blocks", hindering: "Hinders", clear: "Passable" };
const swatchClass = (t: TerrainType) =>
  `terrain-swatch k-${t.kind}${t.elevated ? " elev" : ""}${t.water ? " water" : ""}`;

export default function TerrainPlacement({ initialView, onDone, onCancel }: Props) {
  const [view, setView] = useState<GameView>(initialView);
  const [types, setTypes] = useState<TerrainType[]>([]);
  const [selKey, setSelKey] = useState<string | null>(null);
  const [poly, setPoly] = useState<Pt[]>([]);
  const [cursor, setCursor] = useState<Pt | null>(null);
  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState("");
  const [thoughts, setThoughts] = useState<{ summary: string; reasoning: string; used: boolean }[]>([]);
  const streamRef = useRef<EventSource | null>(null);

  const boardW = view.meta.board.width;
  const boardH = view.meta.board.height;
  const phase = view.meta.phase;
  const myTurn = phase === "terrain" && view.meta.terrain_turn === "human";
  const myBudget = view.meta.terrain_budget.human ?? 0;
  const llmBudget = view.meta.terrain_budget.llm ?? 0;

  useEffect(() => {
    getTerrainTypes()
      .then((ts) => {
        setTypes(ts);
        setSelKey((k) => k ?? (ts[0]?.key ?? null));
      })
      .catch(() => setMsg("Could not load terrain types."));
  }, []);

  const selected = useMemo(() => types.find((t) => t.key === selKey) ?? null, [types, selKey]);

  const reason = useMemo(() => {
    if (poly.length < 3) return "Click at least 3 points on the board to outline a shape.";
    if (!polygonSimple(poly)) return "The outline crosses itself — draw a simple shape.";
    const size = sizeReason(poly);
    if (size) return `That shape is ${size}.`;
    return placementReason(poly, view.terrain, boardW, boardH);
  }, [poly, view.terrain, boardW, boardH]);
  const ok = myTurn && reason === null;

  const ghost = useMemo<DrawGhost | null>(() => {
    if (!selected || !myTurn) return null;
    return {
      poly,
      cursor,
      ok,
      kind: selected.kind,
      elevated: selected.elevated,
      water: selected.water,
      lowWall: selected.low_wall,
    };
  }, [selected, myTurn, poly, cursor, ok]);

  // Stream the opponent placing its run of the alternation.
  const runLLM = useCallback(() => {
    if (streamRef.current) return;
    setBusy(true);
    const es = new EventSource(terrainPlacementStreamUrl());
    streamRef.current = es;
    const finishStream = () => {
      es.close();
      streamRef.current = null;
      setBusy(false);
    };
    es.onmessage = (m) => {
      const e = JSON.parse(m.data) as TerrainStreamEvent;
      if (e.type === "place") {
        setThoughts((ts) => [...ts, { summary: e.summary, reasoning: e.reasoning, used: e.used_llm }]);
        setView(e.view);
      } else if (e.type === "done") {
        setView(e.view);
        finishStream();
        if (e.view.meta.phase !== "terrain") onDone(e.view);
      } else if (e.type === "error") {
        if (e.view) setView(e.view);
        finishStream();
      }
    };
    es.onerror = () => finishStream();
  }, [onDone]);

  // If the opponent places first (won initiative), kick the stream once on mount.
  useEffect(() => {
    if (initialView.meta.phase === "terrain" && initialView.meta.terrain_turn === "llm") runLLM();
    return () => {
      if (streamRef.current) {
        streamRef.current.close();
        streamRef.current = null;
      }
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const finish = useCallback(async () => {
    if (!selected || !myTurn || busy) return;
    if (reason !== null) {  // covers vertex count, simplicity, size caps, placement
      setMsg(reason);
      return;
    }
    setBusy(true);
    try {
      const res = await placeTerrainPolygon(selected.key, poly);
      if (!res.ok) {
        setMsg(res.detail || res.reason || "Illegal placement.");
        setBusy(false);
        return;
      }
      setMsg("");
      setPoly([]);
      setCursor(null);
      setView(res.view);
      setBusy(false);
      if (res.view.meta.phase !== "terrain") onDone(res.view);
      else if (res.view.meta.terrain_turn === "llm") runLLM();
    } catch (err) {
      setMsg(String(err));
      setBusy(false);
    }
  }, [selected, myTurn, busy, poly, view.terrain, boardW, boardH, reason, onDone, runLLM]);

  const nearFirst = (w: Pt) => poly.length >= 3 && Math.hypot(w[0] - poly[0][0], w[1] - poly[0][1]) < 1.0;
  const onDrawPoint = useCallback(
    (w: Pt) => {
      if (nearFirst(w)) {
        finish();
        return;
      }
      setPoly((p) => [...p, w]);
    },
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [poly, finish],
  );
  const onDrawUndo = useCallback(() => setPoly((p) => p.slice(0, -1)), []);

  const finishPlacing = useCallback(async () => {
    if (!myTurn || busy) return;
    setBusy(true);
    try {
      const res = await skipTerrain();
      setView(res.view);
      setBusy(false);
      if (res.view.meta.phase !== "terrain") onDone(res.view);
      else if (res.view.meta.terrain_turn === "llm") runLLM();
    } catch (err) {
      setMsg(String(err));
      setBusy(false);
    }
  }, [myTurn, busy, onDone, runLLM]);

  const myFigs = view.figures.filter((f) => f.owner === "human" && !f.eliminated);

  return (
    <div className="app">
      <header className="hud">
        <div className="hud-left">
          <strong>Place terrain</strong>
          <span className="fig-sub">Pick a type, then click points on the board to draw the shape.</span>
        </div>
        <div className="hud-right">
          <span className="pill">You: {myBudget} left</span>
          <span className="pill">Opponent: {llmBudget} left</span>
          <button className="btn" type="button" onClick={onCancel}>
            Quit
          </button>
        </div>
      </header>

      <div className="zones placement-zones">
        {/* Terrain type + draw controls */}
        <div className="zone" style={{ minWidth: 250, maxWidth: 290 }}>
          <div className="zone-head">
            <span>Terrain type</span>
          </div>
          <div className="zone-body">
            <div className="terrain-palette">
              {types.map((t) => (
                <button
                  key={t.key}
                  type="button"
                  className={`terrain-chip${t.key === selKey ? " sel" : ""}`}
                  onClick={() => setSelKey(t.key)}
                  disabled={!myTurn}
                  title={t.blurb}
                >
                  <span className={swatchClass(t)} />
                  <span className="terrain-chip-body">
                    <span className="terrain-chip-name">
                      {t.label} <span className="terrain-kind">{KIND_TAG[t.kind] ?? t.kind}</span>
                    </span>
                    <span className="terrain-chip-blurb">{t.blurb}</span>
                  </span>
                </button>
              ))}
            </div>

            <div className="terrain-controls">
              <button className="btn primary" type="button" onClick={finish} disabled={!ok || busy}>
                Place shape
              </button>
              <button className="btn" type="button" onClick={onDrawUndo} disabled={!myTurn || poly.length === 0}>
                Undo point
              </button>
              <button className="btn" type="button" onClick={() => setPoly([])} disabled={!myTurn || poly.length === 0}>
                Clear
              </button>
            </div>
            <p className="fig-sub" style={{ marginTop: 8 }}>
              Left-click to drop points ({poly.length} placed); click the first point (or “Place shape”) to finish.
              Right-click undoes the last point. Stay clear of the shaded deploy bands and ≥2″ from other pieces.
            </p>
            {myTurn && poly.length >= 3 && (
              <p className={`fig-sub terrain-size${sizeReason(poly) ? " over" : ""}`}>
                Size: {polygonArea(poly).toFixed(1)} in² of {MAX_POLYGON_AREA} ·{" "}
                {polygonExtent(poly).toFixed(1)}″ of {MAX_POLYGON_EXTENT}″ across
              </p>
            )}
            {msg && <p className="terrain-msg bad">{msg}</p>}
            {myTurn && poly.length > 0 && reason && <p className="terrain-msg warn">{reason}</p>}
          </div>
        </div>

        {/* Board */}
        <div className="zone board-zone">
          <div className="zone-head">
            <span>{myTurn ? "Draw your terrain" : phase !== "terrain" ? "Ready" : "Opponent is placing…"}</span>
            <span className="fig-sub">
              {boardW} × {boardH} in
            </span>
          </div>
          <div className="zone-body no-pad board-host">
            <BoardCanvas
              view={view}
              selectedUid={null}
              onSelect={() => {}}
              activeUid={null}
              armedTargets={[]}
              armedMembers={[]}
              moveGhost={null}
              onMoveDrag={() => {}}
              onMoveDrop={() => {}}
              onMoveCancel={() => {}}
              pendingMove={null}
              onFaceDrag={() => {}}
              fx={[]}
              fxSeq={0}
              placementMode={myTurn}
              draw={ghost}
              onDrawPoint={onDrawPoint}
              onDrawMove={(w) => setCursor(w)}
              onDrawUndo={onDrawUndo}
            />
          </div>
        </div>

        {/* Your army reference */}
        <div className="zone" style={{ minWidth: 230, maxWidth: 270 }}>
          <div className="zone-head">
            <span>Your army</span>
            <span className="fig-sub">who benefits</span>
          </div>
          <div className="zone-body">
            <div className="tp-army">
              {myFigs.map((f) => (
                <div className="tp-fig" key={f.uid}>
                  <div className="tp-fig-top">
                    <span className="tp-fig-name">{f.short_name}</span>
                    <span className={`tp-fig-role ${f.is_ranged ? "ranged" : "melee"}`}>
                      {f.is_ranged ? `➶ R${f.range}` : "⚔"}
                    </span>
                  </div>
                  <div className="tp-fig-stats">
                    S{f.speed} A{f.attack} D{f.defense} Dm{f.damage}
                  </div>
                  {f.active_abilities.length > 0 && (
                    <div className="tp-fig-abil">{f.active_abilities.map((a) => a.name).join(" · ")}</div>
                  )}
                </div>
              ))}
            </div>
            <button
              className="btn primary"
              type="button"
              onClick={finishPlacing}
              disabled={!myTurn || busy}
              style={{ width: "100%", marginTop: 8 }}
            >
              {myBudget > 0 ? `Done placing (skip ${myBudget})` : "Done placing"}
            </button>
          </div>
        </div>

        {/* Opponent reasoning */}
        <div className="zone" style={{ minWidth: 230, maxWidth: 280 }}>
          <div className="zone-head">
            <span>Opponent</span>
            {busy && !myTurn && <span className="fig-sub">thinking…</span>}
          </div>
          <div className="zone-body">
            {thoughts.length === 0 && <p className="fig-sub">The opponent's terrain choices will appear here.</p>}
            <div className="opp-thoughts">
              {thoughts.map((t, i) => (
                <div key={i} className="thought">
                  <div className="thought-move">
                    {!t.used && <span className="badge">auto</span>} {t.summary}
                  </div>
                  {t.reasoning && <div className="thought-reason">“{t.reasoning}”</div>}
                </div>
              ))}
            </div>
          </div>
        </div>
      </div>
    </div>
  );
}
