#!/usr/bin/env python3
"""rotation_freshness.py — SPEC-98: rotation-aware distribution freshness (VELVET-DWF lesson).

The desk's live exit signal on a distribution short is the top holder's last_out_ts
freshness (memory: feedback_distribution_timestamp_freshness_is_the_live_exit_signal).
VELVET proved the binary fresh/frozen read defeatable: DWF rotated supply through FRESH
wallets between pump legs, so the tracked wallet's timestamp FROZE while distribution
continued — a false pause the engine reported as "distribution stopped" (memory:
feedback_rotating_wallets_defeat_single_wallet_verify_dwf_cycle). Three-state verdict:

  FRESH    — tracked top holder(s) show recent outflow (the existing signal, still live).
  FROZEN   — tracked quiet AND no corroborating dump evidence → genuine pause. On a live
             short thesis this carries the VELVET discipline note: bank/exit, don't ride
             a dead-thesis short into the squeeze.
  ROTATED  — tracked quiet BUT distribution evidence continues → the pause is FALSE.

Rotation evidence, two independent legs (EITHER flips FROZEN → ROTATED):
  tape leg    — repeated dump legs on the perp (runs of longs_closing bars with a real
                net down-move) while the tracked wallet is quiet. No on-chain quota.
  onchain leg — recent large transfers into known CEX deposit/execution channels from
                senders that are (a) NOT tracked for the token, (b) young (low nonce —
                the fresh-wallet fingerprint), (c) sized meaningfully. Provider-gated
                (SPEC-97 seam); provider down → `unavailable`, NEVER a clean bill (§3).

State: state/rotation_freshness_<TICKER>.json persists the last verdict so the board can
echo it cheaply and so a FROZEN → ROTATED transition on a live thesis fires ONE HIGH
inbox event (that transition is exactly the "about to bank into a false pause" moment).

Thresholds live in config/rotation_freshness.json (documented defaults below).
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
CONFIG_PATH = ROOT / "config" / "rotation_freshness.json"

# Documented defaults — override any key in config/rotation_freshness.json.
DEFAULT_CFG = {
    # tracked leg: an outflow within this window = FRESH (distribution live on the clock)
    "fresh_window_h": 12,
    # tape leg: a "dump leg" = a run of >= leg_min_bars consecutive longs_closing bars
    # with a net move <= -leg_min_move_pct; >= min_legs such legs = rotation evidence
    "tape_leg_min_bars": 3,
    "tape_leg_min_move_pct": 0.5,
    "tape_min_legs": 2,
    # onchain leg: fresh-wallet fingerprint + size gates
    "onchain_days": 2,                 # recent-transfer window into the CEX channels
    "onchain_young_nonce_max": 50,     # sender lifetime nonce <= this = young/fresh wallet
    "onchain_min_fresh_wallets": 2,    # need >= this many distinct fresh senders
    "onchain_min_usd": 100_000,        # ...moving >= this much (priced) to count as the dump
    "onchain_min_pct_float": 0.5,      # ...or >= this % of supply when no price is available
    "onchain_max_channels": 3,         # bounded CEX-channel reads (quota discipline)
    # board annotation: a persisted verdict older than this is stale — don't echo it
    "board_max_age_h": 24,
}

FRESH, FROZEN, ROTATED = "FRESH", "FROZEN", "ROTATED"


def load_cfg(path=None):
    cfg = dict(DEFAULT_CFG)
    try:
        d = json.loads(Path(path or CONFIG_PATH).read_text())
        cfg.update({k: v for k, v in d.items() if k in DEFAULT_CFG})
    except Exception:  # noqa: BLE001 — missing/bad config = documented defaults
        pass
    return cfg


def _parse_ts(ts_iso):
    if not ts_iso:
        return None
    if isinstance(ts_iso, (int, float)):
        return float(ts_iso)
    try:
        return datetime.fromisoformat(str(ts_iso).replace("Z", "+00:00")).timestamp()
    except (ValueError, AttributeError):
        return None


def _short_ts(ts_iso):
    e = _parse_ts(ts_iso)
    if e is None:
        return "unknown"
    return datetime.fromtimestamp(e, tz=timezone.utc).strftime("%m-%d")


def _fmt_usd(v):
    v = abs(v or 0)
    if v >= 1e6:
        return f"${v / 1e6:.1f}M"
    if v >= 1e3:
        return f"${v / 1e3:.0f}K"
    return f"${v:.0f}"


# ── tape leg ────────────────────────────────────────────────────────────────────
def tape_leg_from_bars(bars, cfg=None):
    """Count dump legs in tape-shaped bars (tape.classify_bars output): runs of
    >= tape_leg_min_bars consecutive `longs_closing` bars whose net move is
    <= -tape_leg_min_move_pct. Missing bars → available:False (§3: an unread tape is
    NOT a quiet tape)."""
    cfg = cfg or load_cfg()
    if not bars:
        return {"available": False, "dump_legs": 0, "reason": "no tape bars"}
    legs, run = [], []
    for b in list(bars) + [{"oi_force": "_end"}]:
        if b.get("oi_force") == "longs_closing":
            run.append(b)
            continue
        if len(run) >= cfg["tape_leg_min_bars"]:
            move = sum(x.get("d_price_pct") or 0.0 for x in run)
            if move <= -cfg["tape_leg_min_move_pct"]:
                legs.append({"bars": len(run), "move_pct": round(move, 3)})
        run = []
    return {"available": True, "dump_legs": len(legs), "legs": legs,
            "detail": f"{len(legs)} dump legs (longs_closing runs)"}


def _default_tape_leg(ticker, cfg=None):
    """Live tape read (bounded), degrade-explicit. Lazy import — tape pulls venue HTTP."""
    try:
        import tape as _tape
        t = _tape.build_tape(ticker, window_min=120)
        if not t.get("available"):
            return {"available": False, "dump_legs": 0, "reason": t.get("reason") or "tape unavailable"}
        return tape_leg_from_bars(t.get("bars") or [], cfg=cfg)
    except Exception as e:  # noqa: BLE001
        return {"available": False, "dump_legs": 0, "reason": f"tape read failed: {str(e)[:80]}"}


# ── onchain leg ─────────────────────────────────────────────────────────────────
_CEX_HINTS = ("cex", "binance", "bybit", "gate", "bitget", "okx", "kucoin", "mexc",
              "htx", "kraken", "coinbase", "exchange", "hot")


def _known_cex_channels(max_channels):
    """Known CEX deposit/execution channel addresses from the entity maps (lazy import)."""
    try:
        import onchain as _oc
        labels = _oc._entity_labels()
    except Exception:  # noqa: BLE001
        return []
    out = []
    for addr, name in labels.items():
        if any(h in (name or "").lower() for h in _CEX_HINTS):
            out.append({"address": addr, "label": name})
            if len(out) >= max_channels:
                break
    return out


def fresh_wallet_cex_leg(ticker, contract, chain_key, tracked_addrs, channels=None,
                         transfers_fn=None, nonce_fn=None, price=None, float_supply=None,
                         now=None, cfg=None):
    """The on-chain rotation leg: among recent transfers INTO known CEX channels, senders
    that are NOT tracked for the token, YOUNG (nonce <= onchain_young_nonce_max) and sized
    meaningfully (usd >= onchain_min_usd priced, or >= onchain_min_pct_float of supply).
    Provider down / no contract / no channels → {available:False} — an unreadable leg is
    NEVER a clean bill (§3)."""
    cfg = cfg or load_cfg()
    now = now if now is not None else time.time()
    tracked = {(a or "").lower() for a in (tracked_addrs or set())}
    if not contract:
        return {"available": False, "reason": "no contract for token"}
    if channels is None:
        channels = _known_cex_channels(cfg["onchain_max_channels"])
    if not channels:
        return {"available": False, "reason": "no known CEX channels to scan"}
    if transfers_fn is None:
        try:
            import onchain as _oc
            transfers_fn = lambda a, c, ck, days=cfg["onchain_days"]: _oc.token_transfers(a, c, ck, days=days)  # noqa: E731
        except Exception as e:  # noqa: BLE001
            return {"available": False, "reason": f"provider seam unavailable: {str(e)[:80]}"}
    if nonce_fn is None:
        try:
            import onchain as _oc
            nonce_fn = _oc.nonce_of
        except Exception:  # noqa: BLE001
            nonce_fn = lambda a, ck="binance-smart-chain": None  # noqa: E731

    cutoff = now - cfg["onchain_days"] * 86400
    by_sender, read_any = {}, False
    for ch in channels[:cfg["onchain_max_channels"]]:
        ch_addr = (ch.get("address") or "").lower()
        try:
            txs, _src, _partial = transfers_fn(ch_addr, contract, chain_key)
        except Exception:  # noqa: BLE001 — a dead channel read is a coverage gap, not zero flow
            continue
        read_any = True
        for t in txs or []:
            to = (t.get("to_address") or "").lower()
            frm = (t.get("from_address") or "").lower()
            if to != ch_addr or not frm or frm in tracked:
                continue
            e = _parse_ts(t.get("block_timestamp"))
            if e is not None and e < cutoff:
                continue
            by_sender.setdefault(frm, 0.0)
            by_sender[frm] += float(t.get("value_decimal") or 0.0)
    if not read_any:
        return {"available": False, "reason": "provider unavailable (all channel reads failed)"}

    fresh, amount = [], 0.0
    for frm, amt in sorted(by_sender.items(), key=lambda kv: -kv[1]):
        try:
            n = nonce_fn(frm)
        except Exception:  # noqa: BLE001
            n = None
        if n is None or n > cfg["onchain_young_nonce_max"]:
            continue                       # unverifiable/old wallet ≠ the fresh-wallet fingerprint
        fresh.append({"address": frm, "amount": amt, "nonce": n})
        amount += amt
    usd = amount * price if price else None
    pct_float = (amount / float_supply * 100) if float_supply else None
    sized = ((usd is not None and usd >= cfg["onchain_min_usd"])
             or (pct_float is not None and pct_float >= cfg["onchain_min_pct_float"]))
    suspect = sized and len(fresh) >= cfg["onchain_min_fresh_wallets"]
    detail = (f"{len(fresh)} fresh wallets → CEX {_fmt_usd(usd) if usd is not None else f'{amount:,.0f} {ticker}'}"
              if fresh else "no fresh-wallet CEX flow")
    return {"available": True, "suspect": suspect, "n_fresh_wallets": len(fresh),
            "usd_to_cex": usd if usd is not None else 0.0, "pct_float": pct_float,
            "fresh_wallets": fresh[:5], "detail": detail}


def _default_onchain_leg(ticker, cfg=None):
    """Live fresh-wallet scan off the tracked config (lazy, quota-bounded, degrade-explicit)."""
    cfg = cfg or load_cfg()
    try:
        tok = json.loads((ROOT / "config" / "tracked_wallets.json").read_text()).get("tokens", {}).get(ticker.upper())
        if not tok:
            return {"available": False, "reason": "untracked token (no contract/wallet map)"}
        contracts = tok.get("contracts", {}) or {}
        chain = "binance-smart-chain" if "binance-smart-chain" in contracts else next(iter(contracts), None)
        if not chain:
            return {"available": False, "reason": "no contract on any chain"}
        tracked = {(w.get("address") or "").lower() for w in tok.get("wallets", [])}
        return fresh_wallet_cex_leg(ticker, contracts[chain], chain, tracked, cfg=cfg)
    except Exception as e:  # noqa: BLE001
        return {"available": False, "reason": f"onchain leg failed: {str(e)[:80]}"}


# ── the verdict (pure core) ────────────────────────────────────────────────────
def classify_freshness(last_out_ts, tape, onchain, dex=None, drip=None, breadth=None, now=None,
                       live_short_thesis=False, cfg=None):
    """PURE three-state verdict. `tape`/`onchain`/`dex`/`drip`/`breadth` are the
    evidence-leg dicts above (or None = not evaluated → treated as unavailable).
    Returns {verdict, line, evidence, note, tape_leg, onchain_leg, dex_leg, drip_leg,
    breadth_leg, last_out_ts}.

    SPEC-115: `dex` is a rotation-evidence leg — a qualifying large DEX sell by a
    tracked wallet or its SPEC-98 fresh-wallet child while the tracked top holder is
    quiet. It's *stronger* than the CEX leg (execution confirmed, not inferred —
    memory: feedback_predictor_vs_cause_dex_sale_is_execution) so it's cited first
    when it fires.

    SPEC-117: `drip` is the cumulative-micro-swap sibling of the `dex` leg — a
    DRIP_SELLER hit (recurring same-direction executions below SPEC-115's per-swap
    floor) from a tracked wallet or fresh child. Same seniority tier as `dex` (both
    are confirmed on-chain execution), cited right after it.

    SPEC-118: `breadth` is the pattern-level sibling of the `onchain` (fresh-wallet→CEX)
    leg — a BREADTH_SPIKE (many DISTINCT, largely untracked senders → CEX at once)
    while the tracked top holder is quiet. Weaker than `dex`/`drip` (inferred deposit
    activity, not a confirmed sell) but catches what single-wallet legs structurally
    miss (VELVET), so it's cited after them, alongside `onchain`."""
    cfg = cfg or load_cfg()
    now = now if now is not None else time.time()
    tape = tape or {"available": False, "dump_legs": 0, "reason": "not evaluated"}
    onchain = onchain or {"available": False, "reason": "not evaluated"}
    dex = dex or {"available": False, "reason": "not evaluated"}
    drip = drip or {"available": False, "reason": "not evaluated"}
    breadth = breadth or {"available": False, "reason": "not evaluated"}
    e = _parse_ts(last_out_ts)
    fresh = e is not None and (now - e) <= cfg["fresh_window_h"] * 3600

    if fresh:
        verdict = FRESH
        evidence = f"tracked top holder out {_short_ts(last_out_ts)} (<{cfg['fresh_window_h']}h)"
        note = "distribution live on the tracked clock — thesis check = keep verifying every cycle"
    else:
        frozen_since = _short_ts(last_out_ts) if e is not None else "baseline"
        tape_dumping = bool(tape.get("available")) and tape.get("dump_legs", 0) >= cfg["tape_min_legs"]
        oc_suspect = bool(onchain.get("available")) and bool(onchain.get("suspect"))
        dex_suspect = bool(dex.get("available")) and bool(dex.get("suspect"))
        drip_suspect = bool(drip.get("available")) and bool(drip.get("suspect"))
        breadth_suspect = bool(breadth.get("available")) and bool(breadth.get("suspect"))
        if tape_dumping or oc_suspect or dex_suspect or drip_suspect or breadth_suspect:
            verdict = ROTATED
            parts = []
            if dex_suspect:   # senior evidence — DEX execution is confirmed, not inferred
                lines = dex.get("lines") or []
                parts.append(f"{dex.get('n_sells', 0)} DEX EXECUTION sell(s)"
                             + (f" ({lines[0]})" if lines else ""))
            if drip_suspect:  # confirmed on-chain execution, same tier as `dex`
                lines = drip.get("lines") or []
                parts.append(f"{drip.get('n_hits', 0)} DRIP EXECUTION sell(s)"
                             + (f" ({lines[0]})" if lines else ""))
            if oc_suspect:
                usd = onchain.get("usd_to_cex")
                parts.append(f"{onchain.get('n_fresh_wallets', 0)} fresh wallets → CEX"
                             + (f" {_fmt_usd(usd)}" if usd else ""))
            if breadth_suspect:
                parts.append(f"BREADTH_SPIKE {breadth.get('senders')} senders "
                             f"(baseline {breadth.get('baseline')}) → CEX"
                             + (f" {_fmt_usd(breadth.get('usd'))}" if breadth.get("usd") else ""))
            if tape_dumping:
                parts.append(f"{tape.get('dump_legs')} dump legs on tape")
            evidence = f"top holder frozen {frozen_since} but " + " + ".join(parts)
            note = ("FALSE pause — distribution continues via rotation; do NOT bank off the "
                    "frozen timestamp; read the dump LEGS, not one wallet's clock (VELVET-DWF)")
        else:
            verdict = FROZEN
            evidence = f"tracked quiet since {frozen_since}"
            caveats = []
            if not onchain.get("available"):
                caveats.append("onchain_leg=unavailable — no clean bill (§3)")
            if not tape.get("available"):
                caveats.append("tape_leg=unavailable")
            if not dex.get("available"):
                caveats.append("dex_leg=unavailable")
            if not drip.get("available"):
                caveats.append("drip_leg=unavailable")
            if not breadth.get("available"):
                caveats.append("breadth_leg=unavailable")
            if caveats:
                evidence += " (" + "; ".join(caveats) + ")"
            confirmed = bool(onchain.get("available")) and bool(tape.get("available"))
            if confirmed and live_short_thesis:
                note = ("genuine pause — bank/exit the short; don't ride a dead-thesis short "
                        "into the squeeze (VELVET exit lesson)")
            elif not confirmed:
                note = "pause UNCONFIRMED (a leg unreadable) — re-check before banking on it (§3)"
            else:
                note = None
    line = f"distribution: {verdict} — {evidence}"
    return {"verdict": verdict, "line": line, "evidence": evidence, "note": note,
            "tape_leg": tape, "onchain_leg": onchain, "dex_leg": dex, "drip_leg": drip,
            "breadth_leg": breadth, "last_out_ts": last_out_ts}


