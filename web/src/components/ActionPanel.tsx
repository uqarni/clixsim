import type { AttackExplain, Candidate, FigureView, GameView } from "../api";

interface PendingMove {
  dest: [number, number];
  facing: number;
}

interface Props {
  view: GameView;
  selectedFig: FigureView | null;
  candidates: Candidate[];
  hints: string[];
  formations: Candidate[];
  armed: Candidate | null;
  explain: AttackExplain | null;
  pendingMove: PendingMove | null;
  busy: boolean;
  onArm: (c: Candidate) => void;
  onConfirm: () => void;
  onCancel: () => void;
  onConfirmMove: () => void;
  onCancelMove: () => void;
  // Interactive formation move (place members one at a time).
  formation: {
    total: number;
    placedCount: number;
    currentName: string | null;
    speed: number;
    canDefer: boolean;
  } | null;
  onFormationStart: (c: Candidate) => void;
  onFormationBack: () => void;
  onFormationLeave: () => void;
  onFormationDefer: () => void;
  onFormationCancel: () => void;
  onFormationSubmit: () => void;
  // Marquee / shift+click group selection: live formation legality + entry point.
  group: { uids: number[]; names: string[]; ok: boolean; reason: string | null } | null;
  onGroupMove: () => void;
  onGroupClear: () => void;
}

const ATTACK_KINDS = new Set([
  "ranged",
  "close",
  "weapon_master",
  "magic_blast",
  "flame_lightning",
  "shockwave",
]);

function variantName(kind: string): string {
  switch (kind) {
    case "ranged":
      return "Shoot";
    case "close":
      return "Attack";
    case "weapon_master":
      return "Weapon Master";
    case "magic_blast":
      return "Magic Blast";
    case "flame_lightning":
      return "Flame / Lightning";
    case "shockwave":
      return "Shockwave";
    default:
      return kind;
  }
}

function num(a: Record<string, unknown>, k: string): number | null {
  return typeof a[k] === "number" ? (a[k] as number) : null;
}

function annLine(c: Candidate): string {
  const a = c.annotation;
  const bits: string[] = [];
  const hit = num(a, "hit_odds");
  if (hit != null) bits.push(`${Math.round(hit * 100)}%`);
  const exp = num(a, "expected_clicks");
  if (exp != null) bits.push(`~${exp.toFixed(1)} clk`);
  const heal = a["heal_amount"];
  if (heal != null) bits.push(`heal ${heal === "1d6" ? "d6" : heal}`);
  const dist = num(a, "move_distance");
  if (dist != null) bits.push(`${dist.toFixed(1)}"`);
  if (a["rear"] === true) bits.push("rear +1");
  if (a["free"] === true) bits.push("free");
  return bits.join(" · ");
}

function targetUid(c: Candidate): number | null {
  const a = c.annotation;
  if (typeof a.target === "number") return a.target;
  if (Array.isArray(a.targets) && typeof a.targets[0] === "number") return a.targets[0];
  return null;
}

function Breakdown({ x }: { x: AttackExplain }) {
  const d = x.defense;
  const g = x.damage;
  const defMods: string[] = [];
  if (d.battle_armor) defMods.push(`Battle Armor +${d.battle_armor}`);
  if (d.defend) defMods.push(`Defend +${d.defend}`);
  const dmgMods: string[] = [];
  if (g.enhancement) dmgMods.push(`Enhancement +${g.enhancement}`);
  if (g.toughness) dmgMods.push(`Toughness ${g.toughness}`);
  return (
    <div className="explain">
      <div className="explain-row">
        <span className="k">Defense</span>
        <span>
          {d.base}
          {d.effective !== d.base && ` → ${d.effective}`}
          {defMods.length > 0 && <span className="explain-mod up"> ({defMods.join(", ")})</span>}
        </span>
      </div>
      <div className="explain-row">
        <span className="k">Damage / hit</span>
        <span>
          {g.base}
          {g.per_hit !== g.base && ` → ${g.per_hit}`}
          {dmgMods.length > 0 && (
            <span className={`explain-mod ${g.toughness < 0 ? "down" : "up"}`}> ({dmgMods.join(", ")})</span>
          )}
        </span>
      </div>
      <div className="explain-row">
        <span className="k">Outcome</span>
        <span>
          {Math.round(x.hit_odds * 100)}% hit · ~{x.expected_clicks.toFixed(1)} clk
        </span>
      </div>
    </div>
  );
}

