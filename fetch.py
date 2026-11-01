#!/usr/bin/env python3
"""
Pull everything the PackaDarts engine needs from Sleeper's public feed and write
compact JSON snapshots into data/.  Runs on a schedule in GitHub Actions; no
credentials are needed because every endpoint used here is public and read-only.

Outputs (all under data/):
  state.json        Sleeper NBA state (current week etc.)
  league.json       league settings and scoring
  rosters.json      all rosters (raw)
  users.json        all owners (raw)
  matchups.json     {"week": N, "current": [...], "next": [...] or null}
  transactions.json {"weeks": {"N": [...], "N-1": [...]}}
  players.json      compact identity for every rostered player plus relevant free agents
  gamelogs.json     per-player game logs for this season (+ a compressed last-season summary)
  schedule.json     every NBA game this season (date, week, away, home, status)
  meta.json         when this snapshot was taken and how long it took
"""
import json, os, sys, time, datetime as dt
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError

LEAGUE_ID = os.environ.get("LEAGUE_ID", "1352074497095655424")
MY_ROSTER_ID = int(os.environ.get("MY_ROSTER_ID", "7"))
SEASON = None  # filled from state
OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
os.makedirs(OUT, exist_ok=True)

APP = "https://api.sleeper.app"
COM = "https://api.sleeper.com"
UA = {"User-Agent": "packadarts-engine/1.0 (fantasy league helper; public read-only data)"}

# Stat keys we keep per game (everything the league scoring can touch, plus minutes)
KEEP = ["pts", "reb", "ast", "stl", "blk", "to", "tpm", "fgmi", "ff", "tf", "dd", "td",
        "bonus_pt_40p", "bonus_pt_50p", "bonus_reb_20p", "bonus_ast_15p", "min", "fga", "fgm", "fta", "ftm"]


def get(url, tries=3, sleep=1.0):
    last = None
    for i in range(tries):
        try:
            with urlopen(Request(url, headers=UA), timeout=30) as r:
                return json.loads(r.read().decode("utf-8"))
        except HTTPError as e:
            last = e
            if e.code == 404:
                return None
            time.sleep(sleep * (i + 1))
        except (URLError, TimeoutError, json.JSONDecodeError) as e:
            last = e
            time.sleep(sleep * (i + 1))
    print(f"WARN giving up on {url}: {last}", file=sys.stderr)
    return None


def save(name, obj):
    with open(os.path.join(OUT, name), "w") as f:
        json.dump(obj, f, separators=(",", ":"), sort_keys=True)


def fantasy_points(stats, scoring):
    """Sleeper's own formula: sum of scoring weight x stat value for every scoring key present."""
    return round(sum(float(w) * float(stats.get(k, 0) or 0) for k, w in scoring.items()), 2)


