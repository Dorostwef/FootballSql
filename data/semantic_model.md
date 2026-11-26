# Football DB — Semantic Model

**Iran Persian Gulf Pro League, season 2026-27 (1405-06)**
Source: varzesh3.com public API, snapshot taken 2026-11-01 (after matchweek 11).
Engine: SQLite. Open it **read-only** (`file:football.db?mode=ro`).

| | |
|---|---|
| Teams | 18 |
| Matches | 306 (34 matchweeks × 9) — 95 finished, 1 awarded, 3 cancelled, 207 scheduled |
| Players | 531 (names in Persian) |
| Lineup rows | 3,945 (starters + bench) |
| Goals / Cards / Subs / Penalties / VAR | 187 / 327 / 855 / 21 / 39 |

---

## Football language → tables

| Fan / analyst says… | Table · column |
|---|---|
| competition, league | `competition` |
| season | `season` (`name` = '2026-27', `name_jalali` = '1405-06') |
| club, team, side | `team` (`name` English, `name_fa` Persian, `city`) |
| fixture, game, match, result, score | `match` |
| matchweek, round, هفته | `match.round` (1–34) |
| home / away, host / guest, میزبان / میهمان | `match.home_team_id` / `match.away_team_id` |
| final score | `match.home_goals`, `match.away_goals` |
| win / draw / loss | `match.result` ('H','D','A') or `v_team_match.outcome` ('W','D','L') |
| referee, داور | `referee` via `match.referee_id` |
| stadium, venue, ورزشگاه | `stadium` via `match.stadium_id` |
| formation, آرایش | `match.home_formation` / `match.away_formation` |
| manager, head coach, سرمربی | `coach` via `match_coach` (per match, per team) |
| lineup, starting XI, bench, ترکیب | `lineup` (`is_starter` 1/0) |
| position | `lineup.position` ('GK','DF','MF','FW'), starters only |
| goal, scorer, گل | `goal` |
| own goal, گل به خودی | `goal.is_own_goal = 1` |
| penalty, پنالتی | `penalty` (all attempts); converted ones also in `goal` with `is_penalty = 1` |
| yellow / red card, sending-off | `card.card_type` ('yellow','second_yellow','red') |
| substitution, تعویض | `substitution` (`player_in_id`, `player_out_id`) |
| VAR | `var_decision` |
| league table, standings, جدول | view `v_standings` |
| minutes played | view `v_player_minutes` (approximate) |

## Tables

### `match`
| column | meaning |
|---|---|
| `match_id` | PK |
| `round` | matchweek number |
| `status` | `finished`, `awarded` (forfeit, 3-0 by disciplinary decision), `cancelled`, `scheduled` |
| `kickoff_utc` | ISO UTC; NULL for unscheduled fixtures |
| `match_date`, `kickoff_local` | Tehran local date/time (UTC+03:30) |
| `match_date_jalali` | Persian calendar date as published, e.g. `1405/06/11` |
| `home_team_id`, `away_team_id` | FK → `team` |
| `home_goals`, `away_goals`, `result` | only set when `status IN ('finished','awarded')` |
| `referee_id`, `stadium_id` | FK; may be NULL for future fixtures |
| `home_formation`, `away_formation` | e.g. `4-2-3-1`; finished matches only |

### `lineup` — PK (`match_id`, `player_id`)
`team_id`, `shirt_number`, `is_starter`, `formation_line` (0 = GK … highest = attack), `position` (derived: line 0 GK, line 1 DF, highest line FW, rest MF; NULL on bench).

### `goal`
| column | meaning |
|---|---|
| `team_id` | team **credited** with the goal (the one whose score went up) |
| `player_id` | scorer |
| `scorer_team_id` | team the scorer plays for — **differs from `team_id` for own goals** |
| `minute`, `added_time` | `90+6` → `minute = 90, added_time = 6` |
| `half` | `first_half` / `second_half` |
| `is_own_goal`, `is_penalty` | 0/1 |

Invariant: for every finished match, `COUNT(goal)` per `team_id` equals `home_goals` / `away_goals`.

### `card`, `substitution`, `penalty`, `var_decision`
All have `match_id`, `team_id`, `minute`, `added_time`, `half` (`card.half` can also be `after_full_time`).
- `penalty.outcome`: `scored`, `saved`, `missed`
- `var_decision.decision`: `goal_disallowed`, `penalty_awarded`, `penalty_cancelled`, `card_changed`. `team_id` is the side the event was recorded under; `player_id` may belong to the other team.

### `match_coach` — PK (`match_id`, `team_id`)
Coach on the touchline for that team in that match. Several teams changed coach mid-season.

### Lookup tables
`competition`, `season`, `team`, `player`, `coach`, `referee`, `stadium` — id + name.

