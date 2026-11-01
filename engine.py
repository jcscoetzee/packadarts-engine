#!/usr/bin/env python3
"""
PackaDarts decision engine for Sleeper Lock-In fantasy basketball.

Reads the snapshot written by fetch.py (data/) plus a parameters file and an optional
decision log, and prints a JSON report that Claude reasons over before writing the
recommendation to the manager.

  python engine.py --mode daily   --data data --params params.json [--decisions decisions.json]
  python engine.py --mode weekly  --data data --params params.json [--decisions decisions.json]
  python engine.py --mode audit   --data data --params params.json --decisions decisions.json

The maths, in one paragraph: every player gets a per-game fantasy-score distribution
(this season's games, weighted toward recent ones, blended with last season scaled by
minutes, plus a probability of not playing).  Lock-or-pass is an optimal-stopping
problem: with games g1..gk left, the value of staying unlocked before the last game is
V_k = E[score_k] (it is forced), and V_j = E[max(score_j, V_{j+1})].  You lock a score s
when s >= V_1.  Because the league is head-to-head, the daily mode also simulates both
teams' remaining weeks and compares win probability for LOCK versus PASS, which is what
actually decides close calls late in the week.  Lineups are a max-weight assignment of
players to slots where a player's value is the expected score the slot will end up
counting.  Streaming and trades use the same quantity: expected best game of a week.
"""
import argparse, json, math, os, random, sys, datetime as dt
from collections import defaultdict

# ----------------------------------------------------------------------------- data

SLOT_ELIGIBLE = {
    "PG": {"PG"}, "SG": {"SG"}, "SF": {"SF"}, "PF": {"PF"}, "C": {"C"},
    "G": {"PG", "SG"}, "F": {"SF", "PF"}, "UTIL": {"PG", "SG", "SF", "PF", "C"},
}
INJURY_MISS = {"Out": 0.97, "Doubtful": 0.80, "Questionable": 0.45, "Day-To-Day": 0.25,
               "DTD": 0.25, "Probable": 0.10, "GTD": 0.40}
# how much of the injury signal carries to the 2nd, 3rd, 4th upcoming game
INJURY_DECAY = {"Out": [1.0, 0.9, 0.8, 0.7], "Doubtful": [1.0, 0.6, 0.4, 0.3], "Questionable": [1.0, 0.4, 0.25, 0.2],
                "Day-To-Day": [1.0, 0.5, 0.3, 0.2], "DTD": [1.0, 0.5, 0.3, 0.2], "Probable": [1.0, 0.3, 0.2, 0.1],
                "GTD": [1.0, 0.4, 0.25, 0.2]}
EASTERN_OFFSET_HOURS = -5  # US Eastern standard time; good enough for game dates


def load(path):
    with open(path) as f:
        return json.load(f)


def load_data(d):
    data = {}
    for name in ["state", "league", "rosters", "users", "matchups", "players", "gamelogs", "schedule", "meta", "transactions"]:
        p = os.path.join(d, name + ".json")
        data[name] = load(p) if os.path.exists(p) else None
    return data


def us_date(now_utc):
    return (now_utc + dt.timedelta(hours=EASTERN_OFFSET_HOURS)).date()


# ----------------------------------------------------------------------------- distributions

class Dist:
    """Weighted empirical distribution of a player's single-game fantasy score (if he plays)."""

    def __init__(self, values, weights):
        pairs = sorted(zip(values, weights))
        self.v = [p[0] for p in pairs]
        self.w = [p[1] for p in pairs]
        tot = sum(self.w) or 1.0
        self.w = [w / tot for w in self.w]
        self.mean = sum(x * w for x, w in zip(self.v, self.w))
        self.sd = math.sqrt(max(0.0, sum(w * (x - self.mean) ** 2 for x, w in zip(self.v, self.w))))
        # cumulative weights for sampling
        self.cum = []
        c = 0.0
        for w in self.w:
            c += w
            self.cum.append(c)

    def e_max(self, c, p_miss):
        """E[max(score, c)] where score is 0 with probability p_miss, else drawn from the distribution."""
        e = p_miss * max(0.0, c)
        e += (1 - p_miss) * sum(w * max(x, c) for x, w in zip(self.v, self.w))
        return e

    def expectation(self, p_miss):
        return (1 - p_miss) * self.mean

    def sample(self, rng, p_miss):
        if rng.random() < p_miss:
            return 0.0
        r = rng.random()
        lo, hi = 0, len(self.cum) - 1
        while lo < hi:
            mid = (lo + hi) // 2
            if self.cum[mid] < r:
                lo = mid + 1
            else:
                hi = mid
        return self.v[lo] * (1 + rng.gauss(0, 0.03))

    def quantile(self, q):
        for x, c in zip(self.v, self.cum):
            if c >= q:
                return x
        return self.v[-1] if self.v else 0.0


