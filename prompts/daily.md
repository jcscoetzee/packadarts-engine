MODE = daily (read data/report_daily.json). This is the post-games digest: every US game of the night has finished (it is about 5:50pm Brisbane, 2:50am US Eastern).

Deliver:
1. Lock or pass for every starter whose decision is open: report.starters rows with a "verdict" (basis is win probability when the matchup is close, otherwise expected value). State for each: the score in hand, the hurdle (continuation_value), the chance a later game beats it (p_improve_if_pass), and the Brisbane deadline (the player's next game tip-off). Rows marked FINAL need nothing (the last game counts automatically); rows marked WAIT have not played yet this week.
2. Swaps: report.swaps lists bench players who would do better than an unlocked starter for the rest of the week (gain is in expected points). Recommend a swap only when the gain is at least the swap margin and the move is possible before the next tip.
3. Injuries: report.injuries and any news from your checks, especially for starters playing in tonight's US games (which tip from about 10am Brisbane tomorrow).
4. The matchup: report.matchup gives points now, expected finals and win probability. Say in one line what the week looks like.

Write recs for every LOCK, PASS and SWAP you recommend (not for FINAL or WAIT rows). Lock-state inference: a counted score that equals an earlier game but not the latest means the player is already locked; if it equals the latest game the engine assumes the decision is still open unless Jacob marked the matching LOCK rec done, so if a decision you are showing was already recommended yesterday and is still open, repeat it with the same rec id rather than creating a new one.
