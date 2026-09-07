#!/usr/bin/env python3
"""operator_veto.py — SPEC-149: the §0.6 counterparty read becomes a LIVE veto field.

The grill (2026-08-19 Q12/Q13) settled what the desk's on-chain counterparty read is
FOR: it cannot time an entry and cannot size one (fixed small size). On 2026-08-19 the
read was COMPLETE and CORRECT on SKYAI and BEAT — both trades still lost -1R, because
the read describes the DESTINATION while the stop lives in the PATH. Its proven skill
is knowing when the operator is NOT finished. That is a veto, not a permission slip:

  operator_not_done: true     -> VETO the short — the selling machine is still running
  operator_not_done: false    -> the structure event fired — short unlocks
  operator_not_done: "unknown"-> a stale or unreadable read; BLOCKS NOTHING (§3: a
                                  non-datum must never be asserted as data)

"Distribution confirmed on-chain => short it" is close to BACKWARDS at entry timescale:
an operator who is actively distributing still needs liquidity to sell into, and
squeezing is how they manufacture it (BEAT +81%, SKYAI +39%, both AFTER distribution
fired, both through the stop before the thesis paid). So:

  FRESH               -> true (the machine is running)
  FROZEN, but ROTATED -> true (the pause is FALSE — the VELVET rotation lesson)
  FROZEN < 24h floor   -> true (flow alone is the weakest evidence; time alone never
                          unlocks before the floor — VELVET's clock froze for days
                          while selling continued elsewhere)
  FROZEN >= 24h, no confirmed breakdown-hold -> "unknown" (flow may only ever BLOCK,
                          it must never unlock on its own — Q12=c)
  FROZEN >= 24h, breakdown-hold, but a squeeze leg still inside the name's own cadence
                       -> true (the squeeze machine isn't off yet — Q13=c)
  FROZEN >= 24h, breakdown-hold, squeeze machine off -> false (the structure event
                          fired; short unlocks)

Squeeze-machine-off is scaled PER NAME (Q13=c): no new leg for 2x that name's own
median gap between legs, derived from leg dates (price_structure's `squeezes[].day`),
never a fixed day count — BEAT ran a leg every ~9d, SKYAI four in four days.

Staleness: the newest distribution evidence aging past a window (default 4h) downgrades
whatever verdict would otherwise fire to "unknown" — a stale read asserted as a veto is
the §3 error (a non-datum treated as data).

build()/sweep() persist per-ticker state and fire ONE inbox event on a true->false
transition (the entry unlocking) — mirrors rotation_freshness.build_freshness's own
FROZEN->ROTATED transition-fire pattern. `sweep()` is the minimal cadence entry point
(req 6): unlike onchain.py's `_freshness_layer`, which only runs gated on a live SHORT
thesis, this runs headless over any ticker list — the full multi-leg discovery-tick
protocol is a separate spec, this is the entry seam for it.
"""
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(HERE))

STATE = ROOT / "state"

DEFAULT_STALE_WINDOW_H = 4       # newest evidence older than this -> "unknown" (§3)
DEFAULT_FROZEN_FLOOR_H = 24      # a freeze under this long can't contribute to an unlock


def _to_epoch(d):
    """'YYYY-MM-DD' / ISO string / epoch number -> epoch seconds, or None."""
    if d is None:
        return None
    if isinstance(d, (int, float)):
        return float(d)
    s = str(d)
    try:
        return datetime.strptime(s, "%Y-%m-%d").replace(tzinfo=timezone.utc).timestamp()
    except ValueError:
        pass
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()
    except (ValueError, AttributeError):
        return None


# ── squeeze-machine-off (Q13=c): the cadence threshold is DERIVED, never a constant ──

