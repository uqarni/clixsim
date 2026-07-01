import type { FigureView, GameEvent, GameView } from "../api";

interface Props {
  view: GameView;
  events: GameEvent[];
}

function eventLine(e: GameEvent, name: (uid: unknown) => string): string {
  if (typeof e.summary === "string") return e.summary;
  const dice = Array.isArray(e.dice) ? ` (${(e.dice as number[]).join("+")})` : "";
  const clk = typeof e.clicks === "number" ? e.clicks : 0;
  const ko = e.eliminated ? " — KO" : "";

  const attack = (a: string, t: string) => {
    if (e.result === "miss") return `${a} misses ${t}${dice}`;
    if (e.result === "crit_miss") return `${a} critically misses ${t}${dice}`;
    const crit = e.result === "crit_hit" ? " CRIT" : "";
    return `${a} hits ${t}${crit}${dice} for ${clk} clk${ko}`;
  };

  switch (e.type) {
    case "move": {
      const to = e.to as number[] | undefined;
      const dest = Array.isArray(to) ? ` to (${to[0].toFixed(1)}, ${to[1].toFixed(1)})` : "";
      return e.moved === false
        ? `${name(e.figure)} can't break away`
        : `${name(e.figure)} moves${dest}`;
    }
    case "break_away":
      return `${name(e.figure)} break-away: rolled ${e.roll} — ${e.success ? "clear" : "failed"}`;
    case "ranged_attack":
    case "magic_blast":
    case "flame_lightning":
    case "shockwave":
      return attack(name(e.attacker), name(e.target));
    case "close_attack":
      return attack(name(e.attacker), name(e.target)) + (e.rear ? " (rear)" : "");
    case "pole_arm":
      return `${name(e.target)} takes ${clk} from ${name(e.attacker)}'s pole arm${ko}`;
    case "crit_miss_self":
      return `${name(e.figure)} backfires — ${clk} self-click${ko}`;
    case "push_damage":
      return `${name(e.figure)} pushes — ${clk} self-click${ko}`;
    case "healing":
    case "magic_healing":
      return `${name(e.healer)} heals ${name(e.target)} (${e.healed ?? 0} clk)`;
    case "regenerate":
      return `${name(e.figure)} regenerates (${e.healed ?? 0} clk)`;
    case "vampirism":
      return `${name(e.figure)} drains ${e.healed ?? 0} clk`;
    case "necromancy":
      return `${name(e.necromancer)} revives ${name(e.target)}`;
    case "necromancy_fail":
      return `${name(e.necromancer)}'s necromancy fails`;
    case "levitate":
      return `${name(e.caster)} levitates ${name(e.target)}`;
    case "command_bonus":
      return `${name(e.figure)} command: rolled ${e.roll}${e.roll === 6 ? " — bonus action" : ""}`;
    case "command_heal":
      return `${name(e.figure)} rallies ${name(e.target)}`;
    case "pass":
      return `${name(e.figure)} passes`;
    case "toggle_ability":
      return `${name(e.figure)} ${e.off ? "cancels" : "restores"} ${e.name}`;
    case "eliminated":
      return `${name(e.figure)} eliminated`;
    case "formation_move": // the per-member move events carry the detail
    case "begin_turn":
    case "end_turn":
      return "";
    default:
      return e.type;
  }
}

// Zone E: scrolling event log (upper) + VP / casualty ledger (lower).
export default function LogLedger({ view, events }: Props) {
  const eliminated: FigureView[] = view.figures.filter((f) => f.eliminated);
  const vp = view.meta.victory_points;

  const nameOf = (uid: unknown): string => {
    const f = view.figures.find((x) => x.uid === uid);
    return f ? f.short_name : `#${uid}`;
  };
  const lines = events
    .map((e) => eventLine(e, nameOf))
    .filter((s) => s.length > 0);

  return (
    <div className="zone">
      <div className="zone-head">
        <span>Log + ledger</span>
      </div>
      <div className="zone-body no-pad">
        <div className="split">
          <div className="upper" style={{ padding: 8 }}>
            <div className="log-list">
              {lines.length === 0 && <div className="empty">No events yet.</div>}
              {lines.map((line, i) => (
                <div className="log-line" key={i}>
                  <span className="lt">{String(i + 1).padStart(2, "0")}</span>
                  {line}
                </div>
              ))}
            </div>
          </div>

          <div className="lower" style={{ padding: 8 }}>
            <div className="ledger">
              <div className="ledger-vp">
                <div className="side">
                  <div className="fig-sub">
                    <span className="dot human" /> Human VP
                  </div>
                  <div className="n">{vp.human}</div>
                </div>
                <div className="side">
                  <div className="fig-sub">
                    <span className="dot llm" /> LLM VP
                  </div>
                  <div className="n">{vp.llm}</div>
                </div>
              </div>

              <div className="section-label">Eliminated</div>
              {eliminated.length === 0 ? (
                <div className="empty">No casualties.</div>
              ) : (
                eliminated.map((f) => (
                  <div className="casualty" key={f.uid}>
                    <span className={`dot ${f.owner}`} />
                    <span>{f.name}</span>
                    <span className="fig-sub">· {f.points} pts</span>
                  </div>
                ))
              )}
            </div>
          </div>
        </div>
      </div>
    </div>
  );
}
