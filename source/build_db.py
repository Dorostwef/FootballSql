"""Build a clean SQLite database (football.db) from the varzesh3 season export.

Source: web-api.varzesh3.com  — Iran Persian Gulf Pro League, season 1405-06 (2026-27)
  /v2.0/football/leagues/6/seasons/903038/{results,fixtures}   (match list)
  /v2.0/football/matches/{id}                                   (match detail: lineups + events)
"""
import json
import sqlite3
import sys
from datetime import datetime, timedelta, timezone

SRC = sys.argv[1]
OUT = sys.argv[2]

raw = json.load(open(SRC, encoding="utf-8"))

TEHRAN = timezone(timedelta(hours=3, minutes=30))  # Iran has had no DST since 2022

TEAM_EN = {
    1: ("Zob Ahan", "Isfahan"),
    2: ("Fajr Sepasi", "Shiraz"),
    4: ("Esteghlal", "Tehran"),
    6: ("Persepolis", "Tehran"),
    9: ("Foolad", "Ahvaz"),
    10: ("Sepahan", "Isfahan"),
    11: ("Paykan", "Tehran"),
    14: ("Malavan", "Bandar Anzali"),
    18: ("Tractor", "Tabriz"),
    21: ("Sanat Naft Abadan", "Abadan"),
    37: ("Nassaji Mazandaran", "Qaem Shahr"),
    44: ("Gol Gohar Sirjan", "Sirjan"),
    488: ("Esteghlal Khuzestan", "Ahvaz"),
    901630: ("Kheybar Khorramabad", "Khorramabad"),
    901631: ("Aluminium Arak", "Arak"),
    902597: ("Mes Shahr-e Babak", "Shahr-e Babak"),
    902983: ("Shams Azar Qazvin", "Qazvin"),
    903039: ("Chadormalu Ardakan", "Ardakan"),
}

STADIUM_ALIASES = {"نقش جهان": "نقش جهان اصفهان"}

STATUS = {0: "scheduled", 7: "finished", 5: "cancelled", 9: "awarded"}
HALF = {2: "first_half", 3: "second_half", 4: "after_full_time"}
CARD = {1: "yellow", 2: "second_yellow", 3: "red"}
PEN = {1: "scored", 2: "saved", 3: "missed"}
VAR = {1: "goal_disallowed", 2: "penalty_awarded", 3: "penalty_cancelled", 5: "card_changed"}

