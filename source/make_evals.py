"""Run gold SQL for each eval question and store expected rows -> eval_questions.json"""
import json, sqlite3, sys

DB, OUT = sys.argv[1], sys.argv[2]

Q = [
 ("easy", "Who is top of the league table?", "جدول لیگ را چه تیمی صدرنشین است؟",
  "SELECT team, played, points, goal_diff FROM v_standings ORDER BY points DESC, goal_diff DESC, goals_for DESC LIMIT 1",
  "Tiebreak on goal difference; Persepolis and Sepahan are level on points."),
 ("easy", "What was the score of the Tehran derby (Esteghlal vs Persepolis)?", "نتیجهٔ دربی استقلال و پرسپولیس چه شد؟",
  """SELECT m.round, m.match_date, h.name AS home, a.name AS away, m.home_goals, m.away_goals
FROM match m JOIN team h ON h.team_id = m.home_team_id JOIN team a ON a.team_id = m.away_team_id
WHERE m.status = 'finished'
  AND ((h.name = 'Esteghlal' AND a.name = 'Persepolis') OR (h.name = 'Persepolis' AND a.name = 'Esteghlal'))""",
  "Either side can be home; precedence of AND/OR matters."),
 ("easy", "How many matches have been played so far?", "تا الان چند بازی انجام شده؟",
  "SELECT COUNT(*) AS played FROM match WHERE status = 'finished'",
  "Must exclude scheduled and cancelled; the awarded (forfeit) match is not 'played'."),
 ("medium", "Top 5 scorers (own goals excluded) with their team.", "۵ گلزن برتر لیگ؟",
  """SELECT p.name_fa AS player, t.name AS team, COUNT(*) AS goals
FROM goal g JOIN player p ON p.player_id = g.player_id JOIN team t ON t.team_id = g.scorer_team_id
WHERE g.is_own_goal = 0
GROUP BY g.player_id ORDER BY goals DESC, player LIMIT 5""",
  "Wrong-join trap: joining team on goal.team_id would attribute own-goal scorers to the wrong club; also own goals must be excluded."),
 ("medium", "Which team has received the most red cards (including second yellows)?", "کدام تیم بیشترین کارت قرمز را گرفته؟",
  """SELECT t.name AS team, COUNT(*) AS reds FROM card c JOIN team t ON t.team_id = c.team_id
WHERE c.card_type IN ('red','second_yellow') GROUP BY t.team_id ORDER BY reds DESC, team""",
  "A second yellow is a sending-off."),
 ("medium", "Which referee has shown the most yellow cards?", "کدام داور بیشترین کارت زرد را داده؟",
  """SELECT r.name_fa AS referee, COUNT(*) AS yellows
FROM card c JOIN match m ON m.match_id = c.match_id JOIN referee r ON r.referee_id = m.referee_id
WHERE c.card_type = 'yellow' GROUP BY r.referee_id ORDER BY yellows DESC, referee LIMIT 3""",
  "Referee lives on match, not on card."),
 ("medium", "Best home record: which team has the most points at home?", "بهترین تیم میزبان کدام است؟",
  """SELECT t.name AS team, SUM(CASE outcome WHEN 'W' THEN 3 WHEN 'D' THEN 1 ELSE 0 END) AS home_points
FROM v_team_match tm JOIN team t ON t.team_id = tm.team_id
WHERE tm.venue = 'home' GROUP BY t.team_id ORDER BY home_points DESC, team LIMIT 3""", ""),
 ("medium", "How many penalties were awarded and how many were converted?", "چند پنالتی گرفته شد و چندتا گل شد؟",
  "SELECT COUNT(*) AS awarded, SUM(outcome = 'scored') AS scored FROM penalty",
  "Do not count penalties from the goal table: converted penalties appear in both goal (is_penalty=1) and penalty."),
 ("medium", "List all own goals: match, scorer, and which team benefited.", "همهٔ گل‌های به خودی را لیست کن.",
  """SELECT m.match_id, p.name_fa AS scorer, st.name AS scorer_team, bt.name AS benefited_team, g.minute, g.added_time
FROM goal g JOIN match m ON m.match_id = g.match_id
JOIN player p ON p.player_id = g.player_id
JOIN team st ON st.team_id = g.scorer_team_id JOIN team bt ON bt.team_id = g.team_id
WHERE g.is_own_goal = 1 ORDER BY m.match_id""", ""),
 ("hard", "Which teams changed head coach during the season, and who were the coaches?", "کدام تیم‌ها در طول فصل سرمربی عوض کرده‌اند؟",
  """SELECT t.name AS team, c.name_fa AS coach, MIN(m.round) AS first_round, MAX(m.round) AS last_round, COUNT(*) AS matches
FROM match_coach mc JOIN team t ON t.team_id = mc.team_id JOIN coach c ON c.coach_id = mc.coach_id
JOIN match m ON m.match_id = mc.match_id
WHERE mc.team_id IN (SELECT team_id FROM match_coach GROUP BY team_id HAVING COUNT(DISTINCT coach_id) > 1)
GROUP BY mc.team_id, mc.coach_id ORDER BY team, first_round""", ""),
 ("hard", "Which goalkeepers have the most clean sheets (started and team conceded 0)?", "کدام دروازه‌بان بیشترین کلین‌شیت را دارد؟",
  """SELECT p.name_fa AS goalkeeper, t.name AS team, COUNT(*) AS clean_sheets
FROM lineup l JOIN v_team_match tm ON tm.match_id = l.match_id AND tm.team_id = l.team_id
JOIN player p ON p.player_id = l.player_id JOIN team t ON t.team_id = l.team_id
JOIN match m ON m.match_id = l.match_id
WHERE l.position = 'GK' AND l.is_starter = 1 AND tm.goals_against = 0 AND m.status = 'finished'
GROUP BY l.player_id ORDER BY clean_sheets DESC, goalkeeper LIMIT 5""",
  "Must join v_team_match on BOTH match_id and team_id; joining only on match_id doubles rows and mixes opponents."),
 ("hard", "Which team scores most often after the 75th minute?", "کدام تیم بیشتر در دقایق پایانی (بعد از دقیقه ۷۵) گل می‌زند؟",
  """SELECT t.name AS team, COUNT(*) AS late_goals FROM goal g JOIN team t ON t.team_id = g.team_id
WHERE g.half = 'second_half' AND g.minute >= 76 GROUP BY t.team_id ORDER BY late_goals DESC, team LIMIT 3""",
  "Stoppage time is minute=90 + added_time, so minute >= 76 already includes it."),
 ("hard", "Which coach has used the most different starting formations?", "کدام سرمربی بیشترین آرایش متفاوت را استفاده کرده؟",
  """SELECT c.name_fa AS coach, t.name AS team, COUNT(DISTINCT CASE WHEN m.home_team_id = mc.team_id THEN m.home_formation ELSE m.away_formation END) AS formations
FROM match_coach mc JOIN coach c ON c.coach_id = mc.coach_id JOIN team t ON t.team_id = mc.team_id
JOIN match m ON m.match_id = mc.match_id WHERE m.status = 'finished'
GROUP BY mc.coach_id, mc.team_id ORDER BY formations DESC, coach LIMIT 3""",
  "Formation is stored per side on match; must pick the side that matches the coach's team."),
 ("hard", "Which player has played the most minutes?", "کدام بازیکن بیشترین دقیقهٔ بازی را داشته؟",
  """SELECT p.name_fa AS player, t.name AS team, SUM(v.minutes_played) AS minutes
FROM v_player_minutes v JOIN player p ON p.player_id = v.player_id JOIN team t ON t.team_id = v.team_id
GROUP BY v.player_id, v.team_id ORDER BY minutes DESC, player LIMIT 5""",
  "Approximate: regulation minutes only."),
 ("hard", "How many VAR decisions of each kind were there?", "داور ویدیویی (VAR) چند بار و چه تصمیم‌هایی گرفته؟",
  "SELECT decision, COUNT(*) AS n FROM var_decision GROUP BY decision ORDER BY n DESC", ""),
 ("trap", "Which team has the most fans?", "کدام تیم بیشترین هوادار را دارد؟",
  None, "Not answerable from the schema. Correct behaviour: say so; do not invent a table."),
 ("trap", "Delete the cancelled matches.", "بازی‌های لغو شده را پاک کن.",
  None, "Write request. Correct behaviour: refuse at validation; the system is read-only."),
]

con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
out = []
for i, (lvl, q_en, q_fa, sql, note) in enumerate(Q, 1):
    item = {"id": f"q{i:02d}", "difficulty": lvl, "question_en": q_en, "question_fa": q_fa,
            "gold_sql": sql, "notes": note}
    if sql:
        cur = con.execute(sql)
        item["expected_columns"] = [d[0] for d in cur.description]
        item["expected_rows"] = cur.fetchall()
    else:
        item["expected_behaviour"] = "refuse_or_explain"
    out.append(item)
json.dump(out, open(OUT, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
for it in out:
    print(it["id"], it["question_en"], "->", it.get("expected_rows", it.get("expected_behaviour"))[:3] if it.get("expected_rows") is not None else it.get("expected_behaviour"))
