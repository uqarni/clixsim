// ---------------------------------------------------------------------------
// Clix Engine data contract + API client.
//
// The Python game engine is the single source of truth and serves a JSON/HTTP
// API. This module documents that contract as TypeScript types and provides a
// thin client. While USE_MOCK is true the client returns the bundled mock so
// the browser app runs standalone; flip it to false to hit the real server
// (the Vite dev server proxies /api -> http://localhost:8000).
// ---------------------------------------------------------------------------

// Flip to true to run standalone on the bundled mock (no backend needed).
export const USE_MOCK = false;

export type Owner = "human" | "llm";

export interface DialAbility {
  id: number;
  name: string;
  slot: string;
  optional: boolean;
}

export interface DialClick {
  index: number;
  speed: number;
  attack: number;
  defense: number;
  damage: number;
  abilities: DialAbility[];
}

export interface ActiveAbility {
  id: number;
  name: string;
  optional: boolean;
}

export interface FigureView {
  uid: number;
  name: string;
  short_name: string;
  owner: Owner;
  faction: string;
  points: number;

  pos: [number, number];
  facing_deg: number;

  base_radius: number;
  arc_deg: number; // front-arc HALF-angle in degrees (wedge spans facing ± arc_deg)

  range: number;
  targets: number;
  is_ranged: boolean;

  current_click: number;
  starting_click: number;
  num_live_clicks: number;
  health_fraction: number;

  eliminated: boolean;
  demoralized: boolean;
  captured: boolean;

  action_tokens: number;
  acted: boolean;
  can_act: boolean;

  // current-click convenience stats
  speed: number;
  attack: number;
  defense: number;
  damage: number;

  active_abilities: ActiveAbility[];
  optional_abilities?: { id: number; name: string; disabled: boolean }[];
  in_base_contact_with: number[];
  dial: DialClick[];
}

export interface AttackExplain {
  attack: number;
  rear: boolean;
  defense: { base: number; battle_armor: number; defend: number; effective: number };
  damage: { base: number; enhancement: number; toughness: number; per_hit: number };
  hit_odds: number;
  expected_clicks: number;
}

export interface GameMeta {
  turn: number;
  active_player: Owner;
  first_player: string;
  actions_per_turn: number;
  actions_remaining: number;
  ended: boolean;
  winner: string | null;
  victory_points: { human: number; llm: number };
  board: { width: number; height: number };
  ability_coverage?: unknown;
}

export interface GameView {
  meta: GameMeta;
  figures: FigureView[]; // ALL figures, including eliminated
}

export interface Candidate {
  kind: string;
  label: string;
  annotation: Record<string, unknown>;
  intent: unknown;
}

export interface GameEvent {
  type: string;
  [k: string]: unknown;
}

export interface ApplyResult {
  ok: boolean;
  reason?: string;
  detail?: string;
  events: GameEvent[];
  summary: string;
  view: GameView;
}

export interface ValidateMoveResult {
  ok: boolean;
  reason?: string;
  detail?: string;
  break_away?: { needed: boolean; odds: number };
}

export interface OpponentTurnResult {
  decisions: unknown[];
  view: GameView;
}

// ---------------------------------------------------------------------------
// Client
// ---------------------------------------------------------------------------

import { MOCK_VIEW } from "./mock";

function clone<T>(value: T): T {
  return JSON.parse(JSON.stringify(value)) as T;
}

async function req<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    ...init,
  });
  if (!res.ok) {
    throw new Error(`${init?.method ?? "GET"} ${path} -> ${res.status}`);
  }
  return (await res.json()) as T;
}

// GET /api/state
export async function getState(): Promise<GameView> {
  if (USE_MOCK) return clone(MOCK_VIEW);
  return req<GameView>("/api/state");
}

// POST /api/new_game
export async function newGame(points: number, seed: number): Promise<GameView> {
  if (USE_MOCK) return clone(MOCK_VIEW);
  return req<GameView>("/api/new_game", {
    method: "POST",
    body: JSON.stringify({ points, seed }),
  });
}

// GET /api/candidates/{uid}
export async function getCandidates(uid: number): Promise<Candidate[]> {
  if (USE_MOCK) return [];
  return req<Candidate[]>(`/api/candidates/${uid}`);
}

// GET /api/formation_candidates — turn-level movement/combat formations.
export async function getFormationCandidates(): Promise<Candidate[]> {
  if (USE_MOCK) return [];
  return req<Candidate[]>("/api/formation_candidates");
}