## Views
| view | grain | notes |
|---|---|---|
| `v_team_match` | one row per team per counted match | `venue` home/away, `goals_for`, `goals_against`, `outcome` W/D/L. Includes the awarded match. |
| `v_standings` | one row per team | played, won, drawn, lost, GF, GA, GD, points. Order with `points DESC, goal_diff DESC, goals_for DESC`. |
| `v_match` | one row per match | team and referee names already joined |
| `v_player_minutes` | player × finished match | regulation minutes only; stoppage time ignored |

## Join rules (the usual mistakes)
1. A team appears in a match as **either** home or away. Use `v_team_match`, or `OR`, or `UNION ALL` — never only `home_team_id`.
2. **Own goals & team goals vs. scorer club:** `goal.team_id` is the **scoring/credited** team (whose scoreboard increased); `goal.scorer_team_id` is the team the **player plays for**.
   - For team-level goals-scored questions ("which team scored..."), ALWAYS join `team` on `goal.team_id` (NEVER `goal.scorer_team_id`).
   - For individual player top-scorer charts, join `team` on `goal.scorer_team_id` and exclude `is_own_goal = 1`.
   - For listing own goals (`is_own_goal = 1`), ALWAYS join `player` (for `p.name_fa`) and join `team` twice (on `scorer_team_id` AND `team_id`), projecting `(match_id, scorer, scorer_team, benefited_team, minute, added_time)` ordered by `match_id`.
3. Do not add `penalty` rows to `goal` rows; converted penalties are already in `goal` (`is_penalty = 1`). Use `penalty` for total penalty attempts and outcomes (`scored`, `saved`, `missed`).
4. **Goalkeeper clean sheets:** Join `lineup` (`position = 'GK'`, `is_starter = 1`) to `v_team_match` on **BOTH** `match_id` AND `team_id` (`l.match_id = tm.match_id AND l.team_id = tm.team_id`), join `match` with `m.status = 'finished'`, join `player` and `team`, and filter `tm.goals_against = 0`. Never join `lineup` to `v_team_match` on `team_id` alone.
5. **Referee & stadium:** Live on `match` (`match.referee_id`, `match.stadium_id`), never on event tables (`goal`, `card`, `substitution`, `penalty`, `var_decision`). Join event tables to `match` on `match_id`, then to `referee` / `stadium`.
6. **Played matches vs. standings:** Counting played matches MUST query `match WHERE status = 'finished'` directly (NEVER `v_team_match`, NEVER `home_goals IS NOT NULL`, and NEVER `status IN ('finished','awarded')` — the 1 `awarded` forfeit match counts only in standings/`v_team_match`).
7. **Sending-offs:** A sending-off / red card includes straight reds and second yellows: `card_type IN ('red','second_yellow')`.
8. **Starting formations:** Stored per side on `match` (`home_formation`, `away_formation`) for `status = 'finished'`. When joining via `match_coach` (or a team), pick the matching side: `CASE WHEN m.home_team_id = mc.team_id THEN m.home_formation ELSE m.away_formation END`.
9. **Late goals & stoppage time:** `minute` stores the regulation minute (`45` or `90` during stoppage time) and `added_time` stores stoppage minutes. "After the Nth minute" means strictly greater than N (`minute > N`); therefore "after the 75th minute" is `half = 'second_half' AND minute >= 76` (which already includes `90+...` stoppage time).
10. **Coaching changes:** `match_coach` stores the head coach per `(match_id, team_id)`. To list teams that changed head coach and every coach they used, first find teams with `HAVING COUNT(DISTINCT coach_id) > 1` in a subquery or CTE, then `GROUP BY mc.team_id, mc.coach_id` (joining `team`, `coach`, and `match` for round ranges/match counts) — never `GROUP BY team_id` alone in SQLite, which collapses multiple coaches into one arbitrary row.
11. **Minutes played:** Use `v_player_minutes` directly (`SUM(minutes_played)` grouped by `player_id` and `team_id`, joined to `player` and `team`).
12. **Lookup tables & team names:** Always join `team`, `player`, `coach`, `referee`, and `stadium` to display human-readable names rather than raw foreign-key IDs. Always project `team.name` (English) for team names in SQL output (consistent with `v_standings` and `v_match`), while `player`, `coach`, `referee`, and `stadium` only have `name_fa`.

## Known data quirks
- Player and coach names are Persian only; team names exist in both English and Persian.
- 58 substitutes entered the pitch without being listed on the bench (source gap); they appear in `substitution` but not in `lineup`.
- One player id (288406) has no name in the source and is stored as `(نامشخص)`.
- Stadium names are as published; some are short forms (e.g. `تختی`).
- Rounds are not strictly chronological: postponed games keep their original round number.
