import type { FigureView } from "../api";

interface Props {
  fig: FigureView;
  selected: boolean;
  onSelect: (uid: number) => void;
}

function healthColor(frac: number): string {
  if (frac > 0.5) return "var(--good)";
  if (frac > 0.25) return "var(--warn)";
  return "var(--bad)";
}

// Compact figure card shared by the force rail and opponent panel.
export default function FigCard({ fig, selected, onSelect }: Props) {
  const spent = !fig.can_act;
  const cls = [
    "fig-card",
    fig.owner,
    selected ? "selected" : "",
    spent ? "spent" : "",
  ]
    .filter(Boolean)
    .join(" ");

  const pct = Math.round(Math.max(0, Math.min(1, fig.health_fraction)) * 100);

  return (
    <button className={cls} onClick={() => onSelect(fig.uid)} type="button">
      <div className="fig-card-top">
        <span className="fig-name">{fig.name}</span>
        <span className="fig-sub">{fig.points} pts</span>
      </div>

      <div className="health-bar" title={`Health ${pct}%`}>
        <div
          className="health-fill"
          style={{ width: `${pct}%`, background: healthColor(fig.health_fraction) }}
        />
      </div>

      <div className="fig-card-top">
        <span className="fig-sub">
          {fig.faction} · SPD {fig.speed} ATK {fig.attack} DEF {fig.defense} DMG{" "}
          {fig.damage}
        </span>
        <span style={{ display: "inline-flex", gap: 6, alignItems: "center" }}>
          {fig.action_tokens > 0 && (
            <span
              className="token-pips"
              title={`${fig.action_tokens} push token(s)`}
            >
              {Array.from({ length: fig.action_tokens }).map((_, i) => (
                <span className="token-pip" key={i} />
              ))}
            </span>
          )}
          {fig.demoralized && <span className="badge warn">demoralized</span>}
        </span>
      </div>
    </button>
  );
}
