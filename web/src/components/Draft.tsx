import { useEffect, useMemo, useState } from "react";
import { getRoster, getSealedPacks, type ConstructFigure } from "../api";
import type { GameConfig } from "./NewGame";

// Rank -> star tier (Weak * / Standard ** / Tough ***); Unique shown as a badge.
const RANK_TIER: Record<string, number> = { Weak: 1, Standard: 2, Tough: 3 };

function RankBadge({ rank }: { rank: string }) {
  if (rank === "Unique") return <span className="cand-rank uniq" title="Unique">◆ U</span>;
  const n = RANK_TIER[rank] ?? 0;
  return (
    <span className="cand-rank" title={rank}>
      {"★".repeat(n)}
      <span className="rank-dim">{"★".repeat(3 - n)}</span>
    </span>
  );
}

function StatLine({ f }: { f: ConstructFigure }) {
  if (!f.stats) return null;
  const s = f.stats;
  return (
    <div className="cand-stats">
      <span title="Speed">S{s.speed}</span>
      <span title="Attack">A{s.attack}</span>
      <span title="Defense">D{s.defense}</span>
      <span title="Damage">Dm{s.damage}</span>
      {s.range > 0 && <span title="Range / targets" className="stat-range">R{s.range}{s.targets > 1 ? `×${s.targets}` : ""}</span>}
      {f.clicks != null && <span title="Life (clicks)" className="stat-life">♥{f.clicks}</span>}
    </div>
  );
}

function ArmyComposition({ army }: { army: ConstructFigure[] }) {
  const byFaction = new Map<string, number>();
  let melee = 0;
  let ranged = 0;
  for (const f of army) {
    byFaction.set(f.faction, (byFaction.get(f.faction) ?? 0) + 1);
    if (f.role === "ranged") ranged++;
    else melee++;
  }
  const factions = [...byFaction.entries()].sort((a, b) => b[1] - a[1]);
  const maxF = Math.max(1, ...factions.map(([, n]) => n));
  if (army.length === 0) return null;
  return (
    <div className="army-comp">
      <div className="comp-block">
        <div className="comp-label">Roles</div>
        <div className="comp-split">
          <div className="comp-seg melee" style={{ flex: melee || 0.0001 }} title={`${melee} melee`}>
            {melee > 0 && `⚔ ${melee}`}
          </div>
          <div className="comp-seg ranged" style={{ flex: ranged || 0.0001 }} title={`${ranged} ranged`}>
            {ranged > 0 && `➶ ${ranged}`}
          </div>
        </div>
      </div>
      <div className="comp-block">
        <div className="comp-label">Factions</div>
        {factions.map(([fac, n]) => (
          <div className="comp-row" key={fac}>
            <span className="comp-name" title={fac}>{fac}</span>
            <span className="comp-bar-wrap">
              <span className="comp-bar" style={{ width: `${(n / maxF) * 100}%` }} />
            </span>
            <span className="comp-n">{n}</span>
          </div>
        ))}
      </div>
      <div className="comp-foot">
        {army.length} figure{army.length === 1 ? "" : "s"} · {byFaction.size} faction{byFaction.size === 1 ? "" : "s"}
      </div>
    </div>
  );
}

