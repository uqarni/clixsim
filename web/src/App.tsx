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
import BoardCanvas, { type Fx, type SpinGhost } from "./components/BoardCanvas";
import Construction from "./components/Construction";
import Deploy from "./components/Deploy";
import DialInspector from "./components/DialInspector";
import Draft from "./components/Draft";
import ForceRail from "./components/ForceRail";
import LogLedger from "./components/LogLedger";
import NewGame, { type GameConfig } from "./components/NewGame";
import OpponentPanel from "./components/OpponentPanel";
import TerrainPlacement from "./components/TerrainPlacement";
import TurnHud from "./components/TurnHud";
import { effectiveSpeed, moveBlockReason, snapToContactRing } from "./terrainGeom";

interface MoveGhost {
  dest: [number, number];
  facing: number;
  ok: boolean;
  breakAway: boolean;
  reason?: string; // why the drop is illegal (terrain/speed), shown at the ghost
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
        // Name the self-damage sources: a bare "-1" next to two touching figures
        // (e.g. a healer and its patient) reads as damage to the wrong one.
        const label =
          t === "crit_miss_self" ? `backfire -${clk}` : t === "push_damage" ? `push -${clk}` : `-${clk}`;
        out.push({ kind: "float", x: p[0], y: p[1], text: label, color: RED, dur: 900 });
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
  const [phase, setPhase] = useState<
    "menu" | "drafting" | "constructing" | "placing_terrain" | "deploying" | "battle"
  >("menu");
  const [config, setConfig] = useState<GameConfig | null>(null);
  const [humanIds, setHumanIds] = useState<number[]>([]);

  const [view, setView] = useState<GameView | null>(null);
  const [selectedUid, setSelectedUid] = useState<number | null>(null);
  // Multi-selection (marquee / shift+click), in selection order — the group the
  // user wants to move together; legality is judged live, never persisted.
  const [selection, setSelection] = useState<number[]>([]);
  const [events, setEvents] = useState<GameEvent[]>([]);
  const [candidates, setCandidates] = useState<Candidate[]>([]);
  const [hints, setHints] = useState<string[]>([]);
  const [formations, setFormations] = useState<Candidate[]>([]);
  const [armed, setArmed] = useState<Candidate | null>(null);
  const [explain, setExplain] = useState<AttackExplain | null>(null);
  const [moveGhost, setMoveGhost] = useState<MoveGhost | null>(null);
  const [pendingMove, setPendingMove] = useState<PendingMove | null>(null);
  const [fx, setFx] = useState<Fx[]>([]);
  const [fxSeq, setFxSeq] = useState(0);
  const [busy, setBusy] = useState(false);
  const [oppThoughts, setOppThoughts] = useState<{ summary: string; reasoning: string; fallback: boolean }[]>([]);
  const [freeSpin, setFreeSpin] = useState<{ spinners: number[]; idx: number; by: number | null; facing: number } | null>(null);
  // Interactive formation move (P4-R14): members are placed ONE AT A TIME with the
  // normal drag/facing UX; the whole arrangement submits as a single MoveIntent.
  const [formationStage, setFormationStage] = useState<{
    uids: number[];
    placed: { uid: number; dest: [number, number]; facing: number }[];
    speed: number; // slowest member's hindering-halved speed (P4-R13)
  } | null>(null);
  const viewRef = useRef<GameView | null>(null);
  const oppStreamRef = useRef<EventSource | null>(null);
  // Set after onReady is defined; lets early effects trigger a server resync.
  const resyncRef = useRef<() => void>(() => {});

  const log = useCallback((items: GameEvent[]) => {
    if (items.length === 0) return;
    setEvents((prev) => [...prev, ...items].slice(-MAX_LOG));
  }, []);

  const selectedFig = useMemo(
    () => view?.figures.find((f) => f.uid === selectedUid) ?? null,
    [view, selectedUid],
  );

  const isHumanTurn = !!view && view.meta.active_player === "human" && !view.meta.ended;
  // While a formation move is being staged, the member being placed is the one
  // that drags — regardless of what's selected for inspection.
  const stagingUid = formationStage ? (formationStage.uids[formationStage.placed.length] ?? null) : null;
  const activeUid = formationStage
    ? stagingUid
    : selectedFig && selectedFig.owner === "human" && selectedFig.can_act && isHumanTurn
      ? selectedFig.uid
      : null;
  const canToggle = !!selectedFig && selectedFig.owner === "human" && isHumanTurn;

