# data/teams.csv — canonical team registry

Build-order item **1** from `PLAN.md` §1: the `teams` table, shipped as a flat CSV so it
can live in the repo and be reviewed/edited by hand. When the SQLite layer lands, this
file is the seed the `teams` table loads from — the CSV stays the source of truth.

## Coverage (275 teams)

| sport | teams | notes |
|---|---|---|
| NFL | 32 | 2026 season venues (new Highmark Stadium, Northwest Stadium, etc.) |
| NBA | 30 | |
| MLB | 30 | 2026 homes: Athletics at Sutter Health Park (Sacramento), Rays back at Tropicana Field |
| NHL | 32 | Utah Mammoth at Delta Center |
| WNBA | 15 | 2026 season incl. expansion Golden State Valkyries, Portland Fire, Toronto Tempo |
| CFB | 136 | 2026 FBS membership & conference alignment (Pac-12 rebuild, UTEP/NIU → MW, Texas State → Pac-12, NIU out of MAC, UMass in MAC) |

## Columns

| Column | Meaning |
|---|---|
| `team_id` | Stable primary key, `<sport>-<slug>` (e.g. `nfl-buf`, `cfb-ohio-state`). Never reuse or rename — everything else will foreign-key to this. |
| `sport` | `NFL`, `NBA`, `MLB`, `NHL`, `WNBA`, `CFB` (future: `CBB`, `WCBB`) |
| `name` | Full display name |
| `abbrev` | Common short code, unique within a sport. NHL abbrevs are the NHL API tri-codes. |
| `conference` / `division` | 2026 alignment. Division blank for CFB/WNBA where not applicable. |
| `external_ids` | JSON object mapping source → native id: `mlb` (MLB Stats API teamId), `nba` (NBA Stats API teamId), `nhl` (NHL API teamId), `espn` (ESPN teamId, NFL only so far). Empty `{}` where not yet mapped (WNBA, CFB) — adapters must treat an unmapped team as a loud error, per PLAN §1. |
| `venue_name` | Primary home venue, 2026 |
| `venue_lat` / `venue_lon` | Venue coordinates (decimal degrees). Pro venues ≈ ±0.01°; college venues are stadium/campus approximations, good enough for travel-distance features, not for navigation. |
| `timezone` | IANA zone of the venue (`America/New_York`, `America/Indiana/Indianapolis`, `America/Phoenix`, …) — this is what rest/travel features and local start times key off. |

## Validation

```
python3 scripts/validate_teams.py
```

Checks header shape, `team_id` uniqueness/format, per-sport abbrev uniqueness,
lat/lon sanity bounds, IANA timezone validity, `external_ids` JSON, and exact
per-league team counts. Run it after any hand edit; wire it into CI when a workflow
exists.

## Known gaps / follow-ups

- **College basketball (CBB) and women's college basketball (WCBB) are not included
  yet.** That's ~360 teams each; curating them by hand isn't reliable. The intended
  source is ESPN's public team APIs
  (`site.api.espn.com/apis/site/v2/sports/basketball/mens-college-basketball/teams?limit=500`,
  same for `womens-college-basketball`), which this sandbox's egress policy blocks.
  When run from a machine with normal internet access, a fetch script can append those
  rows in this same schema (arena coords via the venue data or campus geocode) and add
  `CBB`/`WCBB` counts to the validator.
- WNBA and CFB `external_ids` are empty — fill with ESPN ids from the same endpoints.
- Venue moves to watch: Jacksonville (EverBank renovation), Tennessee (new Nissan
  Stadium 2027), Cleveland Browns (Brook Park), Athletics (Las Vegas), Rays (Tropicana
  status year to year). Update the row when a move takes effect; if historical accuracy
  ever matters for backtests, venue history becomes its own table (`venue_from`/`venue_to`).
- Conference realignment changes annually for CFB — the validator's counts will catch
  membership drift at the next edit.
