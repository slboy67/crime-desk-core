# Crime-Pump Desk — Language

The desk issues trading calls on engineered-pump micro-caps, tracks them on a board, and keeps a scored record of its own calls. This glossary fixes the words the record and the rules are written in. Decision rules live in `CLAUDE.md`; this file is vocabulary only.

## Language

### The record

**Signature**:
A named setup pattern with its own entry rules, under which a call is committed and scored. A change to a signature's entry rules makes it a new signature.
_Avoid_: setup type, play, edge (an edge is a signature that has printed GO)

**Commit**:
The act of writing a thesis with restable order params to the board. A commit tests nothing until it fills.
_Avoid_: call, watch (a watch is a commit without a live entry)

**Filled trade**:
A commit on which the user actually took a position. Only filled trades count toward any gate.
_Avoid_: trade, entry, position (a position is the live venue state of a filled trade)

**Desk-scoped**:
Originated by the desk. The record contains only desk-scoped calls, never the user's trading outside the desk.
_Avoid_: the user's P&L, performance

**Paper row**:
A counterfactual score of a commit against forward price. Paper rows can warn but never gate.
_Avoid_: backtest, replay (replay is the quarantined harness record), simulation

**Override**:
A filled trade the desk would not have taken, tagged `desk_disagreed`. Measured, never blocked.
_Avoid_: mistake, discretionary (discretionary is the bucket for "no signature")

