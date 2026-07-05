# Prompt for Claude Code — build the "My Model" Researcher Console

Use this prompt together with `researcher-console-design.html` (in the repo root). That file is a
self-contained, interactive design reference — open it in a browser and click through it. Your job
is to rebuild it as a real application, matching the design pixel-for-pixel and behavior-for-behavior.

---

## The prompt

Build a web app called the **Researcher Console** for a single researcher who tunes a sports
betting prediction model called "My Model." The complete visual and interaction design is in
`researcher-console-design.html` — a self-contained HTML file with realistic fake data. Treat it as
the source of truth for layout, spacing, colors, typography, copy, and interaction behavior.
Everything below describes what that file contains and how it works.

### Domain context

My Model predicts final scores for MLB, NBA, and NHL games by blending five statistical methods
(season averages, common opponents, recent form, home adjustment, head-to-head), converts the
predicted margin into a win probability via a steepness constant `k`, and applies gates to decide
whether to publish a pick. Picks are Moneyline (STRONG/LEAN confidence) or Total (OVER/UNDER);
below the gates the model passes (TOSS-UP). Bets are graded WIN/LOSS/PUSH at flat $100 stakes.
Every constant is tunable **per sport**. Factory defaults (MLB / NBA / NHL):

- Weights: season 0.30, common opponents 0.35, recent form 0.20, home 0.10, head-to-head 0.05 (same all sports; must sum to 1.000)
- Home advantage: 0.35 runs / 3.0 pts / 0.25 goals
- Form multiplier base/range: 0.80/0.40 · 0.85/0.30 · 0.80/0.40
- Win-prob steepness k: 0.40 / 0.15 / 0.70
- Moneyline gates STRONG/LEAN: 0.60/0.55 (all sports)
- Totals thresholds OVER/UNDER: 9.2/7.8 · 225/215 · 6.3/5.7

### Visual system (match the design file exactly)

Dark trading-terminal aesthetic. Fonts: **IBM Plex Sans** for UI, **IBM Plex Mono** for all
numbers, config names, and table figures. Palette: page `#0B0E13`, chrome `#0D1118`, panels
`#12161F` with `#232A38` borders, hairlines `#1B212D`, text `#DDE3EE`, secondary `#C4CBDA`, muted
`#7D879C`, faint `#59637A`. One accent: blue `#4C8DFF` (interactive elements, candidate lines,
primary buttons). Green `#3FB77E` / red `#E5606B` are reserved for P&L, ROI, and win/loss. Amber
`#E3B341` marks dirty/warning states. Base font 13px; uppercase 10px letter-spaced panel labels;
information-dense 12px tables.

### App shell

- Left nav (200px): "MY MODEL / researcher console" wordmark, the four screens with keyboard
  shortcuts **1–4** (global hotkeys, ignored while typing in inputs), and a "Live configs" summary
  at the bottom.
- Header (52px): current screen title + subtitle, and always-visible LIVE config chips per sport
  (green dot · sport · config name). These update whenever a config is promoted.
- Toast notifications appear bottom-center for actions (save, load, promote, reset).

### Screen 1 — Parameters (the editor)

- Sport tabs MLB/NBA/NHL; header line shows which saved config is loaded (or "unsaved draft") and
  an amber "N modified" chip counting edited fields across all sports.
- **Method weights panel**: five rows, each with slider (0–1, step .01) + numeric input + factory
  default beside it + amber dirty dot + per-field reset button (↺, only visible when ≠ default).
  A live **Σ sum indicator** at top: green when 1.000, amber with an inline error banner and a
  one-click **renormalize** button when not (renormalize scales all weights proportionally to sum
  exactly 1.000). A thin stacked proportion bar visualizes the blend.
- Three more panels: Score adjustments (home advantage with per-sport units, form multiplier
  base/range), Win probability (k), Pick gates (ML STRONG/LEAN gates, totals OVER/UNDER
  thresholds). Same default/dirty/reset treatment; steps and decimal precision vary per sport.
- "Reset all to defaults" resets every sport to factory.
- **Persistent footer bar**: name input + "Save as new config", "Save over '<name>'" (disabled
  with an "immutable — has been live" note when the loaded config is locked), "Discard changes"
  (reverts to loaded config), and primary **"Run backtest with these values →"** which jumps to
  the Backtest screen and immediately starts a run sourced from the editor draft.

### Screen 2 — Backtest

One screen, four states in the main column, plus a persistent right rail.