export default function Draft({
  config,
  onConfirm,
  onCancel,
}: {
  config: GameConfig;
  onConfirm: (ids: number[]) => void;
  onCancel: () => void;
}) {
  const isSealed = config.mode === "sealed";
  const budget = isSealed ? 200 : config.points;

  const [roster, setRoster] = useState<ConstructFigure[]>([]);
  const [packs, setPacks] = useState<ConstructFigure[][]>([]);
  const [opened, setOpened] = useState<boolean[]>([false, false, false, false]);
  const [sealedPhase, setSealedPhase] = useState<"open" | "build">(isSealed ? "open" : "build");
  const [army, setArmy] = useState<ConstructFigure[]>([]);
  const [faction, setFaction] = useState("all");
  const [role, setRole] = useState("all");
  const [q, setQ] = useState("");
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    (isSealed
      ? getSealedPacks(config.seed, config.expansions).then(setPacks)
      : getRoster(config.expansions).then(setRoster)
    ).finally(() => setLoading(false));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const spent = army.reduce((s, f) => s + f.points, 0);
  const remaining = budget - spent;
  const pool = useMemo(() => opened.flatMap((o, i) => (o && packs[i] ? packs[i] : [])), [opened, packs]);
  const allOpened = opened.every(Boolean);

  const armyCount = (id: number) => army.filter((f) => f.id === id).length;
  const poolCount = (id: number) => pool.filter((f) => f.id === id).length;
  const hasUnique = (id: number) => army.some((f) => f.id === id);

  const canAdd = (f: ConstructFigure) => {
    if (f.points > remaining) return false;
    if (f.rank === "Unique" && hasUnique(f.id)) return false;
    if (isSealed && poolCount(f.id) - armyCount(f.id) <= 0) return false;
    return true;
  };

  const factions = useMemo(() => ["all", ...Array.from(new Set(roster.map((f) => f.faction))).sort()], [roster]);

  const candidates = useMemo(() => {
    if (isSealed) {
      const seen = new Map<number, ConstructFigure>();
      for (const f of pool) if (!seen.has(f.id)) seen.set(f.id, f);
      return [...seen.values()].sort((a, b) => b.points - a.points);
    }
    return roster.filter(
      (f) =>
        (faction === "all" || f.faction === faction) &&
        (role === "all" || f.role === role) &&
        (q === "" || f.name.toLowerCase().includes(q.toLowerCase())),
    );
  }, [isSealed, pool, roster, faction, role, q]);

  // Organize the candidate list into faction sections (sorted; figures priciest first).
  const groups = useMemo(() => {
    const m = new Map<string, ConstructFigure[]>();
    for (const f of candidates) {
      if (!m.has(f.faction)) m.set(f.faction, []);
      m.get(f.faction)!.push(f);
    }
    return [...m.entries()]
      .sort((a, b) => a[0].localeCompare(b[0]))
      .map(([fac, figs]) => [fac, figs.slice().sort((x, y) => y.points - x.points)] as const);
  }, [candidates]);

  const legal = army.length > 0 && spent <= budget;
  const pct = Math.min(100, Math.round((spent / budget) * 100));

  if (loading) return <div className="menu"><div className="menu-card">Loading…</div></div>;

  // --- sealed: pack opening ---
  if (isSealed && sealedPhase === "open") {
    return (
      <div className="menu">
        <div className="menu-card wide">
          <h1 className="menu-title">Open your boosters</h1>
          <p className="menu-sub">Four packs, five figures each. Open them to see what you pull.</p>
          <div className="pack-row">
            {packs.map((pk, i) => (
              <div className="pack" key={i}>
                {opened[i] ? (
                  <div className="pack-cards">
                    {pk.map((f, j) => (
                      <div className="pull-card" style={{ animationDelay: `${j * 60}ms` }} key={j}>
                        <div className="pull-name">{f.name}</div>
                        <div className="pull-meta">
                          {f.faction} · {f.role} · <b>{f.points}</b>
                        </div>
                        {f.rank === "Unique" && <span className="badge warn">unique</span>}
                      </div>
                    ))}
                  </div>
                ) : (
                  <button
                    className="pack-back"
                    onClick={() => setOpened((o) => o.map((v, k) => (k === i ? true : v)))}
                    type="button"
                  >
                    <div className="pack-label">Pack {i + 1}</div>
                    <div className="pack-hint">Click to open</div>
                  </button>
                )}
              </div>
            ))}
          </div>
          <div className="construct-actions">
            <button className="btn" onClick={onCancel} type="button">Back</button>
            <button className="btn" onClick={() => setOpened([true, true, true, true])} type="button">
              Open all
            </button>
            <button className="btn primary" disabled={!allOpened} onClick={() => setSealedPhase("build")} type="button">
              Build army →
            </button>
          </div>
        </div>
      </div>
    );
  }

  // --- building (both modes) ---
  return (
    <div className="menu">
      <div className="menu-card wide">
        <div className="construct-head">
          <h1 className="menu-title">Draft your army</h1>
          <span className="menu-sub">
            {isSealed ? "Sealed — build from your pool" : "Preconstructed — pick from the full roster"} · {spent}/{budget} pts
          </span>
        </div>
        <div className="draft-bar">
          <div className="draft-fill" style={{ width: `${pct}%`, background: "var(--human)" }} />
        </div>

        {!isSealed && (
          <div className="draft-filters">
            <select value={faction} onChange={(e) => setFaction(e.target.value)}>
              {factions.map((f) => (
                <option key={f} value={f}>{f === "all" ? "All factions" : f}</option>
              ))}
            </select>
            <select value={role} onChange={(e) => setRole(e.target.value)}>
              <option value="all">All roles</option>
              <option value="ranged">Ranged</option>
              <option value="melee">Melee</option>
            </select>
            <input placeholder="Search…" value={q} onChange={(e) => setQ(e.target.value)} />
          </div>
        )}

        <div className="draft-2col">
          <div className="draft-candidates">
            {groups.map(([fac, figs]) => (
              <div className="cand-group" key={fac}>
                <div className="cand-group-head">
                  {fac} <span className="cand-group-n">{figs.length}</span>
                </div>
                <div className="cand-group-grid">
                {figs.map((f) => {
                  const left = isSealed ? poolCount(f.id) - armyCount(f.id) : null;
                  return (
                    <button
                      className="cand-card"
                      key={f.id}
                      onClick={() => canAdd(f) && setArmy((a) => [...a, f])}
                      disabled={!canAdd(f)}
                      type="button"
                      title={f.abilities.join(", ")}
                    >
                      <div className="cand-top">
                        <span className="cand-name">
                          {f.mounted && (
                            <span title="Mounted — double base (cavalry)" aria-label="mounted">🐴 </span>
                          )}
                          {f.name}
                        </span>
                        <RankBadge rank={f.rank} />
                        <span className="cand-pts">{f.points}</span>
                      </div>
                      <div className="cand-sub">
                        {f.role}
                        {left != null && ` · ${left} left`}
                      </div>
                      <StatLine f={f} />
                      {f.abilities.length > 0 && <div className="cand-abil">{f.abilities.join(" · ")}</div>}
                    </button>
                  );
                })}
                </div>
              </div>
            ))}
          </div>

          <div className="draft-army">
            <div className="menu-section-label"><span className="dot human" /> Your army — {army.length} figs · {spent} pts</div>
            <ArmyComposition army={army} />
            {army.length === 0 && <div className="empty">Click figures to add them.</div>}
            {army.map((f, i) => (
              <div className="army-row" key={i}>
                <span className="draft-name">{f.name}</span>
                <span className="draft-pts">{f.points}</span>
                <button className="army-remove" onClick={() => setArmy((a) => a.filter((_, j) => j !== i))} type="button" aria-label="remove">×</button>
              </div>
            ))}
          </div>
        </div>

        <div className="construct-actions">
          <button className="btn" onClick={isSealed ? () => setSealedPhase("open") : onCancel} type="button">
            Back
          </button>
          <button className="btn primary" disabled={!legal} onClick={() => onConfirm(army.map((f) => f.id))} type="button">
            Confirm army → watch the opponent draft
          </button>
        </div>
      </div>
    </div>
  );
}