def build_dist(pid, data, params):
    """Blend this season (recency weighted) with last season (scaled by minutes) into one distribution."""
    gl = (data["gamelogs"] or {}).get(pid) or {"cur": [], "prev": None}
    cur = [g for g in gl.get("cur", []) if g.get("played")]
    cur.sort(key=lambda g: g.get("d") or "")
    n = len(cur)
    half = float(params.get("recency_half_life", 15))
    vals, wts = [], []
    for i, g in enumerate(cur):
        vals.append(float(g["fp"]))
        wts.append(0.5 ** ((n - 1 - i) / half))
    cur_w = sum(wts)
    prev = gl.get("prev") or {}
    pfps = prev.get("fps") or []
    if pfps:
        scale = 1.0
        cur_min = [float((g.get("st") or {}).get("min") or 0) for g in cur if (g.get("st") or {}).get("min")]
        if len(cur_min) >= 3 and prev.get("mean_min"):
            scale = max(0.6, min(1.4, (sum(cur_min) / len(cur_min)) / float(prev["mean_min"])))
        prior = float(params.get("prior_games", 12))
        if n == 0:
            prior = max(prior, 20.0)
        each = prior / len(pfps)
        for x in pfps:
            vals.append(float(x) * scale)
            wts.append(each)
    if not vals:
        return None
    if n < 3 and not pfps:
        m = sum(vals) / len(vals)
        vals += [m * 0.6, m * 1.4]
        wts += [0.5, 0.5]
    return Dist(vals, wts)


def miss_prob(pid, data, params, game_index=0, is_b2b=False, game_date=None):
    """Probability the player does not play a given upcoming game (index 0 = his next game).
    params["overrides"][pid] may carry Claude's reading of the news: {"miss_prob_all": 1.0} or {"out_until": "YYYY-MM-DD"}."""
    ov = ((params.get("overrides") or {}).get(str(pid))) or {}
    if "miss_prob_all" in ov:
        return max(0.0, min(0.99, float(ov["miss_prob_all"])))
    if ov.get("out_until") and game_date and game_date < str(ov["out_until"]):
        return 0.98
    p = (data["players"] or {}).get(pid) or {}
    inj = p.get("inj")
    base = float(params.get("base_miss_rate", 0.08))
    gl = (data["gamelogs"] or {}).get(pid) or {}
    cur = gl.get("cur", [])
    if cur:
        dnp = sum(1 for g in cur if not g.get("played"))
        base = max(base, min(0.6, dnp / (len(cur) + 4)))
    if inj and inj in INJURY_MISS:
        m = INJURY_MISS[inj]
        decay = INJURY_DECAY.get(inj, [1.0, 0.7, 0.5, 0.35])[min(game_index, 3)]
        prob = base + (m - base) * decay
    else:
        prob = base
    if is_b2b:
        years = p.get("years_exp") or 0
        prob += float(params.get("b2b_miss_add_vet", 0.08)) if years >= 8 else float(params.get("b2b_miss_add", 0.03))
    return max(0.0, min(0.98, prob))


# ----------------------------------------------------------------------------- schedule helpers

def week_bounds(data):
    """{week: (first_date, last_date)} from the schedule feed."""
    out = {}
    for g in (data["schedule"] or {}).get("games", []):
        w = g.get("week")
        if w is None:
            continue
        d = g["date"]
        lo, hi = out.get(w, (d, d))
        out[w] = (min(lo, d), max(hi, d))
    return out


def _index_schedule(data):
    if "_by_team" in data:
        return
    by_team, team_dates = defaultdict(list), set()
    for g in (data["schedule"] or {}).get("games", []):
        for t in (g["away"], g["home"]):
            by_team[t].append(g)
            team_dates.add((t, g["date"]))
    for t in by_team:
        by_team[t].sort(key=lambda g: g["date"])
    data["_by_team"], data["_team_dates"] = by_team, team_dates


def team_games(data, team, week):
    _index_schedule(data)
    gs = [dict(g) for g in data["_by_team"].get(team, []) if g.get("week") == week]
    for g in gs:
        g["b2b"] = is_back_to_back(data, team, g["date"])
    return gs


def remaining_games(pid, data, week, today):
    """Upcoming games this week for the player's team that have not appeared in his log yet."""
    p = (data["players"] or {}).get(pid) or {}
    team = p.get("team")
    if not team:
        return []
    done_dates = {g.get("d") for g in ((data["gamelogs"] or {}).get(pid) or {}).get("cur", [])}
    out = []
    for g in team_games(data, team, week):
        if g["date"] in done_dates:
            continue
        if g["date"] < today.isoformat():
            continue  # stale snapshot guard: game day already passed
        out.append(g)
    return out


def is_back_to_back(data, team, date):
    _index_schedule(data)
    prev = (dt.date.fromisoformat(date) - dt.timedelta(days=1)).isoformat()
    return (team, prev) in data["_team_dates"]


# ----------------------------------------------------------------------------- lock-in maths

def continuation_values(dist, games, pid, data, params):
    """V_j for j = 1..k (value of being unlocked before game j), via backward induction."""
    k = len(games)
    if k == 0 or dist is None:
        return []
    pm = game_miss_probs(games, pid, data, params)
    V = [0.0] * (k + 1)
    V[k] = 0.0  # after the last game nothing is left
    # before the last game: forced lock of whatever happens (0 if he does not play)
    V[k - 1] = dist.expectation(pm[k - 1])
    for j in range(k - 2, -1, -1):
        V[j] = dist.e_max(V[j + 1], pm[j])
    return V[:k]  # V[0] is the value of entering the next game unlocked


def game_miss_probs(games, pid, data, params):
    return [miss_prob(pid, data, params, i, g.get("b2b", False), g.get("date")) for i, g in enumerate(games)]