- **Setup**: parameter source (Editor draft — labeled with loaded config + edit count — or a
  Saved config picked from a dropdown), date range presets (Last 30 / Last 90 / Season to date)
  plus editable from/to fields, league checkboxes, bet-type checkboxes, an "≈ N games in range"
  estimate, and Run (disabled unless ≥1 league and ≥1 bet type).
- **Running**: pulsing indicator, overall "N / M games" counter and progress bar, per-league
  progress bars, Cancel. Simulated progress is fine in a first pass.
- **Results**: five headline tiles — Record W–L–P, ROI, Total P&L, Picks made, Pass rate — each
  showing the candidate value, the **factory-default baseline over the same slice** underneath,
  and a delta chip (green/red for record/ROI/P&L; neutral gray for picks/pass rate). Below: a
  cumulative P&L SVG line chart (solid blue candidate vs dashed gray baseline, $0 gridline,
  end-value labels), three breakdown tables (by league, by bet type, by confidence tier — each
  REC/ROI/P&L), and a pick log (date, league, game, pick, line, WIN/LOSS/PUSH, P&L) collapsed to
  7 rows with a "show all" toggle.
- **Error**: a failed run shows a red-bordered panel with the failure reason (e.g. "Odds feed
  missing for 14 NHL games…") and Edit setup / Retry actions.
- **History rail** (264px, right): every past run as a card — config name, range, run date, ROI
  (red "FAILED" for failed runs) — plus "+ new". Clicking selects (blue border); selecting **two**
  runs switches the main column to a metric-by-metric **compare view** with the better value
  highlighted per row.

### Screen 3 — Configs

- Table: name (with a LOCK tag on any config that has ever been live — immutable, archivable,
  never deletable), note, created date, status badges (green `LIVE · MLB` per sport, `retired`,
  `draft`, `archived` dims the row), best backtest ROI, live record, and row actions: **load**
  (into editor, navigates to Parameters), **duplicate**, **compare** (toggle; two selected opens
  compare), **set live**, **archive**.
- **Compare view**: both configs' names with best-BT ROI and live record at top, then
  field-by-field rows showing **differences only** (sport, field, value A, value B, highlighted).
- **Set-live flow**: modal with sport selector chips, a summary of what it replaces ("Replaces
  conservative-gates — live record 40–45, live since Jun 11…"), the full field diff vs the
  currently live config for that sport (current live vs candidate, candidate values in amber), a
  note that promoting locks the config, and Cancel / "Set live for <sport>". Confirming updates
  the live map everywhere (header chips, nav summary, Live screen) and prepends a promotion-history
  entry.
- **Promotion history** log below the table: date, sport, "new ← old (replaced at record)", with a
  **roll back** action on the latest entry per sport that reopens the same modal in rollback mode.

### Screen 4 — Live

- Three per-sport cards: live config name + "live since" date, and this morning's picks (game,
  time, pick, STRONG/LEAN/TOTAL/TOSS-UP chip, line, pending status). Out-of-season sports show an
  off-season note instead (the fake data is dated July 5, 2026, so NBA/NHL are off-season).
- **Centerpiece**: "Live vs backtest expectation" — cumulative P&L since promotion (solid green)
  charted against the config's backtest curve over the same horizon (dashed gray), with sport tabs.
  Answers "is it holding up out of sample?"
- Right column: Record / ROI / P&L / Picks tiles since promotion, a by-bet-type table, and a
  "Yesterday" grading summary strip. Full-width recent graded picks table below.

### Empty states

Design them for: no configs yet (Configs screen card with a CTA to the editor), no backtests yet
(rail placeholder + setup form), and first day live (banner, dash tiles, "no graded picks yet"
chart placeholder). The design file exposes these via its Tweaks/props (`emptyStates` flag).

### Implementation notes

- Ship it with the same realistic fake data as the design file (config names like
  `aggressive-totals-v2`, records like 34–28, ROI like −3.2%/+6.8%, real team matchups) behind a
  data layer you can later swap for a real backend.
- Charts are lightweight inline SVG in the design — no chart library needed.
- State to persist (localStorage is fine initially): saved configs, editor draft, run history,
  live map, promotion history.
- Your stack choice, but keep it simple: a single-page app, client-side state, no backend required
  for v1.

Recreate it faithfully — when in doubt about any spacing, color, label, or behavior, open
`researcher-console-design.html` and copy what it does.