**Verdict**:
The one result the evaluator returns for a thesis on a tick: CONFIRMS (nothing crossed), WATCH-ARMED (a watch level crossed, no entry yet), TRIGGERS (a leg's entry filled, or a pre-committed action is due), BREAKS (a named field is broken). A BREAKS always carries `broken_field` — stop, time_stop, zone_blown, funding_watch, or regime_flip; a BREAK that cannot name its field is not a BREAK. Verdicts are computed per leg and rolled up by precedence BREAKS > TRIGGERS > WATCH-ARMED > CONFIRMS.
_Avoid_: state (the committed thesis is the state; the verdict is what the tick says about it), signal, alert (an alert is what a verdict transition sends)

**Leg**:
One restable entry inside a thesis — entry mode (breakdown, fade, or momentum), zone or reference level, stop, targets. A thesis commits at least two legs (a preferred retest/fade leg and a smaller insurance leg). Legs are the geometry; the thesis-level entry_zone/stop/tp are a view of the preferred leg.
_Avoid_: order (orders are what the user rests on the venue for a leg), setup

### Tiers and gates

**Tier**:
A signature's standing in the record: Hypothesis, GO, or Demoted. Tier changes the size ceiling and nothing else.
_Avoid_: status, grade, proven (use GO)

**Hypothesis**:
The default tier. Every signature starts here and returns here on demotion.
_Avoid_: unproven, scout (scout describes size, not tier)

**GO**:
The tier a signature reaches on live filled n≥10 with total R ≥ +5 and average R > 0. Symmetric with demotion.
_Avoid_: validated, promoted, proven

**Demoted**:
The tier a GO'd signature falls to when its trailing-10 R is negative. Re-promotion requires clearing GO again on the trailing window.
_Avoid_: killed, retired (retired is a board row, not a signature)

**Trailing-10**:
The total R of a signature's last ten filled trades. The only post-GO monitor between checkpoints.
_Avoid_: rolling window, drawdown

**Checkpoint**:
A filled-count at which a re-grill is due (per signature at n=25; desk-wide at n=30). A checkpoint never changes size on its own.
_Avoid_: kill-switch (the old desk-wide halt is the desk checkpoint), review

**Earned signature**:
A signature with at least one filled trade and positive total R. Earned is not GO; it only means the signature has a record.
_Avoid_: tradeable set (the tradeable set is a CLAUDE.md policy list)

### Size

**Equity**:
The perp account's total value on the execution venue at the moment of commit, read from the venue.
_Avoid_: account size, balance, available margin

**Risk-at-stop**:
The loss in currency if the committed stop fills, expressed as a percentage of equity. The unit every size cap is written in.
_Avoid_: position size, notional, margin, dollar cap

**Per-name cap**:
The most risk-at-stop one name may carry: the tier's ceiling or the size the exit absorbs, whichever is smaller. It follows the live thesis, not the name.
_Avoid_: position cap, $500 cap, $1k cap

**Cluster heat**:
The combined risk-at-stop across all names sharing an operator. Capped independently of tier.
_Avoid_: operator exposure, correlation

**Size-to-exit**:
The largest position the exit-side book absorbs at the desk's slippage tolerance. Computed on the execution venue's book, with liquidation distance computed on the index constituents.
_Avoid_: liquidity, depth, absorbable size

**Max leverage**:
The venue's per-symbol leverage ceiling, read from the venue. Never assumed.
_Avoid_: leverage available, 10x

### OI construction

**OI type**:
One of five populations perp open interest decomposes into: Funding-farm, Vesting-hedge, Operator-AMM, MM-inventory, Cross-venue funding arb. Whatever fits none is Directional. Each type is asserted only on its mandatory evidence plus at least two agreeing signals; otherwise it is Unknown.
_Avoid_: arb OI (only three of the five are arbs), delta-neutral (Operator-AMM is not)

**Squeeze response**:
A type's fixed behavior under (a) a squeeze and (b) carry decay — the attribute downstream gates consume instead of re-deriving mechanics from the label.
_Avoid_: fuel (fuel is the directional remainder, not an attribute)

**Funding-farm**:
Delta-neutral OI collecting funding (long spot or long elsewhere, short the perp; cash-carry and basis books included). Under squeeze: no capitulation. On carry decay: quietly unwinds within a few settlements.
_Avoid_: cash-carry and basis as separate types

**Vesting-hedge**:
A delta-neutral short against locked supply, alive even while paying funding. Unassertable without the on-chain lock contract. Under squeeze: never covers, re-squeezes. On carry decay: stays until unlock.
_Avoid_: OTC hedge (OTC is the invisible variant this type cannot see)

**Operator-AMM**:
The §4-A crew's own perp book — net long, farming shorts. Non-fuel because it is the squeezer's own position, not because it is delta-neutral. Under squeeze: it IS the squeeze. On carry decay: exits on operator discretion, not carry math.
_Avoid_: AMM-farm-as-arb, LP hedge

**MM-inventory**:
Market-maker inventory hedging: side-flipping, short-lived, nets out in hours. Reads as top-book neutrality. A name whose top book is all MM-inventory has no whale conviction either side — a real verdict, distinct from no-data.
_Avoid_: noise, background

**Cross-venue funding arb**:
One delta-neutral book long the perp on one venue and short it on another, harvesting funding dispersion. Counts as OI on both legs, so it overstates aggregate OI by roughly twice its size; the layer flags the overstatement rather than reporting inflated fuel.
_Avoid_: basis trade

**Decomposition verdict**:
The roll-up of asserted OI types: Arb-dominated (non-directional types read dominant), Mixed (asserted but sub-dominant or share unreadable; an asserted Operator-AMM forces at least Mixed), Directional (nothing asserted AND the instruments actually ran), Unknown (they could not). Per-type share is read categorically — unknown / minor / material / dominant — never as an undefendable percentage.
_Avoid_: arb ratio, percentage split

**Leverage state**:
The dynamic read joining OI change to range-hold, on a tactical and a swing window: Reset-constructive (drain, range holds — the next-expansion entry loads), Move-done (drain, range lost), Loading (build, rangebound — the recruitment tape), Trend-feeding (build, trending — chasing territory), Wash-pinned (both sides run by one operator; overrides the rest), Unknown. Hold is judged on closes, never wicks. It annotates entries; it never gates.
_Avoid_: OI momentum, leverage flush

**Battlefield**:
A name's market type — Perp-led, Spot-led, Mixed, or Unknown — decided before any entry framing. Asymmetric: perp-led is provable from one axis (volume ratio or OI/MC), spot-led claims both. Perp-led names take perp-structure entries and downgrade on-chain reads to context; spot-led names take level entries with on-chain at full weight.
_Avoid_: market type, regime (regime is the BTC-context read), perp-heavy (that is the OI/MC flag, one input)

**Venue role**:
One of four independent labels a venue can hold on a name — Mark-engine, Size-book, Exit, Hedge. One venue may hold several; each role independently resolves to a venue, Split, None-detected, or Unknown.
_Avoid_: the venue (roles are per-name, not global), four distinct venues

**Mark-engine**:
A constituent carrying ≥20% weight of the anchor index — the execution venue's own index, falling back to the size-book venue's, then Binance's. The books that move the mark that liquidates.
_Avoid_: the mark (marks are per-venue, plural)

**Size-book**:
The venue holding the top OI share when that share is ≥40%; shares within 15 points = Split. A partial OI sweep cannot crown a size-book — it reads Unknown.
_Avoid_: main venue, where it trades

**Exit** (venue role):
Where the float leaves. Flow-confirmed when a tracked wallet's deposit rail terminates there within 14 days; otherwise Depth-inferred from the deepest real spot book. Flow beats depth. No flow and no spot anywhere = Unknown, itself a perp-only red flag.
_Avoid_: exit liquidity (that is a size question, §7)

**Hedge** (venue role):
The venue where a delta-neutral leg parks: a persistent cross-venue funding outlier (mandatory) plus elevated OI share or contrarian basis. None-detected is the normal answer and is not Unknown.
_Avoid_: short venue, arb venue

**None-detected vs Unknown**:
None-detected = the read ran and the role has no holder. Unknown = the read could not run (unpublished composition, partial sweep, sparse funding table, stale snapshot). Unknown always prints its reason.
_Avoid_: n/a, null, missing

### Execution boundary

**Execution venue**:
Where the user fills (Aster). The desk reads it; only the user trades on it.
_Avoid_: exchange, the book

**Venue read**:
Anything the desk pulls from the execution venue's account endpoints: positions, fills, max leverage, equity. Read-only by definition.
_Avoid_: sync, integration

**Resting order**:
A stop or take-profit the user has placed on the venue that enforces a committed thesis while the user is away.
_Avoid_: automation, desk-managed exit