# ── state + transition event + composition ────────────────────────────────────
def _state_path(ticker, state_dir=None):
    return Path(state_dir or STATE) / f"rotation_freshness_{ticker.upper()}.json"


def read_state(ticker, state_dir=None):
    try:
        return json.loads(_state_path(ticker, state_dir).read_text())
    except Exception:  # noqa: BLE001
        return None


def _live_short_thesis(ticker):
    """Is there a live SHORT thesis on `ticker` in the watchlist? (a READ, config only)"""
    try:
        toks = json.loads((ROOT / "config" / "watchlist.json").read_text()).get("tokens", [])
        for t in toks:
            if (t.get("ticker") or "").upper() != ticker.upper():
                continue
            th = t.get("thesis") or {}
            status = (th.get("status") or "").upper()
            return (th.get("direction") or "").upper() == "SHORT" and status not in ("RETIRED", "PASS")
    except Exception:  # noqa: BLE001
        pass
    return False


def _default_dex_leg(ticker, cfg=None):
    """SPEC-115 live DEX-execution leg: a qualifying sell by a tracked wallet or its
    SPEC-98 fresh-wallet child — the senior rotation leg (execution confirmed, not
    inferred). Provider down / untracked token → {available: False} (§3)."""
    try:
        import dex_execution as _dx
        out = _dx.build_execution(ticker)
        if not out.get("available"):
            return {"available": False, "reason": out.get("reason")}
        sells = [h for h in out.get("hits", [])
                if h.get("direction") == "sell" and (h.get("attribution") or {}).get("kind")
                in ("tracked", "fresh_child")]
        return {"available": True, "suspect": bool(sells), "n_sells": len(sells),
                "lines": [_dx.format_execution_line(h) for h in sells[:3]]}
    except Exception as e:  # noqa: BLE001
        return {"available": False, "reason": f"dex execution leg failed: {str(e)[:80]}"}


