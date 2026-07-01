import { useCallback, useEffect, useMemo, useState } from "react";
import {
  applyIntent,
  endTurn,
  getCandidates,
  getState,
  opponentTurn,
  validateMove,
  type ApplyResult,
  type Candidate,
  type FigureView,
  type GameEvent,
  type GameView,
} from "./api";
import ActionPanel from "./components/ActionPanel";
import BoardCanvas from "./components/BoardCanvas";
import DialInspector from "./components/DialInspector";
import ForceRail from "./components/ForceRail";
import LogLedger from "./components/LogLedger";
import OpponentPanel from "./components/OpponentPanel";
import TurnHud from "./components/TurnHud";

interface MoveGhost {
  dest: [number, number];
  facing: number;
  ok: boolean;
  breakAway: boolean;
}

const MAX_LOG = 200;

function facingToward(from: [number, number], to: [number, number]): number {
  return Math.atan2(to[1] - from[1], to[0] - from[0]);
}

export default function App() {
  const [view, setView] = useState<GameView | null>(null);
  const [selectedUid, setSelectedUid] = useState<number | null>(null);
  const [events, setEvents] = useState<GameEvent[]>([]);
  const [candidates, setCandidates] = useState<Candidate[]>([]);
  const [armed, setArmed] = useState<Candidate | null>(null);
  const [moveGhost, setMoveGhost] = useState<MoveGhost | null>(null);
  const [busy, setBusy] = useState(false);

  const log = useCallback((items: GameEvent[]) => {
    if (items.length === 0) return;
    setEvents((prev) => [...prev, ...items].slice(-MAX_LOG));
  }, []);

  useEffect(() => {
    let cancelled = false;
    getState()
      .then((v) => {
        if (cancelled) return;
        setView(v);
        log([{ type: "info", summary: "Game state loaded." }]);
      })
      .catch((err: unknown) => {
        if (!cancelled) log([{ type: "error", summary: `Failed to load state: ${String(err)}` }]);
      });
    return () => {
      cancelled = true;
    };
  }, [log]);

  const selectedFig = useMemo(
    () => view?.figures.find((f) => f.uid === selectedUid) ?? null,
    [view, selectedUid],
  );

  const isHumanTurn = !!view && view.meta.active_player === "human" && !view.meta.ended;
  const activeUid =
    selectedFig && selectedFig.owner === "human" && selectedFig.can_act && isHumanTurn
      ? selectedFig.uid
      : null;

  // Clear any armed action / ghost when the selection changes.
  useEffect(() => {
    setArmed(null);
    setMoveGhost(null);
  }, [selectedUid]);

  // Refetch the selected figure's legal candidates whenever it can act.
  useEffect(() => {
    if (!view || selectedUid == null) {
      setCandidates([]);
      return;
    }
    const fig = view.figures.find((f) => f.uid === selectedUid);
    const canAct =
      !!fig && fig.owner === "human" && fig.can_act && view.meta.active_player === "human" && !view.meta.ended;
    if (!canAct) {
      setCandidates([]);
      return;
    }
    let cancelled = false;
    getCandidates(selectedUid)
      .then((cs) => !cancelled && setCandidates(cs))
      .catch(() => !cancelled && setCandidates([]));
    return () => {
      cancelled = true;
    };
  }, [view, selectedUid]);

  const armedTargets = useMemo<number[]>(() => {
    if (!armed) return [];
    const a = armed.annotation;
    if (Array.isArray(a.targets)) return a.targets.filter((x): x is number => typeof x === "number");
    if (typeof a.target === "number") return [a.target];
    if (typeof a.toward === "number") return [a.toward];
    return [];
  }, [armed]);

  const handleApply = useCallback(
    (res: ApplyResult) => {
      const out: GameEvent[] = res.events.slice();
      if (res.summary) out.push({ type: "summary", summary: res.summary });
      if (!res.ok) out.push({ type: "rejected", summary: `Rejected: ${res.reason ?? "illegal"}${res.detail ? ` — ${res.detail}` : ""}` });
      log(out);
      if (res.ok) {
        setView(res.view);
        setArmed(null);
        setMoveGhost(null);
      }
    },
    [log],
  );

  const confirmArmed = useCallback(async () => {
    if (!armed || busy) return;
    setBusy(true);
    try {
      handleApply(await applyIntent(armed.intent));
    } catch (err) {
      log([{ type: "error", summary: `Action failed: ${String(err)}` }]);
    } finally {
      setBusy(false);
    }
  }, [armed, busy, handleApply, log]);

  // --- free-placement move (board drag) ---
  const nearestEnemy = useCallback(
    (from: [number, number], owner: string): FigureView | null => {
      if (!view) return null;
      let best: FigureView | null = null;
      let bd = Infinity;
      for (const f of view.figures) {
        if (f.eliminated || f.owner === owner) continue;
        const d = Math.hypot(f.pos[0] - from[0], f.pos[1] - from[1]);
        if (d < bd) {
          bd = d;
          best = f;
        }
      }
      return best;
    },
    [view],
  );

  const ghostFor = useCallback(
    (fig: FigureView, dest: [number, number]): MoveGhost => {
      const dist = Math.hypot(dest[0] - fig.pos[0], dest[1] - fig.pos[1]);
      const moving = dist > 1e-6;
      const enemy = nearestEnemy(dest, fig.owner);
      const facing = enemy ? facingToward(dest, [enemy.pos[0], enemy.pos[1]]) : (fig.facing_deg * Math.PI) / 180;
      const inEnemyContact =
        !!view &&
        fig.in_base_contact_with.some((uid) => {
          const o = view.figures.find((f) => f.uid === uid);
          return o && o.owner !== fig.owner;
        });
      return { dest, facing, ok: dist <= fig.speed + 1e-6, breakAway: moving && inEnemyContact };
    },
    [nearestEnemy, view],
  );

  const onMoveDrag = useCallback(
    (dest: [number, number]) => {
      const fig = view?.figures.find((f) => f.uid === activeUid);
      if (!fig) return;
      setMoveGhost(ghostFor(fig, dest));
    },
    [view, activeUid, ghostFor],
  );

  const onMoveDrop = useCallback(
    async (dest: [number, number]) => {
      const fig = view?.figures.find((f) => f.uid === activeUid);
      if (!fig || busy) {
        setMoveGhost(null);
        return;
      }
      const g = ghostFor(fig, dest);
      setMoveGhost(null);
      if (!g.ok) {
        log([{ type: "rejected", summary: `Too far — beyond ${fig.speed}" speed.` }]);
        return;
      }
      setBusy(true);
      try {
        const check = await validateMove(fig.uid, dest, g.facing);
        if (!check.ok) {
          log([{ type: "rejected", summary: `Rejected: ${check.reason ?? "illegal move"}${check.detail ? ` — ${check.detail}` : ""}` }]);
          return;
        }
        handleApply(
          await applyIntent({ kind: "move", figure_uid: fig.uid, dest, facing: g.facing, free: false }),
        );
      } catch (err) {
        log([{ type: "error", summary: `Move failed: ${String(err)}` }]);
      } finally {
        setBusy(false);
      }
    },
    [view, activeUid, busy, ghostFor, handleApply, log],
  );

  const handleEndTurn = useCallback(async () => {
    if (busy || !view || view.meta.ended) return;
    setBusy(true);
    setArmed(null);
    setMoveGhost(null);
    setSelectedUid(null);
    try {
      let v = await endTurn();
      setView(v);
      log([{ type: "turn", summary: "Turn ended. Opponent is thinking…" }]);
      let guard = 0;
      while (v.meta.active_player !== "human" && !v.meta.ended && guard++ < 8) {
        const r = await opponentTurn();
        v = r.view;
        setView(v);
        const lines: GameEvent[] = r.decisions
          .map((d) => (d && typeof d === "object" && "summary" in d ? String((d as { summary: unknown }).summary) : String(d)))
          .map((s) => ({ type: "opponent", summary: s }));
        log(lines.length ? lines : [{ type: "opponent", summary: "Opponent passed." }]);
      }
      if (!v.meta.ended) log([{ type: "turn", summary: "Your turn." }]);
      else log([{ type: "turn", summary: `Game over — winner: ${v.meta.winner ?? "draw"}.` }]);
    } catch (err) {
      log([{ type: "error", summary: `End turn failed: ${String(err)}` }]);
    } finally {
      setBusy(false);
    }
  }, [busy, view, log]);

  if (!view) {
    return (
      <div className="app">
        <div className="hud">
          <span className="hud-title">Clix Engine</span>
          <div className="hud-group">Loading…</div>
        </div>
        <div className="zones" />
      </div>
    );
  }

  return (
    <div className="app">
      <TurnHud view={view} onEndTurn={handleEndTurn} />
      <div className="zones">
        <ForceRail figures={view.figures} selectedUid={selectedUid} onSelect={setSelectedUid} />
        <DialInspector fig={selectedFig} />
        <div className="zone board-zone">
          <div className="zone-head">
            <span>Board</span>
            <span className="fig-sub">
              {view.meta.board.width} × {view.meta.board.height} in
            </span>
          </div>
          <div className="zone-body no-pad board-host">
            <BoardCanvas
              view={view}
              selectedUid={selectedUid}
              onSelect={setSelectedUid}
              activeUid={activeUid}
              armedTargets={armedTargets}
              moveGhost={moveGhost}
              onMoveDrag={onMoveDrag}
              onMoveDrop={onMoveDrop}
              onMoveCancel={() => setMoveGhost(null)}
            />
            <ActionPanel
              view={view}
              selectedFig={selectedFig}
              candidates={candidates}
              armed={armed}
              busy={busy}
              onArm={setArmed}
              onConfirm={confirmArmed}
              onCancel={() => setArmed(null)}
            />
          </div>
        </div>
        <OpponentPanel figures={view.figures} selectedUid={selectedUid} onSelect={setSelectedUid} />
        <LogLedger view={view} events={events} />
      </div>
    </div>
  );
}