def main():
    t0 = time.time()
    state = get(f"{APP}/v1/state/nba")
    season = str(state["season"])
    week = int(state.get("week") or state.get("display_week") or 1)
    league = get(f"{APP}/v1/league/{LEAGUE_ID}")
    scoring = league["scoring_settings"]
    rosters = get(f"{APP}/v1/league/{LEAGUE_ID}/rosters") or []
    users = get(f"{APP}/v1/league/{LEAGUE_ID}/users") or []
    cur = get(f"{APP}/v1/league/{LEAGUE_ID}/matchups/{week}") or []
    nxt = get(f"{APP}/v1/league/{LEAGUE_ID}/matchups/{week + 1}")
    prev = get(f"{APP}/v1/league/{LEAGUE_ID}/matchups/{week - 1}") if week > 1 else None
    tx = {}
    for w in (week, week - 1):
        if w >= 1:
            tx[str(w)] = get(f"{APP}/v1/league/{LEAGUE_ID}/transactions/{w}") or []

    save("state.json", state)
    save("league.json", league)
    save("rosters.json", rosters)
    save("users.json", users)
    save("matchups.json", {"week": week, "current": cur, "next": nxt, "previous": prev})
    save("transactions.json", {"weeks": tx})

    # ---- who matters: every rostered player + trending adds + best free agents by season average
    rostered = {}
    for r in rosters:
        for pid in (r.get("players") or []):
            rostered[str(pid)] = r["roster_id"]
    wanted = set(rostered)
    for hours in (24, 72):
        for t in (get(f"{APP}/v1/players/nba/trending/add?lookback_hours={hours}&limit=50") or []):
            wanted.add(str(t["player_id"]))
    for hours in (24,):
        for t in (get(f"{APP}/v1/players/nba/trending/drop?lookback_hours={hours}&limit=25") or []):
            wanted.add(str(t["player_id"]))

    # Season aggregates for all players (gives us the free-agent pool ranked by our scoring)
    season_stats = get(f"{COM}/stats/nba/{season}?season_type=regular&position[]=PG&position[]=SG&position[]=SF&position[]=PF&position[]=C&order_by=pts_std") or []
    fa_pool = []
    rows = season_stats.values() if isinstance(season_stats, dict) else season_stats
    for row in rows:
        if not isinstance(row, dict):
            continue
        pid = str(row.get("player_id"))
        st = row.get("stats") or {}
        gp = float(st.get("gp") or 0)
        if gp <= 0:
            continue
        avg = fantasy_points(st, scoring) / gp
        if pid not in rostered:
            fa_pool.append((avg, pid))
    fa_pool.sort(reverse=True)
    for avg, pid in fa_pool[:60]:
        wanted.add(pid)

    # ---- identity from the full catalog (big file, fetched once per run)
    catalog = get(f"{APP}/v1/players/nba") or {}
    players = {}
    for pid in wanted:
        c = catalog.get(pid) or {}
        players[pid] = {
            "name": (f"{c.get('first_name','')} {c.get('last_name','')}").strip() or pid,
            "team": c.get("team"),
            "pos": c.get("position"),
            "fpos": c.get("fantasy_positions") or ([c.get("position")] if c.get("position") else []),
            "inj": c.get("injury_status"),
            "inj_part": c.get("injury_body_part"),
            "inj_notes": c.get("injury_notes"),
            "news_updated": c.get("news_updated"),
            "age": c.get("age"),
            "birth_date": c.get("birth_date"),
            "years_exp": c.get("years_exp"),
            "status": c.get("status"),
            "rostered_by": rostered.get(pid),
        }

    # ---- game logs: this season per game, last season compressed
    gamelogs = {}
    prev_season = str(int(season) - 1)
    old = {}
    old_path = os.path.join(OUT, "gamelogs.json")
    if os.path.exists(old_path):
        try:
            old = json.load(open(old_path))
        except Exception:
            old = {}
    for i, pid in enumerate(sorted(wanted)):
        entry = {"cur": [], "prev": None}
        data = get(f"{COM}/stats/nba/player/{pid}?season_type=regular&season={season}&grouping=week")
        if isinstance(data, dict):
            # keep identity fresh from this feed too (it carries injury status)
            for wk in sorted(data.keys(), key=lambda k: int(k) if k.isdigit() else 0):
                for g in data.get(wk) or []:
                    st = g.get("stats") or {}
                    p = g.get("player") or {}
                    if p and pid in players:
                        players[pid]["inj"] = p.get("injury_status")
                        players[pid]["inj_part"] = p.get("injury_body_part")
                        players[pid]["team"] = p.get("team") or players[pid]["team"]
                        if p.get("fantasy_positions"):
                            players[pid]["fpos"] = p["fantasy_positions"]
                    played = bool(st) and float(st.get("min") or 0) > 0
                    entry["cur"].append({
                        "d": g.get("date"), "w": int(wk) if str(wk).isdigit() else None,
                        "opp": g.get("opponent"), "away": bool(g.get("is_away_team")),
                        "gid": g.get("game_id"), "played": played,
                        "fp": fantasy_points(st, scoring) if played else 0.0,
                        "st": {k: st.get(k) for k in KEEP if k in st} if played else {},
                    })
        # last season: reuse if already cached, otherwise fetch once
        cached_prev = (old.get(pid) or {}).get("prev") if old else None
        if cached_prev:
            entry["prev"] = cached_prev
        else:
            pdata = get(f"{COM}/stats/nba/player/{pid}?season_type=regular&season={prev_season}&grouping=week")
            fps, mins = [], []
            if isinstance(pdata, dict):
                for wk, games in pdata.items():
                    for g in games or []:
                        st = g.get("stats") or {}
                        if st and float(st.get("min") or 0) > 0:
                            fps.append(fantasy_points(st, scoring))
                            mins.append(float(st.get("min") or 0))
            entry["prev"] = {"n": len(fps), "fps": fps, "mean_min": (sum(mins) / len(mins)) if mins else None}
        gamelogs[pid] = entry
        if i % 25 == 0:
            print(f"  gamelogs {i}/{len(wanted)}", file=sys.stderr)
        time.sleep(0.05)

    # ---- schedule (full season, Sleeper's own feed; dates are US local)
    sched_raw = get(f"{APP}/schedule/nba/regular/{season}") or []
    schedule = []
    for g in sched_raw:
        try:
            schedule.append({"date": g["date"], "week": g.get("week"), "status": g.get("status"),
                             "away": g["away"]["team"], "home": g["home"]["team"], "gid": g.get("game_id")})
        except Exception:
            continue
    save("schedule.json", {"season": season, "games": schedule})

    save("players.json", players)
    save("gamelogs.json", gamelogs)
    save("meta.json", {"taken_at_utc": dt.datetime.utcnow().isoformat(timespec="seconds") + "Z",
                       "season": season, "week": week, "players": len(players),
                       "schedule_games": len(schedule), "seconds": round(time.time() - t0, 1)})
    print(f"done: week {week}, {len(players)} players, {len(schedule)} scheduled games, {round(time.time()-t0)}s")


if __name__ == "__main__":
    main()
