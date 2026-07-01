import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  applyIntent,
  endTurn,
  explainAttack,
  getCandidates,
  getFormationCandidates,
  getState,
  opponentTurnStreamUrl,
  toggleAbility,
  validateMove,
  type ApplyResult,
  type OpponentStreamEvent,
  type AttackExplain,
  type Candidate,
  type FigureView,
  type GameEvent,
  type GameView,
} from "./api";
import ActionPanel from "./components/ActionPanel";
import BoardCanvas, { type Fx } from "./components/BoardCanvas";
import Construction from "./components/Construction";
import DialInspector from "./components/DialInspector";
import Draft from "./components/Draft";
import ForceRail from "./components/ForceRail";
import LogLedger from "./components/LogLedger";
import NewGame, { type GameConfig } from "./components/NewGame";
import OpponentPanel from "./components/OpponentPanel";
import TurnHud from "./components/TurnHud";

interface MoveGhost {
  dest: [number, number];
  facing: number;
  ok: boolean;
  breakAway: boolean;
}
interface PendingMove {
  dest: [number, number];
  facing: number;
}

const MAX_LOG = 200;
const CLOSE_KINDS = ["close", "weapon_master"];
const RANGED_KINDS = ["ranged", "magic_blast", "flame_lightning", "shockwave"];

function facingToward(from: [number, number], to: [number, number]): number {
  return Math.atan2(to[1] - from[1], to[0] - from[0]);
}
function annTarget(c: Candidate): number | null {
  const a = c.annotation;
  if (typeof a.target === "number") return a.target;
  if (Array.isArray(a.targets) && typeof a.targets[0] === "number") return a.targets[0];
  if (typeof a.toward === "number") return a.toward;
  return null;
}
function intentField(c: Candidate, key: string): number | null {
  const v = (c.intent as Record<string, unknown> | null)?.[key];
  return typeof v === "number" ? v : null;
}

// Turn a resolution's events into transient board effects (anchored using the
// PRE-update positions, since figures may move/die when the new view applies).
const RED = "#e05a5a";
const GREEN = "#5bd68a";
function deriveFx(events: GameEvent[], view: GameView): Fx[] {
  const pos = (uid: unknown): [number, number] | null => {
    const f = view.figures.find((x) => x.uid === uid);
    return f ? f.pos : null;
  };
  const num = (v: unknown) => (typeof v === "number" ? v : 0);
  const out: Fx[] = [];
  for (const e of events) {
    const t = e.type;
    if (t === "break_away") {
      const p = pos(e.figure);
      if (p) out.push({ kind: "dice", x: p[0], y: p[1], dice: [num(e.roll)], result: e.success ? "hit" : "miss", dur: 900 });
    } else if (["ranged_attack", "close_attack", "magic_blast", "flame_lightning", "shockwave"].includes(t)) {
      const a = pos(e.attacker);
      const tp = pos(e.target);
      if (a && Array.isArray(e.dice)) out.push({ kind: "dice", x: a[0], y: a[1], dice: e.dice as number[], result: e.result as string, dur: 1000 });
      const clk = num(e.clicks);
      if (tp && clk > 0) {
        out.push({ kind: "float", x: tp[0], y: tp[1], text: `-${clk}`, color: RED, dur: 950 });
        out.push({ kind: "flash", x: tp[0], y: tp[1], color: RED, dur: 450 });
      }
    } else if (["healing", "magic_healing"].includes(t)) {
      const hp = pos(e.target);
      if (hp && num(e.healed) > 0) out.push({ kind: "float", x: hp[0], y: hp[1], text: `+${num(e.healed)}`, color: GREEN, dur: 950 });
    } else if (t === "regenerate" || t === "vampirism") {
      const p = pos(e.figure);
      if (p && num(e.healed) > 0) out.push({ kind: "float", x: p[0], y: p[1], text: `+${num(e.healed)}`, color: GREEN, dur: 950 });
    } else if (t === "pole_arm" || t === "crit_miss_self" || t === "push_damage") {
      const u = t === "pole_arm" ? e.target : e.figure;
      const p = pos(u);
      const clk = num(e.clicks);
      if (p && clk > 0) {
        out.push({ kind: "float", x: p[0], y: p[1], text: `-${clk}`, color: RED, dur: 900 });
        out.push({ kind: "flash", x: p[0], y: p[1], color: RED, dur: 400 });
      }
    } else if (t === "eliminated") {
      const p = pos(e.figure);
      if (p) out.push({ kind: "ko", x: p[0], y: p[1], dur: 700 });
    }
  }
  return out;
}