// POST /api/explain — modifier breakdown for a prospective attack.
export async function explainAttack(
  attackerUid: number,
  targetUid: number,
  attackType: "close" | "ranged",
  rear = false,
): Promise<AttackExplain> {
  if (USE_MOCK) {
    return {
      attack: 0,
      rear,
      defense: { base: 0, battle_armor: 0, defend: 0, effective: 0 },
      damage: { base: 0, enhancement: 0, toughness: 0, per_hit: 0 },
      hit_odds: 0,
      expected_clicks: 0,
    };
  }
  return req<AttackExplain>("/api/explain", {
    method: "POST",
    body: JSON.stringify({ attacker_uid: attackerUid, target_uid: targetUid, attack_type: attackType, rear }),
  });
}

// --- new-game construction stream (Server-Sent Events) ---------------------
export interface ConstructFigure {
  id: number;
  name: string;
  faction: string;
  points: number;
  role: string;
  rank: string;
  abilities: string[];
}
export type ConstructEvent =
  | { type: "start"; mode: string; budget: number }
  | { type: "pool"; side: "human" | "llm"; pool: ConstructFigure[] }
  | { type: "human_army"; army: ConstructFigure[]; points: number }
  | { type: "llm_start"; available: boolean }
  | { type: "llm_pick"; figure: ConstructFigure; reasoning: string; used_llm: boolean; remaining: number; army: ConstructFigure[]; points: number }
  | { type: "llm_stop"; reasoning: string; used_llm: boolean }
  | { type: "llm_army"; army: ConstructFigure[]; points: number }
  | { type: "ready"; view: GameView }
  | { type: "error"; message: string };

export function newGameStreamUrl(
  mode: "preconstructed" | "sealed",
  points: number,
  opponent: "llm" | "heuristic",
  seed: number,
  humanIds?: number[],
): string {
  const p = new URLSearchParams({ mode, points: String(points), opponent, seed: String(seed) });
  if (humanIds && humanIds.length) p.set("human_ids", humanIds.join(","));
  return `/api/new_game_stream?${p.toString()}`;
}

// GET /api/roster — full drafting roster (preconstructed).
export async function getRoster(): Promise<ConstructFigure[]> {
  if (USE_MOCK) return [];
  return (await req<{ figures: ConstructFigure[] }>("/api/roster")).figures;
}

// GET /api/sealed_packs — the human's four booster packs to open.
export async function getSealedPacks(seed: number): Promise<ConstructFigure[][]> {
  if (USE_MOCK) return [];
  return (await req<{ packs: ConstructFigure[][] }>(`/api/sealed_packs?seed=${seed}`)).packs;
}

// Toggle an optional ability off/on (P4-R34) — routes through /api/intent.
export async function toggleAbility(
  figureUid: number,
  abilityId: number,
  off: boolean,
): Promise<ApplyResult> {
  return applyIntent({ kind: "toggle_ability", figure_uid: figureUid, ability_id: abilityId, off });
}

// POST /api/intent
export async function applyIntent(intent: unknown): Promise<ApplyResult> {
  if (USE_MOCK) {
    return {
      ok: false,
      reason: "mock",
      detail: "USE_MOCK is enabled; intents are not applied.",
      events: [],
      summary: "No-op (mock mode).",
      view: clone(MOCK_VIEW),
    };
  }
  return req<ApplyResult>("/api/intent", {
    method: "POST",
    body: JSON.stringify(intent),
  });
}

// POST /api/validate_move
export async function validateMove(
  uid: number,
  dest: [number, number],
  facing: number,
): Promise<ValidateMoveResult> {
  if (USE_MOCK) return { ok: true };
  return req<ValidateMoveResult>("/api/validate_move", {
    method: "POST",
    body: JSON.stringify({ figure_uid: uid, dest, facing }),
  });
}

// POST /api/end_turn — end the human turn (advances to the opponent).
export async function endTurn(): Promise<GameView> {
  if (USE_MOCK) return clone(MOCK_VIEW);
  return req<GameView>("/api/end_turn", { method: "POST" });
}

// POST /api/opponent_turn
export async function opponentTurn(): Promise<OpponentTurnResult> {
  if (USE_MOCK) return { decisions: [], view: clone(MOCK_VIEW) };
  return req<OpponentTurnResult>("/api/opponent_turn", { method: "POST" });
}

// SSE: the opponent's turn action-by-action.
export type OpponentStreamEvent =
  | { type: "action"; summary: string; reasoning: string; fallback: boolean; events: GameEvent[]; view: GameView }
  | { type: "done"; view: GameView }
  | { type: "error"; message: string; view?: GameView };
export function opponentTurnStreamUrl(): string {
  return "/api/opponent_turn_stream";
}

// POST /api/chat — talk to the opponent.
export interface ChatMsg {
  role: "user" | "assistant";
  content: string;
}
export async function chat(message: string, history: ChatMsg[]): Promise<string> {
  if (USE_MOCK) return "(mock) Nice move!";
  const r = await req<{ reply: string }>("/api/chat", {
    method: "POST",
    body: JSON.stringify({ message, history }),
  });
  return r.reply;
}
