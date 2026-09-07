# Check whether the retest already printed before offering a retest entry

**Rule.** Before handing the user a retest-leg card on a fast-moving name, pull the LAST 2-3 HOURS of intraday bars (15m, the user's venue included) and check whether the proposed entry zone ALREADY traded since the leg. A zone that was tagged-and-bounced an hour ago is a COMPLETED retest — offering it as a resting entry is stale at birth, and the current price ("pulling back toward the zone") may actually be the bounce LEAVING it.

**Why.** BMT 2026-08-25: the desk committed a retest leg at 0.0168-0.0176 and told the user "price 0.01817, already pulling back — may fill soon." The zone had fully traded 17:00-18:00 UTC (escalated wick to 0.0164, violent bounce) BEFORE the card was written; 0.01817 was the exit from the retest, not the approach. The user waited for an entry that had already happened; thesis confirmed without the trade. Daily/4h bars used at commit time hid the intra-hour event.

**How to apply.** Any retest/fade leg on a name whose trigger leg fired within the last ~24h: check 15m bars over the leg's span on BOTH the signal venue and Aster before presenting entry status. If the zone already traded: say so, mark the retest leg as spent-or-lottery, and lead with the momentum/continuation leg as the live entry.
