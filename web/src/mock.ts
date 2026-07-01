// ---------------------------------------------------------------------------
// Realistic mock GameView used while USE_MOCK is true (see api.ts).
//
// 6 figures on a 36x36 inch board: 3 human, 3 llm. Varied positions, clicks,
// and health; a couple carry abilities; one llm figure is eliminated (it is
// shown only in the ledger, never on the board).
// ---------------------------------------------------------------------------

import type { DialClick, FigureView, GameView } from "./api";

// Build a plausible descending dial: stats generally erode across clicks.
function makeDial(
  entries: Array<{
    speed: number;
    attack: number;
    defense: number;
    damage: number;
    abilities?: DialClick["abilities"];
  }>,
): DialClick[] {
  return entries.map((e, i) => ({
    index: i,
    speed: e.speed,
    attack: e.attack,
    defense: e.defense,
    damage: e.damage,
    abilities: e.abilities ?? [],
  }));
}

const chargeDial = makeDial([
  { speed: 10, attack: 10, defense: 18, damage: 3, abilities: [{ id: 1, name: "Charge", slot: "speed", optional: false }] },
  { speed: 10, attack: 10, defense: 18, damage: 3, abilities: [{ id: 1, name: "Charge", slot: "speed", optional: false }] },
  { speed: 9, attack: 9, defense: 17, damage: 2 },
  { speed: 9, attack: 9, defense: 17, damage: 2, abilities: [{ id: 2, name: "Toughness", slot: "defense", optional: false }] },
  { speed: 8, attack: 8, defense: 16, damage: 2, abilities: [{ id: 2, name: "Toughness", slot: "defense", optional: false }] },
  { speed: 8, attack: 8, defense: 16, damage: 1 },
  { speed: 7, attack: 7, defense: 15, damage: 1 },
  { speed: 6, attack: 6, defense: 15, damage: 1 },
]);

const rangedDial = makeDial([
  { speed: 7, attack: 11, defense: 17, damage: 3, abilities: [{ id: 3, name: "Running Shot", slot: "speed", optional: false }] },
  { speed: 7, attack: 11, defense: 17, damage: 3, abilities: [{ id: 3, name: "Running Shot", slot: "speed", optional: false }] },
  { speed: 6, attack: 10, defense: 16, damage: 2, abilities: [{ id: 4, name: "Ranged Combat Expert", slot: "attack", optional: true }] },
  { speed: 6, attack: 10, defense: 16, damage: 2, abilities: [{ id: 4, name: "Ranged Combat Expert", slot: "attack", optional: true }] },
  { speed: 5, attack: 9, defense: 15, damage: 2 },
  { speed: 5, attack: 9, defense: 15, damage: 1 },
  { speed: 4, attack: 8, defense: 14, damage: 1 },
]);

const scoutDial = makeDial([
  { speed: 12, attack: 8, defense: 16, damage: 2, abilities: [{ id: 5, name: "Stealth", slot: "defense", optional: false }] },
  { speed: 11, attack: 8, defense: 16, damage: 2 },
  { speed: 11, attack: 7, defense: 15, damage: 2 },
  { speed: 10, attack: 7, defense: 15, damage: 1 },
  { speed: 9, attack: 6, defense: 14, damage: 1 },
  { speed: 8, attack: 6, defense: 14, damage: 1 },
]);

const bruteDial = makeDial([
  { speed: 8, attack: 11, defense: 18, damage: 4, abilities: [{ id: 6, name: "Battle Fury", slot: "damage", optional: false }] },
  { speed: 8, attack: 11, defense: 18, damage: 4 },
  { speed: 7, attack: 10, defense: 18, damage: 3, abilities: [{ id: 2, name: "Toughness", slot: "defense", optional: false }] },
  { speed: 7, attack: 10, defense: 17, damage: 3, abilities: [{ id: 2, name: "Toughness", slot: "defense", optional: false }] },
  { speed: 6, attack: 9, defense: 17, damage: 2 },
  { speed: 6, attack: 9, defense: 16, damage: 2 },
  { speed: 5, attack: 8, defense: 16, damage: 1 },
  { speed: 5, attack: 7, defense: 15, damage: 1 },
]);

const casterDial = makeDial([
  { speed: 6, attack: 10, defense: 16, damage: 2, abilities: [{ id: 7, name: "Probability Control", slot: "utility", optional: true }] },
  { speed: 6, attack: 10, defense: 16, damage: 2 },
  { speed: 5, attack: 9, defense: 15, damage: 2, abilities: [{ id: 8, name: "Outwit", slot: "utility", optional: true }] },
  { speed: 5, attack: 9, defense: 15, damage: 1 },
  { speed: 4, attack: 8, defense: 14, damage: 1 },
]);

const gruntDial = makeDial([
  { speed: 8, attack: 9, defense: 16, damage: 2 },
  { speed: 7, attack: 8, defense: 15, damage: 2 },
  { speed: 7, attack: 8, defense: 15, damage: 1 },
  { speed: 6, attack: 7, defense: 14, damage: 1 },
]);