SCHEMA = """
PRAGMA foreign_keys = ON;

CREATE TABLE competition (
    competition_id   INTEGER PRIMARY KEY,
    name             TEXT NOT NULL,
    name_fa          TEXT NOT NULL,
    country          TEXT NOT NULL
);

CREATE TABLE season (
    season_id        INTEGER PRIMARY KEY,
    competition_id   INTEGER NOT NULL REFERENCES competition(competition_id),
    name             TEXT NOT NULL,          -- '2026-27'
    name_jalali      TEXT NOT NULL,          -- '1405-06'
    num_rounds       INTEGER NOT NULL
);

CREATE TABLE team (
    team_id          INTEGER PRIMARY KEY,
    name             TEXT NOT NULL,          -- English name
    name_fa          TEXT NOT NULL,          -- Persian name (as on varzesh3)
    city             TEXT,
    primary_color    TEXT
);

CREATE TABLE player (
    player_id        INTEGER PRIMARY KEY,
    name_fa          TEXT NOT NULL
);

CREATE TABLE coach (
    coach_id         INTEGER PRIMARY KEY,
    name_fa          TEXT NOT NULL
);

CREATE TABLE referee (
    referee_id       INTEGER PRIMARY KEY,
    name_fa          TEXT NOT NULL UNIQUE
);

CREATE TABLE stadium (
    stadium_id       INTEGER PRIMARY KEY,
    name_fa          TEXT NOT NULL UNIQUE
);

CREATE TABLE match (
    match_id          INTEGER PRIMARY KEY,
    season_id         INTEGER NOT NULL REFERENCES season(season_id),
    round             INTEGER NOT NULL,       -- matchweek 1..34
    status            TEXT NOT NULL CHECK (status IN ('scheduled','finished','cancelled','awarded')),
    kickoff_utc       TEXT,                   -- ISO-8601 UTC, NULL if not yet scheduled
    match_date        TEXT,                   -- local (Tehran) date YYYY-MM-DD
    match_date_jalali TEXT,                   -- '1405/06/11'
    kickoff_local     TEXT,                   -- local time HH:MM
    home_team_id      INTEGER NOT NULL REFERENCES team(team_id),
    away_team_id      INTEGER NOT NULL REFERENCES team(team_id),
    home_goals        INTEGER,                -- NULL unless status in ('finished','awarded')
    away_goals        INTEGER,
    result            TEXT CHECK (result IN ('H','D','A')),
    referee_id        INTEGER REFERENCES referee(referee_id),
    stadium_id        INTEGER REFERENCES stadium(stadium_id),
    home_formation    TEXT,
    away_formation    TEXT
);

CREATE TABLE match_coach (
    match_id         INTEGER NOT NULL REFERENCES match(match_id),
    team_id          INTEGER NOT NULL REFERENCES team(team_id),
    coach_id         INTEGER NOT NULL REFERENCES coach(coach_id),
    PRIMARY KEY (match_id, team_id)
);

CREATE TABLE lineup (
    match_id         INTEGER NOT NULL REFERENCES match(match_id),
    team_id          INTEGER NOT NULL REFERENCES team(team_id),
    player_id        INTEGER NOT NULL REFERENCES player(player_id),
    shirt_number     INTEGER,
    is_starter       INTEGER NOT NULL CHECK (is_starter IN (0,1)),   -- 0 = on the bench
    formation_line   INTEGER,                -- 0 = goalkeeper, 1 = defence ... last = attack; NULL for bench
    position         TEXT CHECK (position IN ('GK','DF','MF','FW')),  -- derived from formation_line; NULL for bench
    PRIMARY KEY (match_id, player_id)
);

CREATE TABLE goal (
    goal_id          INTEGER PRIMARY KEY,
    match_id         INTEGER NOT NULL REFERENCES match(match_id),
    team_id          INTEGER NOT NULL REFERENCES team(team_id),   -- team CREDITED with the goal
    player_id        INTEGER REFERENCES player(player_id),        -- scorer
    scorer_team_id   INTEGER NOT NULL REFERENCES team(team_id),   -- team the scorer plays for (differs for own goals)
    minute           INTEGER NOT NULL,       -- regulation minute (45 or 90 during stoppage time)
    added_time       INTEGER NOT NULL DEFAULT 0,  -- stoppage-time minute, e.g. 90+6 -> minute 90, added_time 6
    half             TEXT NOT NULL,
    is_own_goal      INTEGER NOT NULL CHECK (is_own_goal IN (0,1)),
    is_penalty       INTEGER NOT NULL CHECK (is_penalty IN (0,1))
);

CREATE TABLE card (
    card_id          INTEGER PRIMARY KEY,
    match_id         INTEGER NOT NULL REFERENCES match(match_id),
    team_id          INTEGER NOT NULL REFERENCES team(team_id),
    player_id        INTEGER REFERENCES player(player_id),
    card_type        TEXT NOT NULL CHECK (card_type IN ('yellow','second_yellow','red')),
    minute           INTEGER NOT NULL,
    added_time       INTEGER NOT NULL DEFAULT 0,
    half             TEXT NOT NULL
);

CREATE TABLE substitution (
    substitution_id  INTEGER PRIMARY KEY,
    match_id         INTEGER NOT NULL REFERENCES match(match_id),
    team_id          INTEGER NOT NULL REFERENCES team(team_id),
    player_in_id     INTEGER REFERENCES player(player_id),
    player_out_id    INTEGER REFERENCES player(player_id),
    minute           INTEGER NOT NULL,
    added_time       INTEGER NOT NULL DEFAULT 0,
    half             TEXT NOT NULL
);

CREATE TABLE penalty (
    penalty_id       INTEGER PRIMARY KEY,
    match_id         INTEGER NOT NULL REFERENCES match(match_id),
    team_id          INTEGER NOT NULL REFERENCES team(team_id),
    taker_id         INTEGER REFERENCES player(player_id),
    outcome          TEXT NOT NULL CHECK (outcome IN ('scored','saved','missed')),
    minute           INTEGER NOT NULL,
    added_time       INTEGER NOT NULL DEFAULT 0,
    half             TEXT NOT NULL
);

CREATE TABLE var_decision (
    var_id           INTEGER PRIMARY KEY,
    match_id         INTEGER NOT NULL REFERENCES match(match_id),
    team_id          INTEGER NOT NULL REFERENCES team(team_id),   -- side the decision is recorded for
    player_id        INTEGER REFERENCES player(player_id),
    decision         TEXT NOT NULL CHECK (decision IN ('goal_disallowed','penalty_awarded','penalty_cancelled','card_changed')),
    minute           INTEGER NOT NULL,
    added_time       INTEGER NOT NULL DEFAULT 0,
    half             TEXT NOT NULL
);

CREATE INDEX ix_match_round ON match(round);
CREATE INDEX ix_match_home  ON match(home_team_id);
CREATE INDEX ix_match_away  ON match(away_team_id);
CREATE INDEX ix_lineup_player ON lineup(player_id);
CREATE INDEX ix_lineup_team   ON lineup(team_id, match_id);
CREATE INDEX ix_goal_match   ON goal(match_id);
CREATE INDEX ix_goal_player  ON goal(player_id);
CREATE INDEX ix_card_match   ON card(match_id);
CREATE INDEX ix_card_player  ON card(player_id);
CREATE INDEX ix_sub_match    ON substitution(match_id);

-- ---------- Convenience views ----------

-- One row per team per counted match (finished or awarded), from that team's point of view.
CREATE VIEW v_team_match AS
SELECT m.match_id, m.round, m.match_date, m.home_team_id AS team_id, m.away_team_id AS opponent_id,
       'home' AS venue, m.home_goals AS goals_for, m.away_goals AS goals_against,
       CASE WHEN m.home_goals > m.away_goals THEN 'W' WHEN m.home_goals = m.away_goals THEN 'D' ELSE 'L' END AS outcome
FROM match m WHERE m.status IN ('finished','awarded')
UNION ALL
SELECT m.match_id, m.round, m.match_date, m.away_team_id, m.home_team_id,
       'away', m.away_goals, m.home_goals,
       CASE WHEN m.away_goals > m.home_goals THEN 'W' WHEN m.away_goals = m.home_goals THEN 'D' ELSE 'L' END
FROM match m WHERE m.status IN ('finished','awarded');

-- League table computed from results (3 pts win, 1 draw).
CREATE VIEW v_standings AS
SELECT t.team_id, t.name AS team, t.name_fa AS team_fa,
       COUNT(*)                                   AS played,
       SUM(tm.outcome = 'W')                      AS won,
       SUM(tm.outcome = 'D')                      AS drawn,
       SUM(tm.outcome = 'L')                      AS lost,
       SUM(tm.goals_for)                          AS goals_for,
       SUM(tm.goals_against)                      AS goals_against,
       SUM(tm.goals_for) - SUM(tm.goals_against)  AS goal_diff,
       SUM(CASE tm.outcome WHEN 'W' THEN 3 WHEN 'D' THEN 1 ELSE 0 END) AS points
FROM v_team_match tm JOIN team t ON t.team_id = tm.team_id
GROUP BY t.team_id;

-- Human-readable match list.
CREATE VIEW v_match AS
SELECT m.match_id, m.round, m.status, m.match_date, m.kickoff_local,
       h.name AS home_team, a.name AS away_team, m.home_goals, m.away_goals, m.result,
       r.name_fa AS referee, s.name_fa AS stadium
FROM match m
JOIN team h ON h.team_id = m.home_team_id
JOIN team a ON a.team_id = m.away_team_id
LEFT JOIN referee r ON r.referee_id = m.referee_id
LEFT JOIN stadium s ON s.stadium_id = m.stadium_id;

-- Approximate minutes played per player per match (regulation minutes, stoppage time ignored, capped at 90).
CREATE VIEW v_player_minutes AS
SELECT l.match_id, l.team_id, l.player_id,
       l.is_starter,
       CASE WHEN l.is_starter = 1 THEN 0 ELSE si.minute END AS minute_on,
       COALESCE(so.minute, rc.minute, 90)                    AS minute_off,
       COALESCE(so.minute, rc.minute, 90) - CASE WHEN l.is_starter = 1 THEN 0 ELSE si.minute END AS minutes_played
FROM lineup l
JOIN match m ON m.match_id = l.match_id AND m.status = 'finished'
LEFT JOIN (SELECT match_id, player_in_id AS player_id, MIN(minute) AS minute
           FROM substitution GROUP BY 1, 2) si
       ON si.match_id = l.match_id AND si.player_id = l.player_id
LEFT JOIN (SELECT match_id, player_out_id AS player_id, MIN(minute) AS minute
           FROM substitution GROUP BY 1, 2) so
       ON so.match_id = l.match_id AND so.player_id = l.player_id
LEFT JOIN (SELECT match_id, player_id, MIN(minute) AS minute FROM card
           WHERE card_type IN ('red','second_yellow') GROUP BY match_id, player_id) rc
       ON rc.match_id = l.match_id AND rc.player_id = l.player_id
WHERE l.is_starter = 1 OR si.minute IS NOT NULL;
"""