export default function App() {
  const [phase, setPhase] = useState<"menu" | "drafting" | "constructing" | "battle">("menu");
  const [config, setConfig] = useState<GameConfig | null>(null);
  const [humanIds, setHumanIds] = useState<number[]>([]);

  const [view, setView] = useState<GameView | null>(null);
  const [selectedUid, setSelectedUid] = useState<number | null>(null);
  const [events, setEvents] = useState<GameEvent[]>([]);
  const [candidates, setCandidates] = useState<Candidate[]>([]);
  const [formations, setFormations] = useState<Candidate[]>([]);
  const [armed, setArmed] = useState<Candidate | null>(null);
  const [explain, setExplain] = useState<AttackExplain | null>(null);
  const [moveGhost, setMoveGhost] = useState<MoveGhost | null>(null);
  const [pendingMove, setPendingMove] = useState<PendingMove | null>(null);
  const [fx, setFx] = useState<Fx[]>([]);
  const [fxSeq, setFxSeq] = useState(0);
  const [busy, setBusy] = useState(false);
  const [oppThoughts, setOppThoughts] = useState<{ summary: string; reasoning: string; fallback: boolean }[]>([]);
  const viewRef = useRef<GameView | null>(null);
  const oppStreamRef = useRef<EventSource | null>(null);

  const log = useCallback((items: GameEvent[]) => {
    if (items.length === 0) return;
    setEvents((prev) => [...prev, ...items].slice(-MAX_LOG));
  }, []);

  const selectedFig = useMemo(
    () => view?.figures.find((f) => f.uid === selectedUid) ?? null,
    [view, selectedUid],
  );

  const isHumanTurn = !!view && view.meta.active_player === "human" && !view.meta.ended;
  const activeUid =
    selectedFig && selectedFig.owner === "human" && selectedFig.can_act && isHumanTurn
      ? selectedFig.uid
      : null;
  const canToggle = !!selectedFig && selectedFig.owner === "human" && isHumanTurn;

  useEffect(() => {
    setArmed(null);
    setMoveGhost(null);
    setPendingMove(null);
  }, [selectedUid]);

  useEffect(() => {
    viewRef.current = view;
  }, [view]);

  // Stream the opponent's turn action-by-action: log + reason + animate each move.
  // Called imperatively (not from a view-dependent effect) so the EventSource
  // survives per-action re-renders and StrictMode's dev double-invoke.
  const runOpponentStream = useCallback(() => {
    if (oppStreamRef.current) return; // already streaming this turn
    setBusy(true);
    setOppThoughts([]);
    let prev = viewRef.current; // pre-action view, for anchoring effects
    const es = new EventSource(opponentTurnStreamUrl());
    oppStreamRef.current = es;
    const finish = () => {
      es.close();
      oppStreamRef.current = null;
      setBusy(false);
    };
    es.onmessage = (m) => {
      const e = JSON.parse(m.data) as OpponentStreamEvent;
      if (e.type === "action") {
        if (prev) {
          const eff = deriveFx(e.events, prev);
          if (eff.length) {
            setFx(eff);
            setFxSeq((n) => n + 1);
          }
        }
        log([{ type: "opponent", summary: (e.fallback ? "[fallback] " : "") + e.summary }]);
        setOppThoughts((ts) => [...ts, { summary: e.summary, reasoning: e.reasoning, fallback: e.fallback }]);
        prev = e.view;
        setView(e.view);
      } else if (e.type === "done") {
        setView(e.view);
        log([{ type: "turn", summary: e.view.meta.ended ? `Game over — winner: ${e.view.meta.winner ?? "draw"}.` : "Your turn." }]);
        finish();
      } else if (e.type === "error") {
        if (e.view) setView(e.view);
        finish();
      }
    };
    es.onerror = () => {
      getState().then(setView).catch(() => {});
      finish();
    };
  }, [log]);

  // Selected figure's legal candidates.
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

  // Turn-level formations.
  useEffect(() => {
    if (!view || view.meta.active_player !== "human" || view.meta.ended) {
      setFormations([]);
      return;
    }
    let cancelled = false;
    getFormationCandidates()
      .then((cs) => !cancelled && setFormations(cs))
      .catch(() => !cancelled && setFormations([]));
    return () => {
      cancelled = true;
    };
  }, [view]);

  // Attack modifier breakdown when an attack is armed.
  useEffect(() => {
    if (!armed || !view) {
      setExplain(null);
      return;
    }
    const isClose = CLOSE_KINDS.includes(armed.kind);
    const isRanged = RANGED_KINDS.includes(armed.kind);
    const t = annTarget(armed);
    const attacker = intentField(armed, "attacker_uid") ?? selectedUid;
    if (t == null || attacker == null || (!isClose && !isRanged)) {
      setExplain(null);
      return;
    }
    let cancelled = false;
    explainAttack(attacker, t, isClose ? "close" : "ranged", armed.annotation.rear === true)
      .then((x) => !cancelled && setExplain(x))
      .catch(() => !cancelled && setExplain(null));
    return () => {
      cancelled = true;
    };
  }, [armed, view, selectedUid]);

  const armedTargets = useMemo<number[]>(() => {
    if (!armed) return [];
    const a = armed.annotation;
    if (typeof a.target === "number") return [a.target];
    if (Array.isArray(a.targets)) return a.targets.filter((x): x is number => typeof x === "number");
    if (typeof a.toward === "number") return [a.toward];
    return [];
  }, [armed]);

  const armedMembers = useMemo<number[]>(() => {
    const m = armed?.annotation.members;
    return Array.isArray(m) ? m.filter((x): x is number => typeof x === "number") : [];
  }, [armed]);

  const handleApply = useCallback(
    (res: ApplyResult) => {
      const out: GameEvent[] = res.events.slice();
      if (res.ok && out.length === 0 && res.summary) out.push({ type: "summary", summary: res.summary });
      if (!res.ok) out.push({ type: "rejected", summary: `Rejected: ${res.reason ?? "illegal"}${res.detail ? ` — ${res.detail}` : ""}` });
      log(out);
      if (res.ok) {
        if (view) {
          const effects = deriveFx(res.events, view); // anchor on pre-update positions
          if (effects.length) {
            setFx(effects);
            setFxSeq((n) => n + 1);
          }
        }
        setView(res.view);
        setArmed(null);
        setMoveGhost(null);
        setPendingMove(null);
      }
    },
    [log, view],
  );

  const runIntent = useCallback(
    async (intent: unknown) => {
      if (busy) return;
      setBusy(true);
      try {
        handleApply(await applyIntent(intent));
      } catch (err) {
        log([{ type: "error", summary: `Action failed: ${String(err)}` }]);
      } finally {
        setBusy(false);
      }
    },
    [busy, handleApply, log],
  );

  const confirmArmed = useCallback(() => {
    if (armed) runIntent(armed.intent);
  }, [armed, runIntent]);

  const onToggle = useCallback(
    async (abilityId: number, off: boolean) => {
      if (!selectedFig || busy) return;
      setBusy(true);
      try {
        handleApply(await toggleAbility(selectedFig.uid, abilityId, off));
      } catch (err) {
        log([{ type: "error", summary: `Toggle failed: ${String(err)}` }]);
      } finally {
        setBusy(false);
      }
    },
    [selectedFig, busy, handleApply, log],
  );

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
      const enemy = nearestEnemy(dest, fig.owner);
      const facing = enemy ? facingToward(dest, [enemy.pos[0], enemy.pos[1]]) : (fig.facing_deg * Math.PI) / 180;
      const inEnemyContact =
        !!view &&
        fig.in_base_contact_with.some((uid) => {
          const o = view.figures.find((f) => f.uid === uid);
          return o && o.owner !== fig.owner;
        });
      return { dest, facing, ok: dist <= fig.speed + 1e-6, breakAway: dist > 1e-6 && inEnemyContact };
    },
    [nearestEnemy, view],
  );

  const onMoveDrag = useCallback(
    (dest: [number, number]) => {
      const fig = view?.figures.find((f) => f.uid === activeUid);
      if (fig) setMoveGhost(ghostFor(fig, dest));
    },
    [view, activeUid, ghostFor],
  );

  const onMoveDrop = useCallback(
    (dest: [number, number]) => {
      const fig = view?.figures.find((f) => f.uid === activeUid);
      setMoveGhost(null);
      if (!fig) return;
      const g = ghostFor(fig, dest);
      if (!g.ok) {
        log([{ type: "rejected", summary: `Too far — beyond ${fig.speed}" speed.` }]);
        return;
      }
      setPendingMove({ dest, facing: g.facing });
    },
    [view, activeUid, ghostFor, log],
  );

  const confirmMove = useCallback(async () => {
    const fig = view?.figures.find((f) => f.uid === activeUid);
    if (!fig || !pendingMove || busy) return;
    setBusy(true);
    try {
      const check = await validateMove(fig.uid, pendingMove.dest, pendingMove.facing);
      if (!check.ok) {
        log([{ type: "rejected", summary: `Rejected: ${check.reason ?? "illegal move"}${check.detail ? ` — ${check.detail}` : ""}` }]);
        setPendingMove(null);
        return;
      }
      handleApply(
        await applyIntent({ kind: "move", figure_uid: fig.uid, dest: pendingMove.dest, facing: pendingMove.facing, free: false }),
      );
    } catch (err) {
      log([{ type: "error", summary: `Move failed: ${String(err)}` }]);
    } finally {
      setBusy(false);
    }
  }, [view, activeUid, pendingMove, busy, handleApply, log]);

  const handleEndTurn = useCallback(async () => {
    if (busy || !view || view.meta.ended || view.meta.active_player !== "human") return;
    setArmed(null);
    setMoveGhost(null);
    setPendingMove(null);
    setSelectedUid(null);
    setBusy(true);
    try {
      const v = await endTurn();
      viewRef.current = v;
      setView(v);
      log([{ type: "turn", summary: "Turn ended. Opponent is thinking…" }]);
      if (v.meta.active_player !== "human" && !v.meta.ended) {
        runOpponentStream(); // keeps busy true until the opponent finishes
      } else {
        setBusy(false);
      }
    } catch (err) {
      log([{ type: "error", summary: `End turn failed: ${String(err)}` }]);
      setBusy(false);
    }
  }, [busy, view, log, runOpponentStream]);

  // --- new-game flow ---
  const startDraft = useCallback((c: GameConfig) => {
    setConfig(c);
    setHumanIds([]);
    setPhase("drafting");
  }, []);
  const onDraftConfirm = useCallback((ids: number[]) => {
    setHumanIds(ids);
    setPhase("constructing");
  }, []);

  const onReady = useCallback((v: GameView) => {
    if (oppStreamRef.current) {
      oppStreamRef.current.close();
      oppStreamRef.current = null;
    }
    setSelectedUid(null);
    setArmed(null);
    setMoveGhost(null);
    setPendingMove(null);
    setOppThoughts([]);
    setEvents([{ type: "info", summary: "Battle begins." }]);
    viewRef.current = v;
    setView(v);
    setPhase("battle");
    if (v.meta.active_player !== "human" && !v.meta.ended) runOpponentStream();
  }, [runOpponentStream]);

  const handleNewGame = useCallback(() => {
    setView(null);
    setPhase("menu");
  }, []);

  const onResume = useCallback(async () => {
    try {
      onReady(await getState());
    } catch {
      /* no active game — stay on the menu */
    }
  }, [onReady]);

  if (phase === "menu") return <NewGame onStart={startDraft} onResume={onResume} />;
  if (phase === "drafting" && config)
    return <Draft config={config} onConfirm={onDraftConfirm} onCancel={() => setPhase("menu")} />;
  if (phase === "constructing" && config)
    return <Construction config={config} humanIds={humanIds} onReady={onReady} onCancel={() => setPhase("menu")} />;
  if (!view) return <NewGame onStart={startDraft} onResume={onResume} />;

  const gameOver = view.meta.ended;
  const outcome = view.meta.winner === "human" ? "Victory" : view.meta.winner === "llm" ? "Defeat" : "Draw";

  return (
    <div className="app">
      <TurnHud view={view} onEndTurn={handleEndTurn} onNewGame={handleNewGame} />
      <div className="zones">
        <ForceRail figures={view.figures} selectedUid={selectedUid} onSelect={setSelectedUid} />
        <DialInspector fig={selectedFig} canToggle={canToggle} onToggle={onToggle} />
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
              armedMembers={armedMembers}
              moveGhost={moveGhost}
              onMoveDrag={onMoveDrag}
              onMoveDrop={onMoveDrop}
              onMoveCancel={() => setMoveGhost(null)}
              pendingMove={pendingMove}
              onFaceDrag={(facing) => setPendingMove((pm) => (pm ? { ...pm, facing } : pm))}
              fx={fx}
              fxSeq={fxSeq}
            />
            <ActionPanel
              view={view}
              selectedFig={selectedFig}
              candidates={candidates}
              formations={formations}
              armed={armed}
              explain={explain}
              pendingMove={pendingMove}
              busy={busy}
              onArm={setArmed}
              onConfirm={confirmArmed}
              onCancel={() => setArmed(null)}
              onConfirmMove={confirmMove}
              onCancelMove={() => setPendingMove(null)}
            />
          </div>
        </div>
        <OpponentPanel figures={view.figures} selectedUid={selectedUid} onSelect={setSelectedUid} thoughts={oppThoughts} />
        <LogLedger view={view} events={events} />
      </div>

      {gameOver && (
        <div className="overlay">
          <div className="overlay-card">
            <h1 className={`overlay-title ${view.meta.winner ?? "draw"}`}>{outcome}</h1>
            <p className="menu-sub">
              Winner: {view.meta.winner ?? "draw"} · VP {view.meta.victory_points.human}–{view.meta.victory_points.llm}
            </p>
            <button className="btn primary" onClick={handleNewGame} type="button">
              New game
            </button>
          </div>
        </div>
      )}
    </div>
  );
}