def simulate_policy(dist, games, pid, data, params, rng, lock_threshold_values=None, miss_probs=None):
    """Play out remaining games with the EV-optimal policy; return the counted score."""
    k = len(games)
    if k == 0 or dist is None:
        return 0.0
    V = lock_threshold_values or continuation_values(dist, games, pid, data, params)
    pm = miss_probs or game_miss_probs(games, pid, data, params)
    for j in range(k):
        s = dist.sample(rng, pm[j])
        if j == k - 1:
            return s
        if s >= V[j + 1]:
            return s
    return 0.0


def p_improve(dist, games, pid, data, params, s, rng, sims):
    better = 0
    V = continuation_values(dist, games, pid, data, params)
    pm = game_miss_probs(games, pid, data, params)
    for _ in range(sims):
        if simulate_policy(dist, games, pid, data, params, rng, V, pm) > s:
            better += 1
    return better / sims if sims else 0.0


# ----------------------------------------------------------------------------- state inference

def infer_lock_state(pid, counted, data, week, decisions):
    """Return (state, locked_fp, latest_game) using Sleeper's counted score and the game log.
    locked        : counted equals an earlier game but not the latest -> definitely locked
    open          : counted equals the latest game (or nothing played) -> decision may be open
    """
    gl = ((data["gamelogs"] or {}).get(pid) or {}).get("cur", [])
    played = sorted([g for g in gl if g.get("w") == week and g.get("played")], key=lambda g: g["d"])
    if not played:
        return ("open", None, None)
    latest = played[-1]
    eq = lambda a, b: abs(float(a) - float(b)) < 0.26
    if counted is not None and not eq(counted, latest["fp"]):
        for g in played[:-1]:
            if eq(counted, g["fp"]):
                return ("locked", g["fp"], g)
        return ("open", None, latest)  # mismatch: treat as open but flag
    # counted equals latest: locked at latest only if the manager confirmed our lock
    for dsn in decisions or []:
        if str(dsn.get("pid")) == str(pid) and int(dsn.get("week") or -1) == int(week) and dkind(dsn) == "LOCK" \
                and dsn.get("game_date") == latest["d"] and dsn.get("done"):
            return ("locked", latest["fp"], latest)
    return ("open", None, latest)


def dkind(d):
    """Decision records may say `type` (engine) or `kind` (dashboard)."""
    return d.get("type") or d.get("kind")


# ----------------------------------------------------------------------------- assignment

def assign_slots(slots, candidates, values, players):
    """Max-weight assignment of candidate players to slots. slots: list of (slot_key, slot_pos).
    candidates: list of pid. values: {pid: value}. Returns {slot_key: pid}."""
    n = len(candidates)
    elig = []
    for key, pos in slots:
        ok = []
        for i, pid in enumerate(candidates):
            fpos = set((players.get(pid) or {}).get("fpos") or [])
            if fpos & SLOT_ELIGIBLE.get(pos, set()):
                ok.append(i)
        elig.append(ok)
    best = {}
    memo = {}

    def rec(si, used):
        if si == len(slots):
            return 0.0, []
        key = (si, used)
        if key in memo:
            return memo[key]
        # option: leave slot empty
        bv, bl = rec(si + 1, used)
        bl = [None] + bl
        for i in elig[si]:
            if used & (1 << i):
                continue
            v, lst = rec(si + 1, used | (1 << i))
            v += values.get(candidates[i], 0.0)
            if v > bv:
                bv, bl = v, [i] + lst
        memo[key] = (bv, bl)
        return memo[key]

    total, choice = rec(0, 0)
    out = {}
    for (key, pos), ci in zip(slots, choice):
        out[key] = candidates[ci] if ci is not None else None
    return out, total


# ----------------------------------------------------------------------------- report builders

def my_roster(data, roster_id):
    for r in data["rosters"] or []:
        if r["roster_id"] == roster_id:
            return r
    return None


def matchup_for(data, roster_id, which="current"):
    ms = (data["matchups"] or {}).get(which) or []
    mine = next((m for m in ms if m["roster_id"] == roster_id), None)
    if not mine:
        return None, None
    opp = next((m for m in ms if m.get("matchup_id") == mine.get("matchup_id") and m["roster_id"] != roster_id), None)
    return mine, opp


def owner_name(data, roster_id):
    r = my_roster(data, roster_id)
    if not r:
        return str(roster_id)
    for u in data["users"] or []:
        if u.get("user_id") == r.get("owner_id"):
            return (u.get("metadata") or {}).get("team_name") or u.get("display_name") or str(roster_id)
    return str(roster_id)


def pname(data, pid):
    return ((data["players"] or {}).get(pid) or {}).get("name") or str(pid)


def week_value(pid, data, params, week, today, dist_cache):
    """Expected counted score if this player occupies a slot for the rest of the week."""
    dist = dist_cache.get(pid)
    if dist is None:
        dist = build_dist(pid, data, params)
        dist_cache[pid] = dist
    games = remaining_games(pid, data, week, today)
    if not games or dist is None:
        return 0.0, games
    V = continuation_values(dist, games, pid, data, params)
    return V[0], games