export default function ActionPanel({
  view,
  selectedFig,
  candidates,
  hints,
  formations,
  armed,
  explain,
  pendingMove,
  busy,
  onArm,
  onConfirm,
  onCancel,
  onConfirmMove,
  onCancelMove,
  formation,
  onFormationStart,
  onFormationBack,
  onFormationLeave,
  onFormationDefer,
  onFormationCancel,
  onFormationSubmit,
  group,
  onGroupMove,
  onGroupClear,
}: Props) {
  const isHumanTurn = view.meta.active_player === "human" && !view.meta.ended;

  if (view.meta.ended) {
    return (
      <div className="action-panel">
        <div className="action-head"><span>Actions</span></div>
        <div className="empty">Game over — winner: {view.meta.winner ?? "draw"}.</div>
      </div>
    );
  }
  if (!isHumanTurn) {
    return (
      <div className="action-panel">
        <div className="action-head"><span>Actions</span></div>
        <div className="empty">Opponent's turn.</div>
      </div>
    );
  }

  const nameOf = (uid: number | null) =>
    uid == null ? "" : view.figures.find((f) => f.uid === uid)?.short_name ?? `#${uid}`;

  // Group the selected figure's attack candidates by target for the variant chooser.
  const attacks = candidates.filter((c) => ATTACK_KINDS.has(c.kind));
  const supports = candidates.filter((c) => ["heal", "regenerate", "necromancy", "levitate"].includes(c.kind));
  const moves = candidates.filter((c) => c.kind === "move");
  const passes = candidates.filter((c) => c.kind === "pass");
  const byTarget = new Map<number, Candidate[]>();
  const areaAttacks: Candidate[] = [];
  for (const c of attacks) {
    const t = targetUid(c);
    if (t == null) areaAttacks.push(c);
    else {
      if (!byTarget.has(t)) byTarget.set(t, []);
      byTarget.get(t)!.push(c);
    }
  }

  const armedIsAttack = armed != null && ATTACK_KINDS.has(armed.kind);

  return (
    <div className="action-panel">
      <div className="action-head">
        <span>Actions</span>
        {selectedFig && <span className="fig-sub">{selectedFig.short_name}</span>}
      </div>

      {/* Interactive formation move: place each member, then submit as one action */}
      {formation && (
        <div className="armed formation-stage">
          <div className="armed-title">
            Formation move · speed {formation.speed}″
          </div>
          {formation.placedCount < formation.total ? (
            <>
              <div className="armed-stats">
                Placing <strong>{formation.currentName ?? "member"}</strong> ({formation.placedCount + 1} of{" "}
                {formation.total}) — drag it on the board
                {pendingMove ? ", aim the handle, then confirm" : ". It snaps to nearby bases"}
                {formation.placedCount > 0 ? "; it must end touching a placed member." : "."}
              </div>
              <div className="armed-btns">
                {pendingMove ? (
                  <>
                    <button className="btn primary" onClick={onConfirmMove} disabled={busy}>
                      Confirm member
                    </button>
                    <button className="btn" onClick={onCancelMove} disabled={busy}>Re-place</button>
                  </>
                ) : (
                  <>
                    <button className="btn" onClick={onFormationLeave} disabled={busy}>Leave in place</button>
                    {formation.canDefer && (
                      <button className="btn" onClick={onFormationDefer} disabled={busy} title="Place this member after the others">
                        Place later
                      </button>
                    )}
                  </>
                )}
                <button className="btn" onClick={onFormationBack} disabled={busy || (formation.placedCount === 0 && !pendingMove)}>
                  Back
                </button>
                <button className="btn" onClick={onFormationCancel} disabled={busy}>Cancel</button>
              </div>
            </>
          ) : (
            <>
              <div className="armed-stats">All {formation.total} members placed — one action moves them all.</div>
              <div className="armed-btns">
                <button className="btn primary" onClick={onFormationSubmit} disabled={busy}>
                  Confirm formation move
                </button>
                <button className="btn" onClick={onFormationBack} disabled={busy}>Back</button>
                <button className="btn" onClick={onFormationCancel} disabled={busy}>Cancel</button>
              </div>
            </>
          )}
        </div>
      )}

      {/* Group selection (marquee / shift+click): move as a formation, or the
          exact reason the group can't. */}
      {!formation && !pendingMove && !armed && group && (
        <div className="armed group-panel">
          <div className="armed-title">Group · {group.uids.length} selected</div>
          <div className="armed-stats group-names">{group.names.join(" · ")}</div>
          <div className="armed-btns">
            <button
              className="btn primary"
              onClick={onGroupMove}
              disabled={busy || !group.ok}
              title={group.ok ? "One action moves the whole group" : group.reason ?? ""}
            >
              Move as formation
            </button>
            <button className="btn" onClick={onGroupClear} disabled={busy}>
              Clear
            </button>
          </div>
          {!group.ok && group.reason && (
            <div className="group-reason">✕ {group.reason}</div>
          )}
          {group.ok && (
            <div className="group-reason ok">✓ legal formation — one action moves all {group.uids.length}</div>
          )}
        </div>
      )}

      {/* Pending free move: place -> aim -> confirm */}
      {!formation && pendingMove && (
        <div className="armed">
          <div className="armed-title">
            Move to ({pendingMove.dest[0].toFixed(1)}, {pendingMove.dest[1].toFixed(1)})
          </div>
          <div className="armed-stats">Drag the handle on the board to aim, then confirm.</div>
          <div className="armed-btns">
            <button className="btn primary" onClick={onConfirmMove} disabled={busy}>Confirm</button>
            <button className="btn" onClick={onCancelMove} disabled={busy}>Cancel</button>
          </div>
        </div>
      )}

      {/* Armed candidate (attack / support / etc.) */}
      {!formation && !pendingMove && armed && (
        <div className="armed">
          <div className="armed-title">{armed.label}</div>
          {armedIsAttack && explain ? <Breakdown x={explain} /> : annLine(armed) && <div className="armed-stats">{annLine(armed)}</div>}
          <div className="armed-btns">
            <button className="btn primary" onClick={onConfirm} disabled={busy}>Confirm</button>
            <button className="btn" onClick={onCancel} disabled={busy}>Cancel</button>
          </div>
        </div>
      )}

      {/* Candidate menu */}
      {!formation && !pendingMove && !armed && !group && (
        <div className="action-groups">
          {!selectedFig && formations.length === 0 && (
            <div className="empty">Select one of your figures, or drag it on the board to move.</div>
          )}

          {selectedFig && selectedFig.owner === "human" && !selectedFig.can_act && (
            <div className="empty">{selectedFig.short_name} has no action left.</div>
          )}

          {(byTarget.size > 0 || areaAttacks.length > 0) && (
            <div className="action-group">
              <div className="action-group-label">Attack</div>
              {[...byTarget.entries()].map(([t, variants]) => (
                <div className="target-group" key={t}>
                  <span className="target-name">{nameOf(t)}</span>
                  <div className="variant-row">
                    {variants.map((c, i) => (
                      <button className="variant-btn" key={i} onClick={() => onArm(c)} disabled={busy} title={c.label}>
                        {variantName(c.kind)} <span className="vstat">{annLine(c)}</span>
                      </button>
                    ))}
                  </div>
                </div>
              ))}
              {areaAttacks.map((c, i) => (
                <button className="action-btn" key={`area-${i}`} onClick={() => onArm(c)} disabled={busy} title={c.label}>
                  <span className="action-label">{c.label}</span>
                  <span className="action-stats">{annLine(c)}</span>
                </button>
              ))}
            </div>
          )}

          {supports.length > 0 && (
            <div className="action-group">
              <div className="action-group-label">Support</div>
              {supports.map((c, i) => (
                <button className="action-btn" key={i} onClick={() => onArm(c)} disabled={busy} title={c.label}>
                  <span className="action-label">{c.label}</span>
                  <span className="action-stats">{annLine(c)}</span>
                </button>
              ))}
            </div>
          )}

          {moves.length > 0 && (
            <div className="action-group">
              <div className="action-group-label">Move</div>
              {moves.map((c, i) => (
                <button className="action-btn" key={i} onClick={() => onArm(c)} disabled={busy} title={c.label}>
                  <span className="action-label">{c.label}</span>
                  <span className="action-stats">{annLine(c)}</span>
                </button>
              ))}
            </div>
          )}

          {formations.length > 0 && (
            <div className="action-group">
              <div className="action-group-label">Formations</div>
              {formations.map((c, i) =>
                c.kind === "formation_move" ? (
                  <div className="action-btn-row" key={i}>
                    <button
                      className="action-btn"
                      onClick={() => onFormationStart(c)}
                      disabled={busy}
                      title={`${c.label} — place each member yourself`}
                    >
                      <span className="action-label">{c.label}</span>
                      <span className="action-stats">place each member · one action</span>
                    </button>
                    {!c.annotation.manual_only && (
                      <button
                        className="btn action-auto"
                        onClick={() => onArm(c)}
                        disabled={busy}
                        title="Move the whole formation straight toward the enemy in one click"
                      >
                        auto
                      </button>
                    )}
                  </div>
                ) : (
                  <button className="action-btn" key={i} onClick={() => onArm(c)} disabled={busy} title={c.label}>
                    <span className="action-label">{c.label}</span>
                    <span className="action-stats">{annLine(c)}</span>
                  </button>
                ),
              )}
            </div>
          )}

          {passes.length > 0 && (
            <div className="action-group">
              <div className="action-group-label">Turn</div>
              {passes.map((c, i) => (
                <button className="action-btn" key={i} onClick={() => onArm(c)} disabled={busy} title={c.label}>
                  <span className="action-label">{c.label}</span>
                </button>
              ))}
            </div>
          )}
        </div>
      )}

      {hints.length > 0 && (
        <div className="action-hints">
          {hints.map((h, i) => (
            <div className="action-hint" key={i}>
              <span className="action-hint-icon">i</span>
              <span>{h}</span>
            </div>
          ))}
        </div>
      )}

      <div className="action-foot">
        {view.meta.actions_remaining} action{view.meta.actions_remaining === 1 ? "" : "s"} left
      </div>
    </div>
  );
}
