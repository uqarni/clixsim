import type { Candidate, FigureView, GameView } from "../api";

interface Props {
  view: GameView;
  selectedFig: FigureView | null;
  candidates: Candidate[];
  armed: Candidate | null;
  busy: boolean;
  onArm: (c: Candidate) => void;
  onConfirm: () => void;
  onCancel: () => void;
}

type Group = "Move" | "Attack" | "Support" | "Turn";

function groupOf(kind: string): Group {
  if (kind === "pass") return "Turn";
  if (kind === "move" || kind === "formation_move") return "Move";
  if (["heal", "regenerate", "necromancy", "levitate"].includes(kind)) return "Support";
  return "Attack";
}

const GROUP_ORDER: Group[] = ["Attack", "Support", "Move", "Turn"];

function num(ann: Record<string, unknown>, key: string): number | null {
  const v = ann[key];
  return typeof v === "number" ? v : null;
}

// A compact, human line of the engine-computed facts a candidate carries.
function annLine(c: Candidate): string {
  const a = c.annotation;
  const bits: string[] = [];
  const hit = num(a, "hit_odds");
  if (hit != null) bits.push(`${Math.round(hit * 100)}% hit`);
  const exp = num(a, "expected_clicks");
  if (exp != null) bits.push(`~${exp.toFixed(1)} clk`);
  const heal = a["heal_amount"];
  if (heal != null) bits.push(`heal ${heal === "1d6" ? "d6" : heal}`);
  const dist = num(a, "move_distance");
  if (dist != null) bits.push(`${dist.toFixed(1)}"`);
  if (a["rear"] === true) bits.push("rear +1");
  if (a["free"] === true) bits.push("free");
  const rev = num(a, "revive_points");
  if (rev != null) bits.push(`${rev} pts`);
  return bits.join(" · ");
}

export default function ActionPanel({
  view,
  selectedFig,
  candidates,
  armed,
  busy,
  onArm,
  onConfirm,
  onCancel,
}: Props) {
  const isHumanTurn = view.meta.active_player === "human" && !view.meta.ended;

  let hint: string | null = null;
  if (view.meta.ended) hint = `Game over — winner: ${view.meta.winner ?? "draw"}.`;
  else if (!isHumanTurn) hint = "Opponent's turn.";
  else if (!selectedFig) hint = "Select one of your figures to act.";
  else if (selectedFig.owner !== "human") hint = `${selectedFig.short_name} is an opponent (read-only).`;
  else if (!selectedFig.can_act) hint = `${selectedFig.short_name} has no action left this turn.`;

  const groups = new Map<Group, Candidate[]>();
  if (!hint) {
    for (const c of candidates) {
      const g = groupOf(c.kind);
      if (!groups.has(g)) groups.set(g, []);
      groups.get(g)!.push(c);
    }
  }

  return (
    <div className="action-panel">
      <div className="action-head">
        <span>Actions</span>
        {selectedFig && !hint && (
          <span className="fig-sub">{selectedFig.short_name}</span>
        )}
      </div>

      {hint && <div className="empty">{hint}</div>}

      {!hint && armed && (
        <div className="armed">
          <div className="armed-title">{armed.label}</div>
          {annLine(armed) && <div className="armed-stats">{annLine(armed)}</div>}
          <div className="armed-btns">
            <button className="btn primary" onClick={onConfirm} disabled={busy}>
              Confirm
            </button>
            <button className="btn" onClick={onCancel} disabled={busy}>
              Cancel
            </button>
          </div>
        </div>
      )}

      {!hint && !armed && (
        <div className="action-groups">
          {candidates.length === 0 && (
            <div className="empty">No legal actions (drag on the board to move).</div>
          )}
          {GROUP_ORDER.map((g) => {
            const list = groups.get(g);
            if (!list || list.length === 0) return null;
            return (
              <div className="action-group" key={g}>
                <div className="action-group-label">{g}</div>
                {list.map((c, i) => (
                  <button
                    className="action-btn"
                    key={`${c.kind}-${i}`}
                    onClick={() => onArm(c)}
                    disabled={busy}
                    title={c.label}
                  >
                    <span className="action-label">{c.label}</span>
                    {annLine(c) && <span className="action-stats">{annLine(c)}</span>}
                  </button>
                ))}
              </div>
            );
          })}
        </div>
      )}

      {!hint && (
        <div className="action-foot">
          {view.meta.actions_remaining} action
          {view.meta.actions_remaining === 1 ? "" : "s"} left
        </div>
      )}
    </div>
  );
}