def _default_drip_leg(ticker, cfg=None):
    """SPEC-117 live drip/DCA-pattern leg: a DRIP_SELLER hit from a tracked wallet or its
    SPEC-98 fresh-wallet child — cumulative micro-selling that evades SPEC-115's per-swap
    floor by construction. Provider down / untracked token → {available: False} (§3)."""
    try:
        import drip_seller as _dr
        out = _dr.build_drip(ticker)
        if not out.get("available"):
            return {"available": False, "reason": out.get("reason")}
        sells = [h for h in out.get("hits", [])
                if h.get("direction") == _dr.SELL and (h.get("attribution") or {}).get("kind")
                in ("tracked", "fresh_child")]
        return {"available": True, "suspect": bool(sells), "n_hits": len(sells),
                "lines": [_dr.format_drip_line(h) for h in sells[:3]]}
    except Exception as e:  # noqa: BLE001
        return {"available": False, "reason": f"drip leg failed: {str(e)[:80]}"}


def _default_breadth_leg(ticker, cfg=None):
    """SPEC-118 live deposit-breadth leg: a BREADTH_SPIKE (many untracked senders → CEX
    at once) while the tracked top holder is quiet — corroborating rotation evidence,
    deliberately NOT gated on tracked_wallets. Provider down → {available: False} (§3)."""
    try:
        import deposit_breadth as _db
        out = _db.build_breadth(ticker)
        if not out.get("available"):
            return {"available": False, "reason": out.get("reason")}
        return {"available": True, "suspect": bool(out.get("spike")), "senders": out.get("senders"),
                "baseline": out.get("baseline"), "usd": out.get("usd"), "line": out.get("line")}
    except Exception as e:  # noqa: BLE001
        return {"available": False, "reason": f"breadth leg failed: {str(e)[:80]}"}


