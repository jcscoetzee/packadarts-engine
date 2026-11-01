MODE = weekly, and also run --mode daily into report_daily.json so you have lock-state-aware swaps. It is about 7:45am Brisbane (4:45pm US Eastern the previous US day), before tonight's US games tip (from about 10am Brisbane). Work out whether today is Thursday or Saturday in Brisbane from the current time.

Thursday and Saturday both deliver:
1. Streaming: report.adds ranked by week_value (this week), next_week_value and ros_week_value, with games_this_week and games_next_week. The edge is games remaining: a free agent with four games left beats a bench player with one. Recommend at most two adds with bids and the paired drops (report.drops), as ADD and DROP recs. Dynasty judgment applies: do not drop a young player with long-term value for a streamer; say when the right move is to do nothing. Note that waivers clear daily in this league.
2. Swaps into open slots: report_daily.swaps (lock-state aware) and report_daily.starters for anything still open from the night before. Write SWAP recs for moves worth the margin.
3. Injury pivots: report.lineup rows and report_daily.injuries plus your news checks for players in tonight's US games.

Saturday additionally delivers the trade scan:
4. report.trade_targets are players on other rosters whose lock-in value (expected best game of a week) exceeds what their average suggests (lock_in_premium above about 1.12 with a healthy mean), ranked by premium; report.my_trade_chips are my players whose average overstates their lock-in value (low premium: steady, low variance). report.league has every team's record, lock efficiency, top-nine value, bench depth and average age; a losing team with an old roster is a seller in a dynasty, and its owner's lock efficiency tells you how sharp they are. Propose at most two concrete trade ideas (who to give, who to get, why it works for both sides, one line each) as TRADE recs. Keep them realistic: value for value, aimed at owners who have a reason to deal.
5. Update profiles/<roster_id> for the owners involved and any owner whose behaviour stands out in data/transactions.json: {owner, record, lock_efficiency, tendencies (short phrases), notes, updated}. Keep notes to two sentences.

Headline format: Thursday "Stream: ADD <x> DROP <y> (bid <b>) · <swap or 'lineup fine'>". Saturday "Weekend: <add/drop or 'hold'> · Trade idea: <give> for <get> (<owner>)".