def split_minute(t, raw_min):
    """'45+12' -> (45, 12); '67' -> (67, 0)."""
    t = (t or "").strip()
    if "+" in t:
        a, b = t.split("+", 1)
        return int(a), int(b)
    if t.isdigit():
        return int(t), 0
    return int(raw_min), 0


def position_for(line, max_line):
    if line is None:
        return None
    if line == 0:
        return "GK"
    if line == 1:
        return "DF"
    if line == max_line:
        return "FW"
    return "MF"


def main():
    con = sqlite3.connect(OUT)
    con.executescript(SCHEMA)
    cur = con.cursor()

    cur.execute("INSERT INTO competition VALUES (6, 'Persian Gulf Pro League', 'لیگ برتر ایران', 'Iran')")
    cur.execute("INSERT INTO season VALUES (903038, 6, '2026-27', '1405-06', 34)")

    for tid, (name_fa, color) in raw["teams"].items():
        en, city = TEAM_EN[int(tid)]
        cur.execute("INSERT INTO team VALUES (?,?,?,?,?)", (int(tid), en, name_fa, city, color))

    players = {int(k): v for k, v in raw["players"].items() if k not in ("null", "undefined")}
    # a few players are referenced by events but have no name in the source
    for e in raw["evs"]:
        for pid in (e[8], e[9]):
            if pid is not None and pid not in players:
                players[pid] = "(نامشخص)"
    for pid, name in sorted(players.items()):
        cur.execute("INSERT INTO player VALUES (?,?)", (pid, name))
    for cid, name in raw["coaches"].items():
        cur.execute("INSERT INTO coach VALUES (?,?)", (int(cid), name))

    referees, stadiums = {}, {}

    def ref_id(name):
        if not name:
            return None
        if name not in referees:
            referees[name] = len(referees) + 1
            cur.execute("INSERT INTO referee VALUES (?,?)", (referees[name], name))
        return referees[name]

    def stad_id(name):
        name = STADIUM_ALIASES.get(name, name)
        if not name:
            return None
        if name not in stadiums:
            stadiums[name] = len(stadiums) + 1
            cur.execute("INSERT INTO stadium VALUES (?,?)", (stadiums[name], name))
        return stadiums[name]

    match_teams = {}
    for (mid, week, jdate, ltime, utc, st, st_title, home, away, hg, ag,
         referee, stadium, _hr, _gr, hf, af) in sorted(raw["matches"], key=lambda m: (m[1], m[4] or "9", m[0])):
        status = STATUS[st]
        kickoff_utc = match_date = kickoff_local = None
        if utc:
            dt = datetime.strptime(utc, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
            kickoff_utc = dt.strftime("%Y-%m-%dT%H:%M:%SZ")
            loc = dt.astimezone(TEHRAN)
            match_date, kickoff_local = loc.strftime("%Y-%m-%d"), loc.strftime("%H:%M")
        if status not in ("finished", "awarded"):
            hg = ag = None
        result = None if hg is None else ("H" if hg > ag else "A" if ag > hg else "D")
        cur.execute(
            "INSERT INTO match VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (mid, 903038, week, status, kickoff_utc, match_date, jdate, kickoff_local, home, away,
             hg, ag, result, ref_id(referee), stad_id(stadium),
             hf if status == "finished" else None, af if status == "finished" else None))
        match_teams[mid] = (home, away)

    for mid, tid, cid in raw["tcoach"]:
        cur.execute("INSERT OR IGNORE INTO match_coach VALUES (?,?,?)", (mid, tid, cid))

    # lineups: max formation line per (match, team) to label forwards
    max_line = {}
    for mid, tid, pid, shirt, start, line, cap, motm in raw["apps"]:
        if line is not None:
            max_line[(mid, tid)] = max(max_line.get((mid, tid), 0), line)
    player_team = {}
    for mid, tid, pid, shirt, start, line, cap, motm in raw["apps"]:
        if start == -1:
            continue
        cur.execute("INSERT OR IGNORE INTO lineup VALUES (?,?,?,?,?,?,?)",
                    (mid, tid, pid, shirt, start, line if start == 1 else None,
                     position_for(line, max_line.get((mid, tid))) if start == 1 else None))
        player_team[(mid, pid)] = tid

    for eid, mid, et, sub, side, scope, t, raw_min, p, p2 in raw["evs"]:
        home, away = match_teams[mid]
        team = home if side == 0 else away
        other = away if side == 0 else home
        minute, added = split_minute(t, raw_min)
        half = HALF[scope]
        if et == 1:  # goal (open play / own goal)
            own = 1 if sub == 1 else 0
            cur.execute("INSERT INTO goal VALUES (?,?,?,?,?,?,?,?,?,?)",
                        (eid, mid, team, p, other if own else team, minute, added, half, own, 0))
        elif et == 2:
            cur.execute("INSERT INTO card VALUES (?,?,?,?,?,?,?,?)",
                        (eid, mid, team, p, CARD[sub], minute, added, half))
        elif et == 3:
            cur.execute("INSERT INTO penalty VALUES (?,?,?,?,?,?,?,?)",
                        (eid, mid, team, p, PEN[sub], minute, added, half))
            if sub == 1:  # converted penalty is also a goal
                cur.execute("INSERT INTO goal VALUES (?,?,?,?,?,?,?,?,?,?)",
                            (eid, mid, team, p, team, minute, added, half, 0, 1))
        elif et == 4:
            cur.execute("INSERT INTO substitution VALUES (?,?,?,?,?,?,?,?)",
                        (eid, mid, team, p, p2, minute, added, half))
        elif et == 5:
            cur.execute("INSERT INTO var_decision VALUES (?,?,?,?,?,?,?,?)",
                        (eid, mid, team, p, VAR[sub], minute, added, half))

    con.commit()

    # ---- integrity checks ----
    bad = con.execute("""
        SELECT m.match_id FROM match m
        WHERE m.status = 'finished' AND (
          m.home_goals <> (SELECT COUNT(*) FROM goal g WHERE g.match_id = m.match_id AND g.team_id = m.home_team_id) OR
          m.away_goals <> (SELECT COUNT(*) FROM goal g WHERE g.match_id = m.match_id AND g.team_id = m.away_team_id))
    """).fetchall()
    assert not bad, f"score/goal mismatch: {bad}"
    fk = con.execute("PRAGMA foreign_key_check").fetchall()
    assert not fk, f"FK violations: {fk[:5]}"
    con.execute("VACUUM")
    con.close()
    print("OK")


if __name__ == "__main__":
    main()