def build_freshness(ticker, last_out_ts=None, tape=None, onchain=None, dex=None, drip=None,
                    breadth=None, tape_fn=None, onchain_fn=None, dex_fn=None, drip_fn=None,
                    breadth_fn=None, live_short_thesis=None, now=None, state_dir=None,
                    fire=True, event_fn=None, cfg=None):
    """Compose the verdict, persist it, and fire ONE HIGH inbox event on a FROZEN→ROTATED
    transition while a short thesis is live (the "about to bank into a false pause" moment).
    Quota discipline: a FRESH tracked leg answers the question — the evidence legs are only
    evaluated when the tracked wallet is quiet."""
    cfg = cfg or load_cfg()
    now = now if now is not None else time.time()
    ticker = ticker.upper()
    if live_short_thesis is None:
        live_short_thesis = _live_short_thesis(ticker)

    e = _parse_ts(last_out_ts)
    fresh = e is not None and (now - e) <= cfg["fresh_window_h"] * 3600
    if not fresh:                                     # only spend the legs on a quiet clock
        if tape is None:
            tape = tape_fn() if tape_fn else _default_tape_leg(ticker, cfg=cfg)
        if onchain is None:
            onchain = onchain_fn() if onchain_fn else _default_onchain_leg(ticker, cfg=cfg)
        if dex is None:
            dex = dex_fn() if dex_fn else _default_dex_leg(ticker, cfg=cfg)
        if drip is None:
            drip = drip_fn() if drip_fn else _default_drip_leg(ticker, cfg=cfg)
        if breadth is None:
            breadth = breadth_fn() if breadth_fn else _default_breadth_leg(ticker, cfg=cfg)

    r = classify_freshness(last_out_ts, tape, onchain, dex=dex, drip=drip, breadth=breadth, now=now,
                           live_short_thesis=live_short_thesis, cfg=cfg)
    prev = read_state(ticker, state_dir) or {}
    transition = prev.get("verdict") == FROZEN and r["verdict"] == ROTATED
    r["prev_verdict"] = prev.get("verdict")
    r["transition"] = transition
    if transition and live_short_thesis and fire:
        if event_fn is None:
            try:
                import inbox
                event_fn = inbox.append_event
            except Exception:  # noqa: BLE001
                event_fn = None
        if event_fn:
            ts_iso = datetime.fromtimestamp(now, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            try:
                event_fn(ts_iso, ticker, "rotation_freshness", "HIGH",
                         f"{r['line']} — was FROZEN; the pause is FALSE, do not bank into it")
            except Exception:  # noqa: BLE001 — the event must never break the read
                pass
    try:
        p = _state_path(ticker, state_dir)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps({"ticker": ticker, "verdict": r["verdict"], "line": r["line"],
                                 "note": r["note"], "ts": now,
                                 "live_short_thesis": live_short_thesis}))
    except Exception:  # noqa: BLE001 — persistence must never break the read
        pass
    return r


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description="SPEC-98 rotation-aware distribution freshness")
    ap.add_argument("ticker")
    ap.add_argument("--last-out-ts", default=None)
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()
    out = build_freshness(args.ticker, last_out_ts=args.last_out_ts)
    print(json.dumps(out) if args.json else out["line"] + (f"\n  note: {out['note']}" if out.get("note") else ""))
