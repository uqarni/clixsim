import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  getTerrainLibrary,
  placeTerrain,
  skipTerrain,
  terrainPlacementStreamUrl,
  type GameView,
  type TerrainStreamEvent,
  type TerrainTemplate,
} from "../api";
import BoardCanvas, { type PlacingGhost } from "./BoardCanvas";
import { placedAccessPoints, placedPolygon, placementReason, type Pt } from "../terrainGeom";

interface Props {
  initialView: GameView;
  onBattle: (v: GameView) => void;
  onCancel: () => void;
}

const KIND_TAG: Record<string, string> = {
  blocking: "Blocks",
  hindering: "Hinders",
  clear: "Passable",
};

export default function TerrainPlacement({ initialView, onBattle, onCancel }: Props) {
  const [view, setView] = useState<GameView>(initialView);
  const [library, setLibrary] = useState<TerrainTemplate[]>([]);
  const [selKey, setSelKey] = useState<string | null>(null);
  const boardW = view.meta.board.width;
  const boardH = view.meta.board.height;
  const [center, setCenter] = useState<Pt>([boardW / 2, boardH / 2]);
  const [rotation, setRotation] = useState(0);
  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState("");
  const [thoughts, setThoughts] = useState<{ summary: string; reasoning: string; used: boolean }[]>([]);
  const streamRef = useRef<EventSource | null>(null);

  const phase = view.meta.phase;
  const myTurn = phase === "terrain" && view.meta.terrain_turn === "human";
  const myBudget = view.meta.terrain_budget.human ?? 0;
  const llmBudget = view.meta.terrain_budget.llm ?? 0;

  useEffect(() => {
    getTerrainLibrary()
      .then((l) => {
        setLibrary(l);
        setSelKey((k) => k ?? (l[0]?.key ?? null));
      })
      .catch(() => setMsg("Could not load the terrain library."));
  }, []);

  const selected = useMemo(() => library.find((t) => t.key === selKey) ?? null, [library, selKey]);

  const reason = useMemo(() => {
    if (!selected || !myTurn) return null;
    const poly = placedPolygon(selected, center, rotation);
    return placementReason(poly, view.terrain, boardW, boardH);
  }, [selected, myTurn, center, rotation, view.terrain, boardW, boardH]);

  const ghost = useMemo<PlacingGhost | null>(() => {
    if (!selected || !myTurn) return null;
    return {
      polygon: placedPolygon(selected, center, rotation),
      accessPoints: placedAccessPoints(selected, center, rotation),
      kind: selected.kind,
      elevated: selected.elevated,
      water: selected.water,
      lowWall: selected.low_wall,
      abrupt: selected.abrupt,
      ok: reason === null,
    };
  }, [selected, myTurn, center, rotation, reason]);

  // Stream the opponent placing its run of the alternation.
  const runLLM = useCallback(() => {
    if (streamRef.current) return;
    setBusy(true);
    const es = new EventSource(terrainPlacementStreamUrl());
    streamRef.current = es;
    const finish = () => {
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
        finish();
        if (e.view.meta.phase === "battle") onBattle(e.view);
      } else if (e.type === "error") {
        if (e.view) setView(e.view);
        finish();
      }
    };
    es.onerror = () => finish();
  }, [onBattle]);

  // If the opponent places first (won initiative), kick the stream once on mount.
  // runLLM guards against a double-open; the cleanup nulls streamRef so React's
  // StrictMode mount→cleanup→mount leaves exactly one live stream.
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

  const commit = useCallback(
    async (pt: Pt) => {
      if (!selected || !myTurn || busy) return;
      const poly = placedPolygon(selected, pt, rotation);
      const r = placementReason(poly, view.terrain, boardW, boardH);
      if (r) {
        setMsg(`Can't place there — ${r}.`);
        return;
      }
      setBusy(true);
      try {
        const res = await placeTerrain(selected.key, pt, rotation);
        if (!res.ok) {
          setMsg(res.detail || res.reason || "Illegal placement.");
          setBusy(false);
          return;
        }
        setMsg("");
        setView(res.view);
        setBusy(false);
        if (res.view.meta.phase === "battle") {
          onBattle(res.view);
        } else if (res.view.meta.terrain_turn === "llm") {
          runLLM();
        }
      } catch (err) {
        setMsg(String(err));
        setBusy(false);
      }
    },
    [selected, myTurn, busy, rotation, view.terrain, boardW, boardH, onBattle, runLLM],
  );

  const onPlacePointer = useCallback(
    (world: Pt, doCommit: boolean) => {
      setCenter(world);
      if (doCommit) commit(world);
    },
    [commit],
  );

  const rotate = useCallback((d: number) => setRotation((r) => r + d), []);

  const finishPlacing = useCallback(async () => {
    if (!myTurn || busy) return;
    setBusy(true);
    try {
      const res = await skipTerrain();
      setView(res.view);
      setBusy(false);
      if (res.view.meta.phase === "battle") onBattle(res.view);
      else if (res.view.meta.terrain_turn === "llm") runLLM();
    } catch (err) {
      setMsg(String(err));
      setBusy(false);
    }
  }, [myTurn, busy, onBattle, runLLM]);

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (!myTurn) return;
      if (e.key === "r" || e.key === "R") rotate(Math.PI / 12);
      else if (e.key === "e" || e.key === "E") rotate(-Math.PI / 12);
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [myTurn, rotate]);

  const done = phase === "battle";

  return (
    <div className="app">
      <header className="hud">
        <div className="hud-left">
          <strong>Place terrain</strong>
          <span className="fig-sub">Set the battlefield before deploying — take turns with your opponent.</span>
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
        {/* Palette */}
        <div className="zone" style={{ minWidth: 260, maxWidth: 300 }}>
          <div className="zone-head">
            <span>Terrain library</span>
          </div>
          <div className="zone-body">
            <div className="terrain-palette">
              {library.map((t) => (
                <button
                  key={t.key}
                  type="button"
                  className={`terrain-chip${t.key === selKey ? " sel" : ""}`}
                  onClick={() => setSelKey(t.key)}
                  disabled={!myTurn}
                  title={t.blurb}
                >
                  <span className={`terrain-swatch k-${t.kind}${t.elevated ? " elev" : ""}${t.water ? " water" : ""}`} />
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
              <button className="btn" type="button" onClick={() => rotate(-Math.PI / 12)} disabled={!myTurn}>
                ⟲ Rotate
              </button>
              <button className="btn" type="button" onClick={() => rotate(Math.PI / 12)} disabled={!myTurn}>
                Rotate ⟳
              </button>
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
            <p className="fig-sub" style={{ marginTop: 8 }}>
              Move the cursor over the board to aim, scroll or press <kbd>R</kbd>/<kbd>E</kbd> to rotate, then click to
              place. Pieces must stay clear of both shaded deploy zones and ≥2″ apart.
            </p>
            {msg && <p className="terrain-msg bad">{msg}</p>}
            {myTurn && selected && reason && (
              <p className="terrain-msg warn">Here: {reason}.</p>
            )}
          </div>
        </div>

        {/* Board */}
        <div className="zone board-zone">
          <div className="zone-head">
            <span>{myTurn ? "Your placement" : done ? "Ready" : "Opponent is placing…"}</span>
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
              placingGhost={ghost}
              onPlacePointer={onPlacePointer}
              onPlaceRotate={rotate}
            />
          </div>
        </div>

        {/* Opponent reasoning */}
        <div className="zone" style={{ minWidth: 260, maxWidth: 320 }}>
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
