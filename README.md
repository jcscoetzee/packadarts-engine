# PackaDarts engine

Decision engine for the **Warriors Fantasy League** (Sleeper, Lock-In mode, 12-team dynasty).
Team: PackaDarts / OnlyFranz, roster 7, league `1352074497095655424`.

It has three parts and none of them need a computer switched on:

1. **This repo + a GitHub Action** (`.github/workflows/pull.yml`) pulls Sleeper's public feed four
   times a day, runs `engine.py` in daily and weekly mode, and commits the snapshot plus
   `data/report_daily.json` and `data/report_weekly.json`. `fetch.py` is the puller; it needs no
   credentials because every endpoint is public and read-only.
2. **Claude scheduled tasks** clone this repo and read the reports and the snapshot. They never
   execute code from the repo (scheduled Claude sessions are not allowed to run downloaded code, and
   do not need to). They add the judgment layer (news, overrides, dynasty calls), read and write the
   engine's memory, and push a phone-sized recommendation.
3. **The dashboard artifact** (a private Claude page) holds the memory: decision log, tuned
   parameters, lessons, league-mate profiles, and a "done" tap that tells the engine what you
   actually did in Sleeper.

## Files

| File | What it does |
|---|---|
| `fetch.py` | Pulls league, rosters, users, matchups (this/next/previous week), transactions, trending adds, the full player catalog (filtered to everyone who matters), per-game logs for this season and a compressed last season, and the full NBA schedule. |
| `engine.py` | The maths. `--mode daily` = lock/pass + swaps + injuries + win probability. `--mode weekly` = lineup, adds/drops with bids, league scan, trade targets, and the audit of last week when a decision log is supplied. `--mode audit` = just the audit. |
| `params.json` | Parameters the Action runs the engine with, including mirrored player overrides. The live copy (newest overrides, tuned values) lives in the dashboard's database; the scheduled runs apply anything newer as judgment. |
| `data/` | Snapshots written by the Action. Never edit by hand. |

## Run it locally

```bash
python fetch.py                      # ~2 minutes; writes data/
python engine.py --mode daily        # JSON report on stdout
python engine.py --mode weekly --decisions decisions.json
```

## The maths in one paragraph

Each player gets a per-game fantasy-score distribution: this season's games weighted toward recent
ones, blended with last season scaled by current minutes, plus a probability of not playing
(injury status, rest history, back-to-backs). Lock-or-pass is optimal stopping: with games
g1..gk left, V_k = E[score_k] because the last game is forced, V_j = E[max(score_j, V_{j+1})], and
you lock a score s when s ≥ V_1. The league is head-to-head, so the daily run also simulates both
teams' remaining weeks and compares win probability for LOCK versus PASS, which decides the close
calls late in the week. Lineups are a max-weight assignment of players to slots. Streaming and
trades use the same number everywhere: the expected best game of a week, which is also why the
engine likes volatile players more than season averages suggest — you lock the max.

## Timezones

Game dates are US dates. Brisbane is 15 hours ahead of US Eastern during US standard time. The
Action pulls at 01:05, 07:05, 13:05, 21:05 UTC; the 07:05 pull is after every game has finished
and feeds the daily digest at 5:40pm Brisbane.