  useEffect(() => {
    setArmed(null);
    setMoveGhost(null);
    // Selecting another figure mid-staging must not wipe the member being aimed.
    setPendingMove((pm) => (formationStage ? pm : null));
    // eslint-disable-next-line react-hooks/exhaustive-deps
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
      } else if (e.type === "free_spin") {
        // Opponent moved into your figures — pause and let you re-face for free (P4-R9).
        setView(e.view);
        viewRef.current = e.view;
        finish(); // close this stream; we re-open it to resume once spinning is done
        const uid = e.spinners[0];
        const fig = e.view.figures.find((f) => f.uid === uid);
        const mover = e.by != null ? e.view.figures.find((f) => f.uid === e.by) : null;
        if (fig) {
          const facing = mover ? facingToward(fig.pos, mover.pos) : (fig.facing_deg * Math.PI) / 180;
          setSelectedUid(uid);
          setFreeSpin({ spinners: e.spinners, idx: 0, by: e.by, facing });
          log([{ type: "info", summary: "Free spin — an enemy reached your line; re-face for free." }]);
        }
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

  // Free spin (P4-R9): re-face the current contacted figure (or skip), then advance
  // to the next; when all are done, resume the opponent's paused turn.
  const spinStep = useCallback(
    async (apply: boolean) => {
      if (!freeSpin) return;
      const uid = freeSpin.spinners[freeSpin.idx];
      let v = viewRef.current;
      if (apply) {
        try {
          const res = await applyIntent({ kind: "free_spin", figure_uid: uid, facing: freeSpin.facing });
          if (res.ok) {
            v = res.view;
            viewRef.current = res.view;
            setView(res.view);
          }
        } catch (err) {
          log([{ type: "error", summary: `Spin failed: ${String(err)}` }]);
        }
      }
      const nextIdx = freeSpin.idx + 1;
      if (v && nextIdx < freeSpin.spinners.length) {
        const nu = freeSpin.spinners[nextIdx];
        const fig = v.figures.find((f) => f.uid === nu);
        const mover = freeSpin.by != null ? v.figures.find((f) => f.uid === freeSpin.by) : null;
        const facing = fig ? (mover ? facingToward(fig.pos, mover.pos) : (fig.facing_deg * Math.PI) / 180) : 0;
        setSelectedUid(nu);
        setFreeSpin({ ...freeSpin, idx: nextIdx, facing });
      } else {
        setFreeSpin(null);
        setSelectedUid(null);
        runOpponentStream(); // resume the opponent's turn
      }
    },
    [freeSpin, runOpponentStream, log],
  );

  const onSpinFace = useCallback((facing: number) => setFreeSpin((fs) => (fs ? { ...fs, facing } : fs)), []);

  const spinGhost = useMemo<SpinGhost | null>(() => {
    if (!freeSpin || !view) return null;
    const uid = freeSpin.spinners[freeSpin.idx];
    const fig = view.figures.find((f) => f.uid === uid);
    return fig ? { uid, pos: fig.pos, facing: freeSpin.facing } : null;
  }, [freeSpin, view]);

  const spinFig = useMemo(
    () => (freeSpin && view ? view.figures.find((f) => f.uid === freeSpin.spinners[freeSpin.idx]) ?? null : null),
    [freeSpin, view],
  );

  // Selected figure's legal candidates.
  useEffect(() => {
    if (!view || selectedUid == null) {
      setCandidates([]);
      setHints([]);
      return;
    }
    const fig = view.figures.find((f) => f.uid === selectedUid);
    const canAct =
      !!fig && fig.owner === "human" && fig.can_act && view.meta.active_player === "human" && !view.meta.ended;
    if (!canAct) {
      setCandidates([]);
      setHints([]);
      return;
    }
    let cancelled = false;
    getCandidates(selectedUid)
      .then((res) => {
        if (cancelled) return;
        setCandidates(res.candidates);
        setHints(res.hints);
      })
      .catch(() => {
        if (cancelled) return;
        setCandidates([]);
        setHints([]);
        // A failing candidates fetch for a figure we can see usually means the
        // SERVER holds a different game (it restarted) — detect and resync.
        resyncRef.current();
      });
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
    if (formationStage) return formationStage.uids;
    const m = armed?.annotation.members;
    if (Array.isArray(m)) return m.filter((x): x is number => typeof x === "number");
    return selection.length >= 2 ? selection : []; // group-selection rings
  }, [armed, formationStage, selection]);

  const handleApply = useCallback(
    (res: ApplyResult) => {
      // free_spin_offer is an internal signal (the AI auto-re-faces server-side); not user log.
      const out: GameEvent[] = res.events.filter((e) => e.type !== "free_spin_offer");
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
    (fig: FigureView, dest: [number, number], faceUid?: number): MoveGhost => {
      const dist = Math.hypot(dest[0] - fig.pos[0], dest[1] - fig.pos[1]);
      // Face the figure we snapped onto when it's an ENEMY (that's a charge);
      // otherwise face the nearest enemy from the destination.
      const snapTarget =
        faceUid != null
          ? view?.figures.find((f) => f.uid === faceUid && f.owner !== fig.owner && !f.eliminated)
          : undefined;
      const enemy = snapTarget ?? nearestEnemy(dest, fig.owner);
      const facing = enemy ? facingToward(dest, [enemy.pos[0], enemy.pos[1]]) : (fig.facing_deg * Math.PI) / 180;
      const inEnemyContact =
        !!view &&
        fig.in_base_contact_with.some((uid) => {
          const o = view.figures.find((f) => f.uid === uid);
          return o && o.owner !== fig.owner;
        });
      // Live terrain truth for the ghost (the engine re-validates on confirm):
      // hindering-halved speed, blocking endpoints/paths, the entry-stop rule.
      const terrain = view?.terrain ?? [];
      const flies = fig.active_abilities.some((a) => a.name === "Flight" || a.name === "Aquatic");
      const eff = formationStage
        ? formationStage.speed // whole formation paces to the slowest member (P4-R13)
        : flies
          ? fig.speed
          : effectiveSpeed(fig.speed, fig.pos, fig.base_radius, terrain);
      let reason: string | undefined;
      if (dist > eff + 1e-6) {
        reason = eff < fig.speed ? `too far — formation/hindering speed ${eff}″` : `too far — speed ${eff}″`;
      } else {
        reason = moveBlockReason(fig.pos, dest, fig.base_radius, terrain, flies && !formationStage) ?? undefined;
      }
      // Nobody may end overlapping another base (mirror of end_on_base).
      if (!reason && view) {
        for (const o of view.figures) {
          if (o.eliminated || o.uid === fig.uid) continue;
          if (formationStage?.placed.some((p) => p.uid === o.uid)) continue; // staged elsewhere
          if (Math.hypot(dest[0] - o.pos[0], dest[1] - o.pos[1]) < fig.base_radius + o.base_radius - 0.02) {
            reason = `overlaps ${o.short_name}'s base`;
            break;
          }
        }
      }
      // Formation cohesion, live: from the 2nd member on, each placement must end
      // in base contact with an already-placed member (P4-R14) and not on top of one.
      if (!reason && formationStage && formationStage.placed.length > 0) {
        const touching = formationStage.placed.some((p) => {
          const pf = view?.figures.find((f) => f.uid === p.uid);
          if (!pf) return false;
          const d = Math.hypot(dest[0] - p.dest[0], dest[1] - p.dest[1]);
          return d <= fig.base_radius + pf.base_radius + 0.02;
        });
        const overlapping = formationStage.placed.some((p) => {
          const pf = view?.figures.find((f) => f.uid === p.uid);
          return pf && Math.hypot(dest[0] - p.dest[0], dest[1] - p.dest[1]) < fig.base_radius + pf.base_radius - 0.02;
        });
        if (overlapping) reason = "overlaps a placed member";
        else if (!touching) reason = "must end touching the formation";
      }
      return { dest, facing, ok: !reason, breakAway: dist > 1e-6 && inEnemyContact, reason };
    },
    [nearestEnemy, view, formationStage],
  );

  // Snap a dragged/dropped point onto EXACT base contact with the nearest base —
  // any figure, friend or foe (the engine's contact epsilon is 1e-6, so eyeballing
  // it never counts as touching). While staging a formation move, already-placed
  // members snap at their STAGED destinations, not their old spots.
  const snapToBase = useCallback(
    (fig: FigureView, dest: [number, number]): { point: [number, number]; uid: number }[] => {
      if (!view) return [];
      const stagedByUid = new Map((formationStage?.placed ?? []).map((p) => [p.uid, p.dest]));
      const targets = view.figures
        .filter((nf) => !nf.eliminated && nf.uid !== fig.uid)
        .map((nf) => ({ pos: stagedByUid.get(nf.uid) ?? nf.pos, radius: nf.base_radius, uid: nf.uid }));
      const isEnemy = (uid: number) =>
        view.figures.some((f) => f.uid === uid && f.owner !== fig.owner && !f.eliminated);
      // Ranked candidates (pockets touching two bases first when the cursor says
      // so); for a pocket, report the ENEMY contact as primary so the charge
      // auto-aim (ghostFor's faceUid) targets it, not a friend.
      return snapToContactRing(fig.base_radius, dest, targets).map((c) => ({
        point: c.point,
        uid: c.uid2 != null && !isEnemy(c.uid) && isEnemy(c.uid2) ? c.uid2 : c.uid,
      }));
    },
    [view, formationStage],
  );

  const onMoveDrag = useCallback(
    (dest: [number, number]) => {
      const fig = view?.figures.find((f) => f.uid === activeUid);
      if (!fig) return;
      // Snap DURING the drag so the ghost visibly sticks to nearby bases; when it
      // snaps onto an ENEMY, the ghost faces that enemy (a charge should never
      // end aimed the wrong way by accident). Candidates are ranked (two-contact
      // pocket first when intended) — take the first one that's actually legal.
      const snapped = snapToBase(fig, dest).find((c) => ghostFor(fig, c.point, c.uid).ok);
      if (snapped) {
        setMoveGhost(ghostFor(fig, snapped.point, snapped.uid));
      } else {
        setMoveGhost(ghostFor(fig, dest));
      }
    },
    [view, activeUid, ghostFor, snapToBase],
  );

  const onMoveDrop = useCallback(
    (dest: [number, number]) => {
      const fig = view?.figures.find((f) => f.uid === activeUid);
      setMoveGhost(null);
      if (!fig) return;
      // Prefer the best LEGAL snapped-to-contact destination; otherwise the raw drop.
      const snapped = snapToBase(fig, dest).find((c) => ghostFor(fig, c.point, c.uid).ok);
      const g = snapped ? ghostFor(fig, snapped.point, snapped.uid) : ghostFor(fig, dest);
      if (!g.ok) {
        log([{ type: "rejected", summary: `Can't move there — ${g.reason ?? "illegal move"}.` }]);
        return;
      }
      setPendingMove({ dest: g.dest, facing: g.facing });
    },
    [view, activeUid, ghostFor, snapToBase, log],
  );

  const confirmMove = useCallback(async () => {
    const fig = view?.figures.find((f) => f.uid === activeUid);
    if (!fig || !pendingMove || busy) return;
    if (formationStage) {
      // Stage this member locally; the engine validates the whole formation at submit.
      const { dest, facing } = pendingMove;
      setPendingMove(null);
      setMoveGhost(null);
      setFormationStage((fs) =>
        fs ? { ...fs, placed: [...fs.placed, { uid: fig.uid, dest, facing }] } : fs,
      );
      return;
    }
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
  }, [view, activeUid, pendingMove, busy, formationStage, handleApply, log]);

  // --- interactive formation move (P4-R14: members placed one at a time) ----
  const startFormationStaging = useCallback(
    (members: number[]) => {
      if (!view || members.length < 3) return;
      // Re-validate against the CURRENT view — a stale entry point could stage
      // an already-acted member and dead-end at submit.
      const figs = members.map((u) => view.figures.find((x) => x.uid === u));
      const bad = figs.find((f) => !f || f.eliminated || !f.can_act);
      if (bad !== undefined || view.meta.active_player !== "human") {
        log([{ type: "rejected", summary: "That formation is no longer available — a member has already acted." }]);
        return;
      }
      const speeds = figs.map((f) => effectiveSpeed(f!.speed, f!.pos, f!.base_radius, view.terrain));
      setArmed(null);
      setMoveGhost(null);
      setPendingMove(null);
      setSelection([]);
      setFormationStage({ uids: members, placed: [], speed: Math.max(1, Math.min(...speeds)) });
      setSelectedUid(members[0]);
    },
    [view, log],
  );

  const startFormationMove = useCallback(
    (c: Candidate) => {
      const members = (c.annotation.members as number[] | undefined) ?? [];
      startFormationStaging(members);
    },
    [startFormationStaging],
  );

  // --- StarCraft-style group selection --------------------------------------
  const handleSelect = useCallback((uid: number | null, additive = false) => {
    if (uid == null) {
      setSelectedUid(null);
      setSelection([]);
      return;
    }
    const fig = viewRef.current?.figures.find((f) => f.uid === uid);
    setSelectedUid(uid);
    const groupable = !!fig && fig.owner === "human" && !fig.eliminated;
    if (additive && groupable) {
      setSelection((sel) => (sel.includes(uid) ? sel.filter((u) => u !== uid) : [...sel, uid]));
    } else {
      setSelection(groupable ? [uid] : []);
    }
  }, []);

  const handleMarquee = useCallback((a: [number, number], b: [number, number]) => {
    const v = viewRef.current;
    if (!v) return;
    const [x0, x1] = [Math.min(a[0], b[0]), Math.max(a[0], b[0])];
    const [y0, y1] = [Math.min(a[1], b[1]), Math.max(a[1], b[1])];
    const picked = v.figures
      .filter(
        (f) =>
          !f.eliminated &&
          f.owner === "human" &&
          f.pos[0] >= x0 && f.pos[0] <= x1 && f.pos[1] >= y0 && f.pos[1] <= y1,
      )
      .sort((p, q) => p.pos[0] - q.pos[0] || p.pos[1] - q.pos[1])
      .map((f) => f.uid);
    setSelection(picked);
    setSelectedUid(picked[0] ?? null);
  }, []);

  // Live legality of the selected group as a movement formation — the button is
  // greyed out with THIS reason when the group can't form up (contact truth comes
  // from the engine-computed in_base_contact_with, not eyeballed pixels).
  const groupInfo = useMemo(() => {
    if (!view || selection.length < 2 || formationStage) return null;
    const figs = selection
      .map((u) => view.figures.find((f) => f.uid === u))
      .filter((f): f is FigureView => !!f && !f.eliminated);
    if (figs.length < 2) return null;
    const names = figs.map((f) => f.short_name);
    const barredOf = (f: FigureView) =>
      f.active_abilities.find((x) => ["Flight", "Aquatic", "Quickness"].includes(x.name));
    let reason: string | null = null;
    if (figs.length < 3 || figs.length > 5) {
      reason = `movement formations are 3–5 figures (${figs.length} selected)`;
    } else if (new Set(figs.map((f) => f.faction)).size > 1) {
      reason = "mixed factions — members must share a faction";
    } else if (figs[0].faction === "Mage Spawn") {
      reason = "Mage Spawn cannot form formations";
    } else {
      const acted = figs.find((f) => !f.can_act);
      const demoral = figs.find((f) => f.demoralized);
      const engaged = figs.find((f) =>
        f.in_base_contact_with.some((u) => view.figures.find((o) => o.uid === u)?.owner !== "human"),
      );
      const barred = figs.find((f) => barredOf(f));
      if (acted) reason = `${acted.short_name} has already acted this turn`;
      else if (demoral) reason = `${demoral.short_name} is demoralized`;
      else if (engaged) reason = `${engaged.short_name} is in enemy contact — it must move individually`;
      else if (barred) {
        reason = `${barred.short_name}'s ${barredOf(barred)!.name} bars movement formations — cancel the optional ability to join`;
      } else {
        const ids = new Set(selection);
        const adj = new Map(figs.map((f) => [f.uid, f.in_base_contact_with.filter((u) => ids.has(u))]));
        const loner = figs.find((f) => (adj.get(f.uid) ?? []).length === 0);
        if (loner) {
          const gap = Math.min(
            ...figs
              .filter((o) => o.uid !== loner.uid)
              .map(
                (o) =>
                  Math.hypot(loner.pos[0] - o.pos[0], loner.pos[1] - o.pos[1]) -
                  (loner.base_radius + o.base_radius),
              ),
          );
          reason = `${loner.short_name} isn't touching the group — it's ${Math.max(0, gap).toFixed(2)}″ short. Drag it next to a member (it snaps).`;
        }
        else {
          const seen = new Set<number>([figs[0].uid]);
          const stack = [figs[0].uid];
          while (stack.length) {
            for (const n of adj.get(stack.pop()!) ?? []) {
              if (!seen.has(n)) {
                seen.add(n);
                stack.push(n);
              }
            }
          }
          if (seen.size !== figs.length) reason = "the group is split — it must be one touching cluster";
        }
      }
    }
    return { uids: figs.map((f) => f.uid), names, ok: reason === null, reason };
  }, [view, selection, formationStage]);

  // Defer the member being placed to the end of the queue, so arrangements that
  // aren't reachable in the default order (e.g. A—C—B chains) can be staged.
  const formationDefer = useCallback(() => {
    setPendingMove(null);
    setMoveGhost(null);
    setFormationStage((fs) => {
      if (!fs) return fs;
      const i = fs.placed.length;
      if (i >= fs.uids.length - 1) return fs; // already the last unplaced member
      const uids = [...fs.uids];
      const [cur] = uids.splice(i, 1);
      uids.push(cur);
      return { ...fs, uids };
    });
  }, []);

  const cancelFormation = useCallback(() => {
    setFormationStage(null);
    setMoveGhost(null);
    setPendingMove(null);
  }, []);

  const formationBack = useCallback(() => {
    if (pendingMove) {
      setPendingMove(null);
      return;
    }
    setFormationStage((fs) =>
      fs && fs.placed.length > 0 ? { ...fs, placed: fs.placed.slice(0, -1) } : fs,
    );
  }, [pendingMove]);

  const formationLeaveInPlace = useCallback(() => {
    if (!view || !formationStage) return;
    const uid = formationStage.uids[formationStage.placed.length];
    const f = view.figures.find((x) => x.uid === uid);
    if (!f) return;
    const g = ghostFor(f, f.pos); // staying put must still satisfy cohesion
    if (!g.ok) {
      log([{ type: "rejected", summary: `Can't leave ${f.short_name} here — ${g.reason ?? "breaks the formation"}.` }]);
      return;
    }
    setPendingMove(null);
    setFormationStage((fs) =>
      fs
        ? { ...fs, placed: [...fs.placed, { uid, dest: f.pos, facing: (f.facing_deg * Math.PI) / 180 }] }
        : fs,
    );
  }, [view, formationStage, ghostFor, log]);

  const submitFormation = useCallback(async () => {
    if (!formationStage || formationStage.placed.length !== formationStage.uids.length || busy) return;
    setBusy(true);
    try {
      const placed = formationStage.placed;
      // uids/dests/facings all derive from the PLACED order, so deferring
      // members mid-staging can never desynchronize the arrays.
      const res = await applyIntent({
        kind: "move",
        figure_uid: placed[0].uid,
        dest: placed[0].dest,
        facing: placed[0].facing,
        free: false,
        formation_uids: placed.map((p) => p.uid),
        member_dests: placed.map((p) => p.dest),
        member_facings: placed.map((p) => p.facing),
      });
      handleApply(res);
      if (res.ok) {
        setFormationStage(null);
      } else {
        // Keep the staging only when repositioning can cure the rejection.
        const incurable = ["already_acted", "no_actions", "pushed_out", "bad_formation", "game_over"];
        if (incurable.includes(res.reason ?? "")) setFormationStage(null);
      }
    } catch (err) {
      log([{ type: "error", summary: `Formation move failed: ${String(err)}` }]);
    } finally {
      setBusy(false);
    }
  }, [formationStage, busy, handleApply, log]);

  const handleEndTurn = useCallback(async () => {
    if (busy || !view || view.meta.ended || view.meta.active_player !== "human") return;
    setArmed(null);
    setMoveGhost(null);
    setPendingMove(null);
    setFormationStage(null);
    setSelectedUid(null);
    setSelection([]);
    setFreeSpin(null);
    setOppThoughts([]);
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

  const enterBattle = useCallback((v: GameView) => {
    if (oppStreamRef.current) {
      oppStreamRef.current.close();
      oppStreamRef.current = null;
    }
    setSelectedUid(null);
    setSelection([]);
    setArmed(null);
    setMoveGhost(null);
    setPendingMove(null);
    setFormationStage(null);
    setFreeSpin(null);
    setOppThoughts([]);
    setEvents([{ type: "info", summary: "Battle begins." }]);
    viewRef.current = v;
    setView(v);
    setPhase("battle");
    if (v.meta.active_player !== "human" && !v.meta.ended) runOpponentStream();
  }, [runOpponentStream]);

  // A freshly-built game may open in a setup phase (terrain, then deploy) before battle.
  const onReady = useCallback((v: GameView) => {
    if (v.meta.phase === "terrain") {
      viewRef.current = v;
      setView(v);
      setPhase("placing_terrain");
      return;
    }
    if (v.meta.phase === "deploy") {
      viewRef.current = v;
      setView(v);
      setPhase("deploying");
      return;
    }
    enterBattle(v);
  }, [enterBattle]);

  const handleNewGame = useCallback(() => {
    setView(null);
    setPhase("menu");
  }, []);

  // Client/server desync detection: if the server's game_id differs from the one
  // this tab is rendering (a restart replaced the in-memory game), announce it
  // and adopt the server's game instead of showing dead controls.
  const resyncingRef = useRef(false);
  const resyncFromServer = useCallback(async () => {
    if (resyncingRef.current) return;
    resyncingRef.current = true;
    try {
      const server = await getState();
      if (viewRef.current && server.meta.game_id !== viewRef.current.meta.game_id) {
        log([{ type: "info", summary: "The server restarted with a different game — synced to it. Start a new game from the menu if this isn't yours." }]);
        onReady(server);
      } else {
        setView(server); // same game — just refresh the stale view
      }
    } catch {
      /* server unreachable — keep the local view */
    } finally {
      setTimeout(() => {
        resyncingRef.current = false;
      }, 1500);
    }
  }, [log, onReady]);
  useEffect(() => {
    resyncRef.current = resyncFromServer;
  }, [resyncFromServer]);

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
  if (phase === "placing_terrain" && view)
    return <TerrainPlacement initialView={view} onDone={onReady} onCancel={handleNewGame} />;
  if (phase === "deploying" && view)
    return <Deploy initialView={view} onDone={onReady} onCancel={handleNewGame} />;
  if (!view) return <NewGame onStart={startDraft} onResume={onResume} />;

  const gameOver = view.meta.ended;
  const outcome = view.meta.winner === "human" ? "Victory" : view.meta.winner === "llm" ? "Defeat" : "Draw";

  return (
    <div className="app">
      <TurnHud view={view} onEndTurn={handleEndTurn} onNewGame={handleNewGame} />
      <div className="zones">
        <ForceRail figures={view.figures} selectedUid={selectedUid} onSelect={handleSelect} />
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
              onSelect={handleSelect}
              onMarquee={handleMarquee}
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
              spin={spinGhost}
              onSpinFace={onSpinFace}
              staged={
                formationStage
                  ? formationStage.placed.map((p) => ({
                      dest: p.dest,
                      facing: p.facing,
                      radius: view.figures.find((f) => f.uid === p.uid)?.base_radius ?? 0.55,
                    }))
                  : null
              }
              dimUids={formationStage ? formationStage.placed.map((p) => p.uid) : []}
              reachOverride={formationStage ? formationStage.speed : null}
            />
            {freeSpin && spinFig && (
              <div className="spin-banner">
                <div className="spin-banner-title">Free spin (P4-R9)</div>
                <div className="spin-banner-body">
                  <strong>{spinFig.short_name}</strong> was contacted — drag the amber handle to face the
                  threat, then confirm. Costs no action.
                  {freeSpin.spinners.length > 1 && (
                    <span className="fig-sub"> {freeSpin.idx + 1}/{freeSpin.spinners.length}</span>
                  )}
                </div>
                <div className="spin-banner-btns">
                  <button className="btn primary" type="button" onClick={() => spinStep(true)}>
                    Confirm spin
                  </button>
                  <button className="btn" type="button" onClick={() => spinStep(false)}>
                    Skip
                  </button>
                </div>
              </div>
            )}
            <ActionPanel
              view={view}
              selectedFig={selectedFig}
              candidates={candidates}
              hints={hints}
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
              formation={
                formationStage
                  ? {
                      total: formationStage.uids.length,
                      placedCount: formationStage.placed.length,
                      currentName:
                        view.figures.find((f) => f.uid === stagingUid)?.short_name ?? null,
                      speed: formationStage.speed,
                      canDefer: formationStage.placed.length < formationStage.uids.length - 1,
                    }
                  : null
              }
              group={groupInfo}
              onGroupMove={() => groupInfo?.ok && startFormationStaging(groupInfo.uids)}
              onGroupClear={() => {
                setSelection([]);
                setSelectedUid(null);
              }}
              onFormationStart={startFormationMove}
              onFormationBack={formationBack}
              onFormationLeave={formationLeaveInPlace}
              onFormationDefer={formationDefer}
              onFormationCancel={cancelFormation}
              onFormationSubmit={submitFormation}
            />
          </div>
        </div>
        <OpponentPanel figures={view.figures} selectedUid={selectedUid} onSelect={handleSelect} thoughts={oppThoughts} />
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