def team_sim(entries, rng, data, params):
    """entries: list of dicts {fixed: float or None, dist, games, pid, V}. Returns one simulated total."""
    tot = 0.0
    for e in entries:
        if e["fixed"] is not None:
            tot += e["fixed"]
        else:
            if "pm" not in e:
                e["pm"] = game_miss_probs(e["games"], e["pid"], data, params)
            tot += simulate_policy(e["dist"], e["games"], e["pid"], data, params, rng, e["V"], e["pm"])
    return tot


def daily_report(data, params, roster_id, decisions, now_utc):
    rng = random.Random(7)
    sims = int(params.get("sims", 4000))
    today = us_date(now_utc)
    week = int((data["matchups"] or {}).get("week") or data["state"].get("week"))
    players = data["players"] or {}
    me, opp = matchup_for(data, roster_id)
    roster = my_roster(data, roster_id)
    slots_pos = [p for p in data["league"]["roster_positions"] if p not in ("BN", "IR")]
    starters = (me or roster).get("starters") or []
    counted = (me or {}).get("players_points") or {}
    dist_cache = {}

    # --- per-starter state and lock/pass decisions
    starter_rows, my_entries = [], []
    for slot_pos, pid in zip(slots_pos, starters):
        pid = str(pid)
        if pid in ("0", "None", ""):
            starter_rows.append({"slot": slot_pos, "pid": None, "state": "empty"})
            my_entries.append({"fixed": 0.0})
            continue
        state, locked_fp, latest = infer_lock_state(pid, counted.get(pid), data, week, decisions)
        dist = dist_cache.setdefault(pid, build_dist(pid, data, params))
        games = remaining_games(pid, data, week, today)
        p = players.get(pid) or {}
        row = {"slot": slot_pos, "pid": pid, "name": p.get("name"), "team": p.get("team"), "inj": p.get("inj"),
               "inj_part": p.get("inj_part"), "state": state, "counted": counted.get(pid),
               "games_remaining": [{"date": g["date"], "opp": (g["home"] if g["away"] == p.get("team") else g["away"]),
                                    "home": g["home"] == p.get("team")} for g in games],
               "dist_mean": round(dist.mean, 1) if dist else None, "dist_sd": round(dist.sd, 1) if dist else None}
        if state == "locked":
            row.update({"locked_fp": locked_fp, "locked_game": latest and latest.get("d")})
            my_entries.append({"fixed": float(locked_fp)})
        else:
            V = continuation_values(dist, games, pid, data, params) if (dist and games) else []
            row["continuation_value"] = round(V[0], 1) if V else None
            if latest is not None:
                s = float(latest["fp"])
                row["latest_game"] = {"date": latest["d"], "opp": latest.get("opp"), "fp": s}
                if not games:
                    row["decision"] = "FINAL"  # last game of the week counts automatically
                    my_entries.append({"fixed": s})
                else:
                    pi = p_improve(dist, games, pid, data, params, s, rng, sims // 4)
                    row["p_improve_if_pass"] = round(pi, 3)
                    row["ev_verdict"] = "LOCK" if s >= V[0] else "PASS"
                    row["deadline"] = games[0]["date"]
                    my_entries.append({"fixed": None, "dist": dist, "games": games, "pid": pid, "V": V, "open_score": s})
            else:
                row["decision"] = "WAIT"  # nothing played yet this week
                my_entries.append({"fixed": None, "dist": dist, "games": games, "pid": pid, "V": V} if (dist and games) else {"fixed": 0.0})
        starter_rows.append(row)

    # --- opponent model
    opp_entries, opp_rows = [], []
    if opp:
        oc = opp.get("players_points") or {}
        for slot_pos, pid in zip(slots_pos, opp.get("starters") or []):
            pid = str(pid)
            if pid in ("0", "None", ""):
                opp_entries.append({"fixed": 0.0})
                continue
            state, locked_fp, latest = infer_lock_state(pid, oc.get(pid), data, week, None)
            dist = dist_cache.setdefault(pid, build_dist(pid, data, params))
            games = remaining_games(pid, data, week, today)
            p = players.get(pid) or {}
            r = {"slot": slot_pos, "pid": pid, "name": p.get("name"), "state": state, "counted": oc.get(pid),
                 "games_remaining": len(games), "inj": p.get("inj")}
            if state == "locked":
                opp_entries.append({"fixed": float(locked_fp)})
            elif latest is not None and not games:
                opp_entries.append({"fixed": float(latest["fp"])})
            elif dist and games:
                V = continuation_values(dist, games, pid, data, params)
                # an unlocked opponent with a score in hand: assume he plays the EV-optimal policy
                if latest is not None and float(latest["fp"]) >= V[0]:
                    opp_entries.append({"fixed": float(latest["fp"])})
                else:
                    opp_entries.append({"fixed": None, "dist": dist, "games": games, "pid": pid, "V": V})
            else:
                opp_entries.append({"fixed": float(latest["fp"]) if latest else 0.0})
            opp_rows.append(r)

    # --- win probability for each open decision: LOCK now vs PASS (others EV-optimal)
    def win_prob(entries_mine, n):
        wins = 0
        for _ in range(n):
            a = team_sim(entries_mine, rng, data, params)
            b = team_sim(opp_entries, rng, data, params) if opp else 0.0
            wins += 1 if a > b else (0.5 if a == b else 0)
        return wins / n

    base_entries = []
    for e in my_entries:
        if e.get("fixed") is None and "open_score" in e:
            # default: EV-optimal choice for everyone
            lock_now = e["open_score"] >= e["V"][0]
            base_entries.append({"fixed": e["open_score"]} if lock_now else e)
        else:
            base_entries.append(e)
    p_win_base = win_prob(base_entries, sims // 2) if opp else None

    for i, (row, e) in enumerate(zip(starter_rows, my_entries)):
        if e.get("fixed") is None and "open_score" in e and opp:
            with_lock = list(base_entries)
            with_lock[i] = {"fixed": e["open_score"]}
            with_pass = list(base_entries)
            with_pass[i] = e
            pl = win_prob(with_lock, sims // 2)
            pp = win_prob(with_pass, sims // 2)
            row["winprob_lock"] = round(pl, 3)
            row["winprob_pass"] = round(pp, 3)
            margin = pl - pp
            if abs(margin) >= 0.01:
                row["verdict"] = "LOCK" if margin > 0 else "PASS"
                row["verdict_basis"] = "win probability"
            else:
                row["verdict"] = row["ev_verdict"]
                row["verdict_basis"] = "expected value (win probability indifferent)"
        elif e.get("fixed") is None and "open_score" in e:
            row["verdict"] = row["ev_verdict"]
            row["verdict_basis"] = "expected value"

    # --- swaps for unlocked slots: could a bench player do better for the rest of the week?
    bench = [str(x) for x in (roster.get("players") or []) if str(x) not in set(map(str, starters))]
    unavailable = set(map(str, (roster.get("reserve") or []) + (roster.get("taxi") or [])))
    bench = [b for b in bench if b not in unavailable]
    open_slots, current, cand_values, cands = [], {}, {}, []
    for row in starter_rows:
        if row.get("state") == "locked" or row.get("pid") is None or row.get("decision") == "FINAL":
            continue
        key = row["slot"] + "#" + str(len(open_slots))
        open_slots.append((key, row["slot"]))
        pid = row["pid"]
        current[key] = pid
        v, _ = week_value(pid, data, params, week, today, dist_cache)
        if row.get("latest_game") and row.get("verdict") == "LOCK":
            v = max(v, float(row["latest_game"]["fp"]))
        cand_values[pid] = v
        cands.append(pid)
    for b in bench:
        v, g = week_value(b, data, params, week, today, dist_cache)
        if v > 0:
            cand_values[b] = v
            cands.append(b)
    swaps = []
    if open_slots:
        assignment, total = assign_slots(open_slots, cands, cand_values, players)
        margin = float(params.get("swap_margin", 2.0))
        for key, pid in assignment.items():
            cur = current.get(key)
            if pid and pid != cur and cand_values.get(pid, 0) - cand_values.get(cur, 0) >= margin:
                swaps.append({"slot": key.split("#")[0], "out": cur, "out_name": pname(data, cur) if cur else None,
                              "in": pid, "in_name": pname(data, pid), "gain": round(cand_values.get(pid, 0) - cand_values.get(cur, 0), 1),
                              "in_games_remaining": len(remaining_games(pid, data, week, today))})

    # --- injuries on my roster
    injuries = [{"pid": pid, "name": pname(data, pid), "status": (players.get(pid) or {}).get("inj"),
                 "part": (players.get(pid) or {}).get("inj_part"), "starter": pid in set(map(str, starters))}
                for pid in map(str, roster.get("players") or []) if (players.get(pid) or {}).get("inj")]

    # --- expected finals
    exp_mine = sum(team_sim(base_entries, rng, data, params) for _ in range(400)) / 400
    exp_opp = (sum(team_sim(opp_entries, rng, data, params) for _ in range(400)) / 400) if opp else None

    return {
        "mode": "daily", "week": week, "us_date": today.isoformat(), "snapshot": (data["meta"] or {}).get("taken_at_utc"),
        "matchup": {"opponent_roster": opp and opp["roster_id"], "opponent": opp and owner_name(data, opp["roster_id"]),
                    "my_points_now": (me or {}).get("points"), "opp_points_now": opp and opp.get("points"),
                    "my_expected_final": round(exp_mine, 1), "opp_expected_final": exp_opp and round(exp_opp, 1),
                    "p_win_now": p_win_base and round(p_win_base, 3)},
        "starters": starter_rows, "opponent_starters": opp_rows, "swaps": swaps, "injuries": injuries,
    }


def weekly_report(data, params, roster_id, decisions, now_utc):
    today = us_date(now_utc)
    players = data["players"] or {}
    roster = my_roster(data, roster_id)
    cur_week = int((data["matchups"] or {}).get("week") or data["state"].get("week"))
    bounds = week_bounds(data)
    # plan for the week that has not started yet if today is past the current week's last date
    week = cur_week
    if cur_week in bounds and today.isoformat() > bounds[cur_week][1]:
        week = cur_week + 1
    dist_cache = {}
    slots_pos = [p for p in data["league"]["roster_positions"] if p not in ("BN", "IR")]
    unavailable = set(map(str, (roster.get("reserve") or []) + (roster.get("taxi") or [])))
    mine = [str(x) for x in roster.get("players") or [] if str(x) not in unavailable]

    def wk_value(pid, w):
        dist = dist_cache.setdefault(pid, build_dist(pid, data, params))
        p = players.get(pid) or {}
        games = team_games(data, p.get("team"), w) if p.get("team") else []
        games = [g for g in games if g["date"] >= today.isoformat()] if w == cur_week else games
        if not games or dist is None:
            return 0.0, len(games)
        V = continuation_values(dist, games, pid, data, params)
        return V[0], len(games)

    values = {pid: wk_value(pid, week)[0] for pid in mine}
    slots = [(f"{p}#{i}", p) for i, p in enumerate(slots_pos)]
    # mid-week: locked starters stay where they are; only the open slots get re-optimised
    locked = {}
    if week == cur_week:
        me, _ = matchup_for(data, roster_id)
        counted = (me or {}).get("players_points") or {}
        for (key, pos), pid in zip(slots, (me or roster).get("starters") or []):
            pid = str(pid)
            if pid in ("0", "None", ""):
                continue
            state, locked_fp, _ = infer_lock_state(pid, counted.get(pid), data, cur_week, decisions)
            if state == "locked":
                locked[key] = (pid, locked_fp)
    open_slots = [s for s in slots if s[0] not in locked]
    free_players = [pid for pid in mine if pid not in {v[0] for v in locked.values()} and values.get(pid, 0) > 0]
    assignment, total = assign_slots(open_slots, free_players, values, players)
    lineup = []
    for (key, pos) in slots:
        if key in locked:
            pid, lfp = locked[key]
            p = players.get(pid) or {}
            lineup.append({"slot": pos, "pid": pid, "name": p.get("name"), "team": p.get("team"), "fpos": p.get("fpos"),
                           "inj": p.get("inj"), "locked_fp": lfp, "week_value": lfp, "games": 0})
            total += float(lfp or 0)
            continue
        pid = assignment.get(key)
        p = players.get(pid) or {}
        lineup.append({"slot": pos, "pid": pid, "name": p.get("name"), "team": p.get("team"), "fpos": p.get("fpos"),
                       "inj": p.get("inj"), "week_value": round(values.get(pid, 0), 1) if pid else None,
                       "games": wk_value(pid, week)[1] if pid else 0})
    started = {x["pid"] for x in lineup if x["pid"]}
    bench_rows = sorted([{"pid": pid, "name": pname(data, pid), "team": (players.get(pid) or {}).get("team"),
                          "fpos": (players.get(pid) or {}).get("fpos"), "inj": (players.get(pid) or {}).get("inj"),
                          "week_value": round(values[pid], 1), "games": wk_value(pid, week)[1],
                          "age": (players.get(pid) or {}).get("age"), "years_exp": (players.get(pid) or {}).get("years_exp")}
                         for pid in mine if pid not in started], key=lambda r: r["week_value"])

    # --- streaming candidates: free agents by this week, next week and rest-of-season value
    def ros_value(pid):
        """Expected best game in a typical week, assuming health (long injuries are Claude's call via overrides)."""
        dist = dist_cache.setdefault(pid, build_dist(pid, data, params))
        if dist is None:
            return 0.0
        ov = ((params.get("overrides") or {}).get(str(pid))) or {}
        pm = float(ov["miss_prob_all"]) if "miss_prob_all" in ov else float(params.get("base_miss_rate", 0.08))
        # typical week: 3.5 games -> E[max of 3 or 4 draws] with the optimal stopping value as proxy
        v3 = dist.e_max(dist.e_max(dist.expectation(pm), pm), pm)
        v4 = dist.e_max(v3, pm)
        return 0.5 * (v3 + v4)

    fa = [pid for pid, p in players.items() if not p.get("rostered_by")]
    fa_rows = []
    for pid in fa:
        vw, gw = wk_value(pid, week)
        vn, gn = wk_value(pid, week + 1)
        fa_rows.append({"pid": pid, "name": pname(data, pid), "team": (players.get(pid) or {}).get("team"),
                        "fpos": (players.get(pid) or {}).get("fpos"), "inj": (players.get(pid) or {}).get("inj"),
                        "week_value": round(vw, 1), "games_this_week": gw, "next_week_value": round(vn, 1),
                        "games_next_week": gn, "ros_week_value": round(ros_value(pid), 1),
                        "age": (players.get(pid) or {}).get("age"), "years_exp": (players.get(pid) or {}).get("years_exp")})
    fa_rows.sort(key=lambda r: -(r["week_value"] + r["next_week_value"] + r["ros_week_value"]))
    # replacement level: my weakest HEALTHY bench player by rest-of-season value (an injured
    # stash or an empty-value prospect is a drop candidate, not the bar a streamer has to clear)
    my_ros = {pid: ros_value(pid) for pid in mine}
    droppable = sorted([(my_ros[pid], pid) for pid in mine if pid not in started])
    healthy = [(v, pid) for v, pid in droppable if (players.get(pid) or {}).get("inj") not in ("Out",) and v > 5]
    repl = healthy[0][0] if healthy else (droppable[0][0] if droppable else 0.0)
    repl_week = min([values[pid] for v, pid in healthy], default=0.0)
    budget_total = int((data["league"].get("settings") or {}).get("waiver_budget") or 400)
    budget_left = budget_total - int((roster.get("settings") or {}).get("waiver_budget_used") or 0)
    adds = []
    for r in fa_rows[:25]:
        gain_ros = r["ros_week_value"] - repl
        gain_week = r["week_value"] - repl_week
        if gain_ros <= 0 and gain_week <= float(params.get("add_margin", 3.0)):
            continue
        # bid: a share of the remaining budget that grows with the lasting value gain; streamers get the minimum
        share = max(0.0, min(0.30, 0.012 * gain_ros)) if gain_ros > 0 else 0.0
        bid = max(1, int(round(budget_left * share))) if share > 0 else 1
        adds.append({**r, "gain_ros_per_week": round(gain_ros, 1), "gain_this_week": round(gain_week, 1),
                     "suggested_bid": bid, "budget_left": budget_left,
                     "kind": "hold" if gain_ros > 2 else "stream"})
    drops = [{"pid": pid, "name": pname(data, pid), "ros_week_value": round(v, 1), "week_value": round(values[pid], 1),
              "age": (players.get(pid) or {}).get("age"), "years_exp": (players.get(pid) or {}).get("years_exp"),
              "inj": (players.get(pid) or {}).get("inj")} for v, pid in droppable[:6]]

    # --- league scan for trades: who is rebuilding, who needs what, where the lock-in premium hides
    teams = []
    for r in data["rosters"] or []:
        rid = r["roster_id"]
        ps = [str(x) for x in r.get("players") or []]
        vals = sorted([ros_value(p) for p in ps], reverse=True)
        s = r.get("settings") or {}
        ages = [a for a in ((players.get(p) or {}).get("age") for p in ps) if isinstance(a, (int, float))]
        teams.append({"roster_id": rid, "owner": owner_name(data, rid), "wins": s.get("wins"), "losses": s.get("losses"),
                      "fpts": s.get("fpts"), "ppts": s.get("ppts"), "division": s.get("division"),
                      "lock_efficiency": round(float(s.get("fpts") or 0) / float(s.get("ppts") or 1), 3) if s.get("ppts") else None,
                      "top9_ros_value": round(sum(vals[:9]), 1), "depth_10_14": round(sum(vals[9:14]), 1),
                      "avg_age": round(sum(ages) / len(ages), 1) if ages else None, "waiver_budget_used": s.get("waiver_budget_used")})
    teams.sort(key=lambda t: -(t["top9_ros_value"]))

    def premium(pid):
        dist = dist_cache.setdefault(pid, build_dist(pid, data, params))
        if dist is None or dist.mean <= 0:
            return None
        return round(ros_value(pid) / dist.mean, 3)

    targets = []
    for r in data["rosters"] or []:
        if r["roster_id"] == roster_id:
            continue
        for pid in map(str, r.get("players") or []):
            pr = premium(pid)
            dist = dist_cache.get(pid)
            if pr and dist and dist.mean >= 18:
                targets.append({"pid": pid, "name": pname(data, pid), "owner": owner_name(data, r["roster_id"]),
                                "roster_id": r["roster_id"], "mean": round(dist.mean, 1), "sd": round(dist.sd, 1),
                                "ros_week_value": round(ros_value(pid), 1), "lock_in_premium": pr,
                                "age": (players.get(pid) or {}).get("age"), "inj": (players.get(pid) or {}).get("inj")})
    targets.sort(key=lambda t: -t["lock_in_premium"])
    chips = []
    for pid in mine:
        pr = premium(pid)
        dist = dist_cache.get(pid)
        if pr and dist and dist.mean >= 15:
            chips.append({"pid": pid, "name": pname(data, pid), "mean": round(dist.mean, 1), "sd": round(dist.sd, 1),
                          "ros_week_value": round(ros_value(pid), 1), "lock_in_premium": pr,
                          "age": (players.get(pid) or {}).get("age")})
    chips.sort(key=lambda t: t["lock_in_premium"])

    out = {"mode": "weekly", "planning_week": week, "current_week": cur_week, "us_date": today.isoformat(),
           "snapshot": (data["meta"] or {}).get("taken_at_utc"),
           "week_dates": bounds.get(week), "lineup": lineup, "lineup_total_value": round(total, 1), "bench": bench_rows,
           "adds": adds[:10], "drops": drops, "budget_left": budget_left, "league": teams,
           "trade_targets": targets[:15], "my_trade_chips": chips[:8]}
    nxt_me, nxt_opp = matchup_for(data, roster_id, "next" if week == cur_week + 1 else "current")
    if nxt_opp:
        out["next_opponent"] = {"roster_id": nxt_opp["roster_id"], "owner": owner_name(data, nxt_opp["roster_id"])}
    if decisions:
        out["audit"] = audit_report(data, params, roster_id, decisions, now_utc)
    return out


def audit_report(data, params, roster_id, decisions, now_utc):
    """Score past LOCK/PASS decisions against what actually happened, and test alternative parameters."""
    today = us_date(now_utc)
    cur_week = int((data["matchups"] or {}).get("week") or data["state"].get("week"))
    bounds = week_bounds(data)
    # a week is finished once today is past its last game date
    finished = cur_week if (cur_week in bounds and today.isoformat() > bounds[cur_week][1]) else cur_week - 1
    gl_all = data["gamelogs"] or {}
    rows = []
    for d in decisions or []:
        if dkind(d) not in ("LOCK", "PASS") or d.get("week") is None:
            continue
        if int(d["week"]) > finished:
            continue  # week not finished
        pid = str(d.get("pid"))
        games = sorted([g for g in (gl_all.get(pid) or {}).get("cur", []) if g.get("w") == int(d["week"])], key=lambda g: g["d"])
        later = [g for g in games if g["d"] > str(d.get("game_date"))]
        best_later = max([float(g["fp"]) for g in later if g.get("played")] + [0.0]) if later else 0.0
        last_game = float(later[-1]["fp"]) if later else None
        s = float(d.get("score") or 0)
        if dkind(d) == "LOCK":
            regret = max(0.0, best_later - s)
            correct = best_later <= s
            realized_if_pass = last_game  # the forced outcome had he passed every time
        else:
            final = last_game if last_game is not None else s
            regret = max(0.0, s - final)
            correct = final >= s
            realized_if_pass = final
        rows.append({**{k: d.get(k) for k in ("id", "week", "pid", "score", "p_improve", "game_date", "done")}, "type": dkind(d),
                     "name": pname(data, pid), "best_later": round(best_later, 1), "last_game": last_game,
                     "regret": round(regret, 1), "correct": correct})
    n = len(rows)
    summary = {"decisions_scored": n}
    if n:
        summary["total_regret"] = round(sum(r["regret"] for r in rows), 1)
        summary["hit_rate"] = round(sum(1 for r in rows if r["correct"]) / n, 3)
        passes = [r for r in rows if r["type"] == "PASS" and r.get("p_improve") is not None]
        if passes:
            summary["pass_calibration"] = {"predicted_improve": round(sum(float(r["p_improve"]) for r in passes) / len(passes), 3),
                                           "actual_improve": round(sum(1 for r in passes if r["correct"]) / len(passes), 3)}
    # lock efficiency from Sleeper's own potential-points figure, me vs league
    eff = []
    for r in data["rosters"] or []:
        s = r.get("settings") or {}
        if s.get("ppts"):
            eff.append((r["roster_id"], round(float(s.get("fpts") or 0) / float(s["ppts"]), 3)))
    eff.sort(key=lambda x: -x[1])
    summary["lock_efficiency_rank"] = [{"roster_id": a, "owner": owner_name(data, a), "efficiency": b} for a, b in eff]

    # parameter search: replay decisions with alternatives, keep the set with least regret (needs volume)
    tuning = None
    if n >= 25:
        best = None
        for prior in (6, 12, 20):
            for half in (8, 15, 30):
                p2 = dict(params, prior_games=prior, recency_half_life=half)
                tot = 0.0
                for d in decisions:
                    if dkind(d) not in ("LOCK", "PASS") or int(d.get("week") or 99) > finished:
                        continue
                    pid = str(d["pid"])
                    cut = str(d.get("game_date"))
                    hist = {"gamelogs": {pid: {"cur": [g for g in (gl_all.get(pid) or {}).get("cur", []) if g["d"] <= cut],
                                              "prev": (gl_all.get(pid) or {}).get("prev")}},
                            "players": data["players"], "schedule": data["schedule"]}
                    dist = build_dist(pid, hist, p2)
                    later = sorted([g for g in (gl_all.get(pid) or {}).get("cur", []) if g.get("w") == int(d["week"]) and g["d"] > cut], key=lambda g: g["d"])
                    if not later or dist is None:
                        continue
                    fake_games = [{"date": g["d"], "away": "", "home": ""} for g in later]
                    V = continuation_values(dist, fake_games, pid, hist, p2)
                    s = float(d.get("score") or 0)
                    lock = s >= V[0]
                    outcome = s if lock else float(later[-1]["fp"])  # simplified: pass -> forced last game
                    best_pos = max(s, max(float(g["fp"]) for g in later))
                    tot += best_pos - outcome
                if best is None or tot < best[0]:
                    best = (tot, prior, half)
        if best:
            tuning = {"suggested_prior_games": best[1], "suggested_recency_half_life": best[2], "hindsight_regret": round(best[0], 1)}
    return {"rows": rows, "summary": summary, "tuning": tuning}


# ----------------------------------------------------------------------------- main

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", choices=["daily", "weekly", "audit"], required=True)
    ap.add_argument("--data", default="data")
    ap.add_argument("--params", default="params.json")
    ap.add_argument("--decisions", default=None)
    ap.add_argument("--roster", type=int, default=7)
    ap.add_argument("--now", default=None, help="ISO UTC timestamp override, e.g. 2026-11-02T07:40:00Z")
    ap.add_argument("--out", default=None)
    a = ap.parse_args()
    data = load_data(a.data)
    params = load(a.params) if os.path.exists(a.params) else {}
    decisions = load(a.decisions) if a.decisions and os.path.exists(a.decisions) else []
    if isinstance(decisions, dict):
        decisions = decisions.get("decisions") or list(decisions.values())
    now = dt.datetime.fromisoformat(a.now.replace("Z", "+00:00")).replace(tzinfo=None) if a.now else dt.datetime.utcnow()
    if a.mode == "daily":
        rep = daily_report(data, params, a.roster, decisions, now)
    elif a.mode == "weekly":
        rep = weekly_report(data, params, a.roster, decisions, now)
    else:
        rep = audit_report(data, params, a.roster, decisions, now)
    js = json.dumps(rep, indent=1)
    if a.out:
        with open(a.out, "w") as f:
            f.write(js)
    print(js)


if __name__ == "__main__":
    main()
