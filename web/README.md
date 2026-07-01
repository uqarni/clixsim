# Clix Engine — web client

Browser client shell + board renderer for Clix Engine (a digital Mage Knight
game, human vs LLM). The Python engine is the single source of truth and serves
a JSON/HTTP API; this app renders a single always-on in-battle screen against
that contract. It ships with mock data so it runs standalone.

## Stack

- Vite + React + TypeScript.
- Runtime dependencies: `react`, `react-dom` only. No UI kit, no state library.
- The board is a plain HTML5 `<canvas>` 2D context (devicePixelRatio-aware).
  Everything else is React + CSS.

## Run

```bash
cd web
npm install
npm run dev      # http://localhost:5173
```

Other scripts:

```bash
npm run build      # typecheck (tsc --noEmit) then vite build
npm run typecheck  # tsc --noEmit only
npm run preview    # serve the production build
```

## Mock vs. real server

The app runs on mock data by default. To point it at the real Python engine:

1. Open `src/api.ts` and set `USE_MOCK = false`.
2. Start the Python server on `http://localhost:8000`.
3. Run `npm run dev`. The Vite dev server proxies `/api` to that target
   (see `vite.config.ts`), so no other code change is needed.

Proxy target: `http://localhost:8000` (change it in `vite.config.ts` if the
engine listens elsewhere).

## API contract

Types and the client live in `src/api.ts`. Endpoints:

| Function                                  | Method | Path                     |
| ----------------------------------------- | ------ | ------------------------ |
| `getState()`                              | GET    | `/api/state`             |
| `newGame(points, seed)`                   | POST   | `/api/new_game`          |
| `getCandidates(uid)`                      | GET    | `/api/candidates/{uid}`  |
| `applyIntent(intent)`                     | POST   | `/api/intent`            |
| `validateMove(uid, dest, facing)`         | POST   | `/api/validate_move`     |
| `opponentTurn()`                          | POST   | `/api/opponent_turn`     |

While `USE_MOCK` is true these return the bundled mock (`src/mock.ts`) or a
no-op, so nothing hits the network.

## Layout (ultrawide, five zones + top HUD)

A thin top HUD spans the full width (turn, active player, action pips, VP,
end-turn). Below it, a CSS grid of five zones fills the viewport height:

- Zone A (~13%) — Force rail: your living figures as compact cards. Click to select.
- Zone B (~15%) — Dial inspector: full combat dial + stat block + abilities for the selection.
- Zone C (~40%) — Board: the canvas (the only zone that scales).
- Zone D (~15%) — Opponent: the LLM's living figures + a reasoning-stream placeholder.
- Zone E (~17%) — Log + ledger: scrolling event log (upper) and VP / casualty ledger (lower).

Selection state is lifted to `App`, so clicking a figure on the board or in any
rail keeps every panel in sync.

## Board renderer notes

- World → screen transform: the board is `meta.board.{width,height}` inches, fit
  into the canvas with a felt margin, preserving aspect (continuous space, no grid).
- devicePixelRatio-aware (backing store sized `css * dpr`) for crisp hi-DPI output.
- Per figure: owner-colored base circle at `pos` (radius = `base_radius` in),
  a translucent front-arc wedge spanning `facing_deg ± arc_deg`, a health ring
  from `health_fraction`, push-token pips from `action_tokens`, and a `short_name`
  label. Eliminated figures are omitted from the board (ledger only).
- Selected figure: highlight ring plus a dashed range ring at `range` inches, or a
  reach ring at `speed` inches when `range` is 0.
- Hovering an enemy while a friendly is selected draws a line of fire between them.

## File tree

```
web/
├── index.html
├── package.json
├── tsconfig.json
├── vite.config.ts
├── README.md
└── src/
    ├── api.ts
    ├── mock.ts
    ├── main.tsx
    ├── index.css
    ├── App.tsx
    └── components/
        ├── BoardCanvas.tsx
        ├── DialInspector.tsx
        ├── FigCard.tsx
        ├── ForceRail.tsx
        ├── LogLedger.tsx
        ├── OpponentPanel.tsx
        └── TurnHud.tsx
```