def squeeze_cadence_threshold_days(leg_dates):
    """2x the median gap (days) between consecutive squeeze legs — the name's own
    cadence. <2 legs (no cadence to derive from) -> None."""
    epochs = sorted(e for e in (_to_epoch(d) for d in (leg_dates or [])) if e is not None)
    if len(epochs) < 2:
        return None
    gaps = sorted((epochs[i + 1] - epochs[i]) / 86400.0 for i in range(len(epochs) - 1))
    n = len(gaps)
    median = gaps[n // 2] if n % 2 else (gaps[n // 2 - 1] + gaps[n // 2]) / 2.0
    return median * 2.0


def squeeze_machine_off(leg_dates, now=None):
    """True iff quiet-days since the LAST leg >= squeeze_cadence_threshold_days(leg_dates).
    No derivable cadence (<2 legs) -> True (nothing on record to block the unlock)."""
    threshold = squeeze_cadence_threshold_days(leg_dates)
    if threshold is None:
        return True
    now = now if now is not None else time.time()
    last = max(e for e in (_to_epoch(d) for d in leg_dates) if e is not None)
    quiet_days = (now - last) / 86400.0
    return quiet_days >= threshold


# ── the pure veto verdict ──────────────────────────────────────────────────────────

def classify(rotation_verdict, now=None, evidence_ts=None, frozen_since_ts=None,
            breakdown_hold=False, squeeze_off=True,
            stale_window_h=DEFAULT_STALE_WINDOW_H, frozen_floor_h=DEFAULT_FROZEN_FLOOR_H):
    """PURE tri-state veto verdict. `rotation_verdict` is rotation_freshness's own
    FRESH|FROZEN|ROTATED (or None/unrecognized). Returns
    {operator_not_done: True|False|"unknown", reason, evidence_age_h}."""
    now = now if now is not None else time.time()
    age_h = round((now - evidence_ts) / 3600.0, 1) if evidence_ts is not None else None
    if age_h is not None and age_h > stale_window_h:
        return {"operator_not_done": "unknown",
                "reason": f"evidence {age_h}h old > {stale_window_h}h staleness window — "
                          f"a stale read is not a veto (§3)",
                "evidence_age_h": age_h}

    v = (rotation_verdict or "").upper()
    if v == "FRESH":
        return {"operator_not_done": True,
                "reason": "distribution FRESH — the selling machine is running, do not short",
                "evidence_age_h": age_h}
    if v == "ROTATED":
        return {"operator_not_done": True,
                "reason": "FROZEN-but-ROTATED — the pause is FALSE, selling moved to fresh "
                          "wallets (VELVET-DWF)",
                "evidence_age_h": age_h}
    if v == "FROZEN":
        frozen_h = (now - frozen_since_ts) / 3600.0 if frozen_since_ts is not None else None
        if frozen_h is None or frozen_h < frozen_floor_h:
            fh = f"{frozen_h:.1f}h" if frozen_h is not None else "unknown-duration"
            return {"operator_not_done": True,
                    "reason": f"FROZEN {fh} < {frozen_floor_h}h floor — flow alone is the "
                              f"weakest evidence, time alone cannot unlock yet",
                    "evidence_age_h": age_h}
        if not breakdown_hold:
            return {"operator_not_done": "unknown",
                    "reason": f"FROZEN {frozen_h:.1f}h >= {frozen_floor_h}h floor but no "
                              f"confirmed breakdown-hold — flow may only ever BLOCK, it never "
                              f"unlocks alone (Q12=c)",
                    "evidence_age_h": age_h}
        if not squeeze_off:
            return {"operator_not_done": True,
                    "reason": "confirmed breakdown-hold, but a squeeze leg is still inside the "
                              "name's own cadence — the machine isn't off yet (Q13=c)",
                    "evidence_age_h": age_h}
        return {"operator_not_done": False,
                "reason": f"FROZEN {frozen_h:.1f}h + confirmed breakdown-hold + squeeze machine "
                          f"off — the structure event fired, short unlocks",
                "evidence_age_h": age_h}
    return {"operator_not_done": "unknown",
            "reason": f"rotation verdict unavailable/unrecognized ({rotation_verdict!r}) — "
                      f"no clean bill (§3)",
            "evidence_age_h": age_h}


# ── persistence + transition + page (mirrors rotation_freshness.build_freshness) ────

def _state_path(ticker, state_dir=None):
    return Path(state_dir or STATE) / f"operator_veto_{ticker.upper()}.json"


def read_state(ticker, state_dir=None):
    try:
        return json.loads(_state_path(ticker, state_dir).read_text())
    except Exception:  # noqa: BLE001
        return None


def build(ticker, rotation_verdict, now=None, state_dir=None, fire=True, event_fn=None,
         **classify_kwargs):
    """Compose the verdict, persist it, and fire ONE HIGH inbox event on a true->false
    transition (the entry unlocking — exactly the moment the user is away for)."""
    now = now if now is not None else time.time()
    ticker = ticker.upper()
    r = classify(rotation_verdict, now=now, **classify_kwargs)
    prev = read_state(ticker, state_dir) or {}
    transition = prev.get("operator_not_done") is True and r["operator_not_done"] is False
    r["prev_operator_not_done"] = prev.get("operator_not_done")
    r["transition"] = transition
    if transition and fire:
        if event_fn is None:
            try:
                import inbox
                event_fn = inbox.append_event
            except Exception:  # noqa: BLE001
                event_fn = None
        if event_fn:
            ts_iso = datetime.fromtimestamp(now, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            try:
                event_fn(ts_iso, ticker, "operator_veto", "HIGH",
                         f"⚡ {ticker} UNLOCKED — operator veto flipped true→false: "
                         f"{r['reason']} → short entry unlocks, read board")
            except Exception:  # noqa: BLE001 — the event must never break the read
                pass
    try:
        p = _state_path(ticker, state_dir)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps({"ticker": ticker, "operator_not_done": r["operator_not_done"],
                                 "reason": r["reason"], "ts": now}))
    except Exception:  # noqa: BLE001 — persistence must never break the read
        pass
    return r


# ── sweep: the minimal headless cadence entry point (req 6) ─────────────────────────

def _default_freshness(ticker, now=None):
    """Best-effort live freshness read (rotation_freshness + whatever last_out_ts the
    persisted rotation_freshness state already carries) — degrades to unavailable, never
    raises. Callers on a real cadence should inject `freshness_fn` sourced from the
    actual top-holder/onchain sweep; this default only echoes what's already on disk so
    `sweep()` still does something useful with zero wiring."""
    try:
        import rotation_freshness as RF
        st = RF.read_state(ticker)
        if not st:
            return {"verdict": None, "evidence_ts": None, "frozen_since_ts": None}
        return {"verdict": st.get("verdict"), "evidence_ts": st.get("ts"),
                "frozen_since_ts": st.get("ts")}
    except Exception:  # noqa: BLE001
        return {"verdict": None, "evidence_ts": None, "frozen_since_ts": None}


def _default_squeeze_legs(ticker):
    """Best-effort squeeze leg dates off price_structure (lazy — pulls venue HTTP).
    Degrades to [] (no cadence history) on any failure."""
    try:
        import price_structure as PS
        s = PS.build_structure(ticker)
        if s.get("error"):
            return []
        return [sq["day"] for sq in (s.get("squeezes") or [])]
    except Exception:  # noqa: BLE001
        return []


def sweep(tickers, now=None, state_dir=None, fire=True, freshness_fn=None,
         breakdown_hold_fn=None, squeeze_legs_fn=None, event_fn=None):
    """Run the operator_not_done veto for every ticker in `tickers` — decoupled from any
    live-SHORT-thesis gate (req 6), so it runs on a bare discovery-tick cadence over the
    whole board, not only inside an on-demand `brief`. Returns {ticker: verdict_dict}."""
    now = now if now is not None else time.time()
    freshness_fn = freshness_fn or _default_freshness
    breakdown_hold_fn = breakdown_hold_fn or (lambda t: False)
    squeeze_legs_fn = squeeze_legs_fn or _default_squeeze_legs
    out = {}
    for ticker in tickers:
        ticker = ticker.upper()
        fr = freshness_fn(ticker, now) or {}
        legs = squeeze_legs_fn(ticker) or []
        sq_off = squeeze_machine_off(legs, now=now)
        bh = bool(breakdown_hold_fn(ticker))
        out[ticker] = build(ticker, fr.get("verdict"), now=now, state_dir=state_dir, fire=fire,
                            event_fn=event_fn, evidence_ts=fr.get("evidence_ts"),
                            frozen_since_ts=fr.get("frozen_since_ts"),
                            breakdown_hold=bh, squeeze_off=sq_off)
    return out


def _watchlist_tickers():
    """Every ticker currently on config/watchlist.json — the discovery-tick cadence
    caller's default (ops/discovery_tick.sh SPEC-149 leg)."""
    try:
        raw = json.loads((ROOT / "config" / "watchlist.json").read_text())
        tokens = raw.get("tokens", raw) if isinstance(raw, dict) else raw
        return [t.get("ticker") for t in tokens if t.get("ticker")]
    except Exception:  # noqa: BLE001
        return []


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description="SPEC-149 operator_not_done live veto sweep")
    ap.add_argument("tickers", nargs="*",
                    help="tickers to sweep (default: every watchlist ticker)")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()
    result = sweep(args.tickers or _watchlist_tickers())
    print(json.dumps(result, indent=None if args.json else 2))
