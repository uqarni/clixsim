import type { DialClick, FigureView } from "../api";

interface Props {
  fig: FigureView | null;
}

function AbilityChips({ abilities }: { abilities: DialClick["abilities"] }) {
  if (abilities.length === 0) return <span className="fig-sub">—</span>;
  return (
    <div className="ability-chips">
      {abilities.map((a) => (
        <span
          key={`${a.id}-${a.slot}`}
          className={`chip${a.optional ? " optional" : ""}`}
          title={`${a.name} (${a.slot}${a.optional ? ", optional" : ""})`}
        >
          {a.name}
        </span>
      ))}
    </div>
  );
}

// Zone B: full combat dial for the selected figure + stat block + abilities.
export default function DialInspector({ fig }: Props) {
  return (
    <div className="zone">
      <div className="zone-head">
        <span>Dial inspector</span>
        {fig && <span className="fig-sub">{fig.short_name}</span>}
      </div>
      <div className="zone-body">
        {!fig && <div className="empty">Select a figure to inspect its dial.</div>}

        {fig && (
          <>
            <div className="stat-block">
              <div className="stat">
                <div className="k">Speed</div>
                <div className="v">{fig.speed}</div>
              </div>
              <div className="stat">
                <div className="k">Attack</div>
                <div className="v">{fig.attack}</div>
              </div>
              <div className="stat">
                <div className="k">Defense</div>
                <div className="v">{fig.defense}</div>
              </div>
              <div className="stat">
                <div className="k">Damage</div>
                <div className="v">{fig.damage}</div>
              </div>
            </div>

            <div className="fig-sub">
              {fig.name} · {fig.faction} · click {fig.current_click + 1}/
              {fig.dial.length}
              {fig.range > 0 && ` · range ${fig.range}`}
              {fig.is_ranged && ` · targets ${fig.targets}`}
            </div>

            <div className="section-label">Active abilities</div>
            {fig.active_abilities.length === 0 ? (
              <div className="empty">None on the current click.</div>
            ) : (
              <div className="ability-chips">
                {fig.active_abilities.map((a) => (
                  <span
                    key={a.id}
                    className={`chip${a.optional ? " optional" : ""}`}
                    title={a.optional ? "Optional" : "Always on"}
                  >
                    {a.name}
                  </span>
                ))}
              </div>
            )}

            <div className="section-label">Combat dial</div>
            <div className="dial-head">
              <span>#</span>
              <span>Spd</span>
              <span>Atk</span>
              <span>Def</span>
              <span>Dmg</span>
              <span>Abilities</span>
            </div>
            <div className="dial-table">
              {fig.dial.map((c) => {
                const before = c.index < fig.starting_click;
                const current = c.index === fig.current_click;
                const dead = c.index >= fig.dial.length; // never true, kept for clarity
                const cls = [
                  "dial-row",
                  before ? "before" : "",
                  current ? "current" : "",
                  dead ? "dead" : "",
                ]
                  .filter(Boolean)
                  .join(" ");
                return (
                  <div className={cls} key={c.index}>
                    <span className="idx">{c.index}</span>
                    <span>{c.speed}</span>
                    <span>{c.attack}</span>
                    <span>{c.defense}</span>
                    <span>{c.damage}</span>
                    <AbilityChips abilities={c.abilities} />
                  </div>
                );
              })}
            </div>
          </>
        )}
      </div>
    </div>
  );
}