function statsAt(dial: DialClick[], click: number) {
  const row = dial[Math.min(click, dial.length - 1)];
  return { speed: row.speed, attack: row.attack, defense: row.defense, damage: row.damage };
}

function figure(f: Omit<FigureView, "speed" | "attack" | "defense" | "damage" | "num_live_clicks" | "health_fraction" | "active_abilities"> & {
  active_abilities?: FigureView["active_abilities"];
}): FigureView {
  const numLive = f.dial.length - f.starting_click;
  const remaining = Math.max(0, f.dial.length - f.current_click);
  const denom = Math.max(1, f.dial.length - f.starting_click);
  const s = statsAt(f.dial, f.current_click);
  const activeRow = f.dial[Math.min(f.current_click, f.dial.length - 1)];
  return {
    ...f,
    ...s,
    num_live_clicks: numLive,
    health_fraction: f.eliminated ? 0 : Math.max(0, Math.min(1, remaining / denom)),
    active_abilities:
      f.active_abilities ??
      activeRow.abilities.map((a) => ({ id: a.id, name: a.name, optional: a.optional })),
  };
}

export const MOCK_VIEW: GameView = {
  meta: {
    turn: 4,
    active_player: "human",
    first_player: "human",
    actions_per_turn: 2,
    actions_remaining: 1,
    ended: false,
    winner: null,
    victory_points: { human: 35, llm: 21 },
    board: { width: 36, height: 36 },
  },
  figures: [
    // ------------------------------- HUMAN -------------------------------
    figure({
      uid: 101,
      name: "Silver Sentinel",
      short_name: "Sentinel",
      owner: "human",
      faction: "Order",
      points: 120,
      pos: [10.5, 12.0],
      facing_deg: 45,
      base_radius: 0.6,
      arc_deg: 45,
      range: 0,
      targets: 1,
      is_ranged: false,
      current_click: 0,
      starting_click: 0,
      eliminated: false,
      demoralized: false,
      captured: false,
      action_tokens: 0,
      acted: false,
      can_act: true,
      in_base_contact_with: [],
      dial: chargeDial,
    }),
    figure({
      uid: 102,
      name: "Longbow Ranger",
      short_name: "Ranger",
      owner: "human",
      faction: "Order",
      points: 95,
      pos: [7.0, 22.5],
      facing_deg: 300,
      base_radius: 0.5,
      arc_deg: 30,
      range: 8,
      targets: 1,
      is_ranged: true,
      current_click: 2,
      starting_click: 0,
      eliminated: false,
      demoralized: false,
      captured: false,
      action_tokens: 1,
      acted: true,
      can_act: true,
      in_base_contact_with: [],
      dial: rangedDial,
    }),
    figure({
      uid: 103,
      name: "Shadow Scout",
      short_name: "Scout",
      owner: "human",
      faction: "Order",
      points: 60,
      pos: [15.0, 8.0],
      facing_deg: 90,
      base_radius: 0.45,
      arc_deg: 60,
      range: 0,
      targets: 1,
      is_ranged: false,
      current_click: 3,
      starting_click: 0,
      eliminated: false,
      demoralized: true,
      captured: false,
      action_tokens: 2,
      acted: true,
      can_act: false,
      in_base_contact_with: [],
      dial: scoutDial,
    }),
    // -------------------------------- LLM --------------------------------
    figure({
      uid: 201,
      name: "Iron Warlord",
      short_name: "Warlord",
      owner: "llm",
      faction: "Chaos",
      points: 150,
      pos: [26.0, 24.0],
      facing_deg: 210,
      base_radius: 0.7,
      arc_deg: 45,
      range: 0,
      targets: 1,
      is_ranged: false,
      current_click: 1,
      starting_click: 0,
      eliminated: false,
      demoralized: false,
      captured: false,
      action_tokens: 0,
      acted: false,
      can_act: true,
      in_base_contact_with: [],
      dial: bruteDial,
    }),
    figure({
      uid: 202,
      name: "Void Sorceress",
      short_name: "Sorceress",
      owner: "llm",
      faction: "Chaos",
      points: 110,
      pos: [29.5, 14.0],
      facing_deg: 160,
      base_radius: 0.5,
      arc_deg: 30,
      range: 6,
      targets: 2,
      is_ranged: true,
      current_click: 2,
      starting_click: 0,
      eliminated: false,
      demoralized: false,
      captured: false,
      action_tokens: 1,
      acted: false,
      can_act: true,
      in_base_contact_with: [],
      dial: casterDial,
    }),
    figure({
      uid: 203,
      name: "Chaos Grunt",
      short_name: "Grunt",
      owner: "llm",
      faction: "Chaos",
      points: 40,
      pos: [20.0, 18.0],
      facing_deg: 0,
      base_radius: 0.45,
      arc_deg: 60,
      range: 0,
      targets: 1,
      is_ranged: false,
      current_click: 4,
      starting_click: 0,
      eliminated: true,
      demoralized: false,
      captured: false,
      action_tokens: 0,
      acted: true,
      can_act: false,
      in_base_contact_with: [],
      dial: gruntDial,
    }),
  ],
};
