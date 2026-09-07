#!/usr/bin/env python3
"""risk_card.py — SPEC-169: the one risk line CLAUDE.md §9 (post-GO management,
grill 2026-08-28) says every card must carry:

  tier=<tier> (n=<n_filled>, <total_r>R, trailing-10 <trailing_10_r>R[, DECAYING])
  · max lev <Nx|unknown> (venue) · risk cap <pct>% eq = $<risk_usd> at stop
  · exit-absorbable $<maxsize> · liq-distance <pct>% at <lev>x [· bound=exit|lev|tier]
  [· cluster heat <x>% of <cap>%]

Two layers: `build_risk_line` is a PURE function — every live input (equity, max_lev,
maxsize_exit_usd, cluster_heat_usd) is injected, so it is unit-testable offline with no
network. `live_risk_line` resolves those inputs from the real world (venue_account
equity — SPEC-170, soft-imported since it may not exist yet; config/aster_max_leverage.json;
maxsize.py's Aster exit-side book walk; positions.json cluster-mates) and calls the pure
builder. A capability that can't reach the network still prints a correct line with the
unresolvable fields marked unknown — never a guessed number, never a crash (§0.5).

  python3 capabilities/risk_card.py GALA --signature faded_bounce --direction SHORT \\
      --entry 0.00198 --stop 0.00211 --equity 199 --json
"""
import argparse
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))          # sibling-capability imports (house style)

import ledger as LG

CONFIG_DIR = HERE.parent / "config"

_DEFAULT_SIZING_CFG = {"risk_pct_by_tier": {"hypothesis": 5, "go": 10, "demoted": 5},
                       "cluster_heat_pct": 6, "exit_slippage_pct": 0.5}


def _read_json(path, default=None):
    try:
        return json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return default


def load_sizing_cfg():
    """config/sizing.json, defaults filled in for any missing key. SPEC-169 one-release
    fallback: if `cluster_heat_pct` is absent (sizing.json predates this spec, or is
    missing entirely), fall back to positions.json's now-deprecated
    `max_operator_heat_pct` before the hardcoded default."""
    cfg = dict(_DEFAULT_SIZING_CFG)
    on_disk = _read_json(CONFIG_DIR / "sizing.json", {}) or {}
    if "cluster_heat_pct" not in on_disk:
        pos = _read_json(CONFIG_DIR / "positions.json", {}) or {}
        if "max_operator_heat_pct" in pos:
            cfg["cluster_heat_pct"] = pos["max_operator_heat_pct"]
    cfg.update(on_disk)
    cfg.setdefault("risk_pct_by_tier", _DEFAULT_SIZING_CFG["risk_pct_by_tier"])
    return cfg


def _fmt_r(v):
    return f"{v:g}" if v is not None else "n/a"


def _fmt_usd(v):
    return f"${v:,.0f}" if v is not None else "unknown"


# ── pure line assembly (no network) ─────────────────────────────────────────────────
def build_risk_line(ticker, signature, direction, entry=None, stop=None,
                    equity=None, equity_source=None, equity_reason=None, max_lev=None,
                    maxsize_exit_usd=None, cluster_heat_usd=None, sizing_cfg=None,
                    oi_construction=None):
    """Every live-data input is a plain argument — no capability call happens in here.
    Returns {"line": <str>, ...the fields the line was built from}, so a caller can both
    print the line and inspect the numbers (e.g. tests, or a JSON card).

    SPEC-180 req 4 (§7 GO-ceiling condition): the GO-tier risk cap (10%) requires
    `oi_construction.verdict != UNKNOWN` AND `gating_ok=true` AT THIS COMMIT — else
    the card caps this commit's risk_pct at the hypothesis ceiling, reason printed.
    Hypothesis/demoted-tier commits are UNCHANGED by `oi_construction` (annotates
    only there, never haircuts — the ceiling condition only gates the GO cap)."""
    sizing_cfg = sizing_cfg or load_sizing_cfg()
    sig = LG.canon_signature(signature, direction=direction) if signature else "discretionary"
    tier_row = LG.stats(signature=sig)
    tier = tier_row.get("tier") or "hypothesis"
    n_filled = tier_row.get("n_filled") or 0
    total_r = tier_row.get("total_r") or 0.0
    trailing_10_r = tier_row.get("trailing_10_r")
    tier_flag = tier_row.get("tier_flag")

    risk_pct = sizing_cfg.get("risk_pct_by_tier", {}).get(tier, 5)
    go_ceiling_note = None
    if tier == "go":
        oic_verdict = (oi_construction or {}).get("verdict")
        oic_gating_ok = bool(oi_construction and oi_construction.get("gating_ok"))
        if oic_verdict in (None, "UNKNOWN") or not oic_gating_ok:
            hyp_pct = sizing_cfg.get("risk_pct_by_tier", {}).get("hypothesis", 5)
            if risk_pct != hyp_pct:
                go_ceiling_note = (f"GO-ceiling not met (oi_construction verdict="
                                   f"{oic_verdict or 'UNKNOWN'}, gating_ok={oic_gating_ok}) — "
                                   f"capped at hypothesis {hyp_pct:g}% for this commit")
                risk_pct = hyp_pct

    stop_distance_pct = None
    if entry is not None and stop is not None and float(entry) != 0:
        stop_distance_pct = abs(float(stop) - float(entry)) / float(entry) * 100.0

    risk_usd = round(float(equity) * risk_pct / 100.0, 2) if equity is not None else None

    notional_usd = None
    if risk_usd is not None and stop_distance_pct:
        notional_usd = round(risk_usd / (stop_distance_pct / 100.0), 2)

    caps = {}
    if notional_usd is not None:
        caps["tier"] = notional_usd
    if maxsize_exit_usd is not None:
        caps["exit"] = maxsize_exit_usd
    if equity is not None and max_lev is not None:
        caps["lev"] = float(equity) * float(max_lev)

    bound, sized_usd = (None, None)
    if caps:
        bound = min(caps, key=caps.get)
        sized_usd = round(caps[bound], 2)

    liq_distance_pct = round(100.0 / float(max_lev), 2) if max_lev else None

    tier_bits = f"tier={tier} (n={n_filled}, {total_r:g}R, trailing-10 {_fmt_r(trailing_10_r)}R"
    if tier_flag:
        tier_bits += f", {tier_flag.upper()}"
    tier_bits += ")"

    lev_bit = f"max lev {max_lev:g}x (venue)" if max_lev is not None else "max lev unknown (venue)"

    if equity is None:
        risk_bit = (f"risk cap n/a (equity=unknown: {equity_reason})" if equity_reason
                   else "risk cap n/a (equity=unknown)")
    elif risk_usd is not None:
        risk_bit = f"risk cap {risk_pct:g}% eq = ${risk_usd:,.0f} at stop"
    else:
        risk_bit = f"risk cap {risk_pct:g}% eq = n/a at stop"

    exit_bit = f"exit-absorbable {_fmt_usd(maxsize_exit_usd)}"

    if liq_distance_pct is not None:
        liq_bit = f"liq-distance {liq_distance_pct:g}% at {max_lev:g}x"
    else:
        liq_bit = "liq-distance unknown"

    parts = [tier_bits, lev_bit, risk_bit, exit_bit, liq_bit]
    if bound:
        parts.append(f"bound={bound}")
    if go_ceiling_note:
        parts.append(go_ceiling_note)

    cluster_pct = None
    if cluster_heat_usd is not None and equity:
        cluster_pct = round(cluster_heat_usd / float(equity) * 100.0, 2)
        cap_pct = sizing_cfg.get("cluster_heat_pct", 6)
        parts.append(f"cluster heat {cluster_pct:g}% of {cap_pct:g}%")

    return {
        "line": " · ".join(parts),
        "ticker": (ticker or "").upper(), "signature": sig, "tier": tier,
        "n_filled": n_filled, "total_r": total_r, "trailing_10_r": trailing_10_r,
        "tier_flag": tier_flag, "risk_pct": risk_pct, "risk_usd": risk_usd,
        "stop_distance_pct": stop_distance_pct, "notional_usd": notional_usd,
        "maxsize_exit_usd": maxsize_exit_usd, "max_lev": max_lev,
        "equity": equity, "equity_source": equity_source, "equity_reason": equity_reason,
        "bound": bound, "sized_usd": sized_usd, "liq_distance_pct": liq_distance_pct,
        "cluster_heat_usd": cluster_heat_usd, "cluster_heat_pct": cluster_pct,
        "go_ceiling_note": go_ceiling_note,   # SPEC-180 req 4
    }


# ── live input resolution (network / disk; every leg best-effort) ──────────────────
def _classify_equity_failure(exc=None, resp=None):
    """SPEC-191 #3: a mandatory, named reason for every equity=unknown line — one of
    no_key / http_<code> / timeout / import_error, or the venue's own error string when
    it doesn't fit a cleaner bucket. Never returns None (the caller only calls this once
    it knows equity is unresolved, so silence would be a data failure masquerading as
    a datum — §3)."""
    if exc is not None:
        if isinstance(exc, TimeoutError):
            return "timeout"
        return f"error:{type(exc).__name__}: {str(exc)[:100]}"
    if isinstance(resp, dict):
        err = resp.get("error")
        if err == "aster_key_missing":
            return "no_key"
        status = resp.get("http_status")
        if status:
            return f"http_{status}"
        if err and "timed out" in str(err).lower():
            return "timeout"
        if err:
            return str(err)[:120]
    return "unknown_error"


def resolve_equity(equity_arg=None):
    """SPEC-170's venue read wins when available; else the --equity arg; else unknown.
    `venue_account` may not exist yet (pre-SPEC-170) — a missing module, or any failure
    reading it, degrades straight to the arg/unknown path, never raises.

    Returns (equity, source, reason) — SPEC-191 #3: `reason` is populated ONLY when the
    final resolved equity is None (venue failed AND no arg fallback available); a
    successful arg fallback clears it, since the caller got a usable number. Historical
    bug this fixes: `venue_account.equity()` actually returns `{"ok": True, "data":
    {"perp_total_value_usd": ...}}` — the old code read `.get("perp_total_value_usd")`
    off the TOP level, which is always None on the real response shape, so the venue
    read silently never won even when the key was live and the venue answered in <2s."""
    reason = None
    try:
        import venue_account as VA
    except Exception:  # noqa: BLE001
        VA = None
        reason = "import_error"
    if VA is not None:
        try:
            eq = VA.equity()
        except Exception as e:  # noqa: BLE001
            reason = _classify_equity_failure(exc=e)
            eq = None
        else:
            if isinstance(eq, dict) and eq.get("ok"):
                val = (eq.get("data") or {}).get("perp_total_value_usd")
                if isinstance(val, (int, float)):
                    return float(val), "venue", None
                reason = "malformed_response"
            else:
                reason = _classify_equity_failure(resp=eq if isinstance(eq, dict) else None)
    if equity_arg is not None:
        return float(equity_arg), "arg", None
    return None, None, reason


def resolve_max_lev(ticker, max_lev_arg=None):
    if max_lev_arg is not None:
        return float(max_lev_arg)
    d = _read_json(CONFIG_DIR / "aster_max_leverage.json", {}) or {}
    sym = f"{(ticker or '').upper()}USDT"
    entry = d.get(sym) or d.get((ticker or "").upper())
    if isinstance(entry, dict):
        v = entry.get("max_lev")
        return float(v) if isinstance(v, (int, float)) else None
    if isinstance(entry, (int, float)):
        return float(entry)
    return None


def resolve_maxsize_exit_usd(ticker, direction, exit_slippage_pct, fetch_fn=None):
    """SPEC-169 req 3: `maxsize.py` on the EXIT side at `exit_slippage_pct`, restricted
    to the Aster book (§7: Aster is the execution venue — the exit-absorbable figure must
    reflect what's actually fillable there, not a cross-venue routing recommendation).
    `fetch_fn(ticker, side, bps) -> usd|None` is injectable for tests; the default wraps
    `maxsize.build_maxsize`, best-effort (a dead book/network failure -> None, never
    raises)."""
    fn = fetch_fn
    if fn is None:
        try:
            import maxsize as MS
        except Exception:  # noqa: BLE001
            return None

        def fn(tk, side, bps):
            d = MS.build_maxsize(tk, side=side, leg="exit", bps=bps, venue="aster")
            v = (d.get("venues") or {}).get("aster")
            return v.get("max_clean_usd") if v else None
    try:
        return fn(ticker, (direction or "").lower(), float(exit_slippage_pct) * 100.0)
    except Exception:  # noqa: BLE001
        return None


def _position_risk_usd(pos):
    """Best-effort risk-at-stop $ for one config/positions.json open-position row.
    Real rows carry wildly different shapes (see the file's own history) — try the
    explicit fields first, then derive from entry/stop + notional (or size_usd*lev).
    None on any unresolvable shape; never guesses a number onto the cluster sum."""
    for k in ("risk_usd", "usd_at_stop"):
        v = pos.get(k)
        if isinstance(v, (int, float)):
            return abs(float(v))
    entry, stop = pos.get("entry"), pos.get("stop")
    if entry is None or stop is None or not entry:
        return None
    try:
        stop_dist_pct = abs(float(stop) - float(entry)) / float(entry)
    except (TypeError, ValueError, ZeroDivisionError):
        return None
    notional = pos.get("notional_usd")
    if notional is None:
        size_usd, lev = pos.get("size_usd"), pos.get("leverage")
        try:
            lev = float(str(lev).rstrip("xX")) if lev is not None else None
        except (TypeError, ValueError):
            lev = None
        if size_usd is not None and lev is not None:
            try:
                notional = float(size_usd) * lev
            except (TypeError, ValueError):
                notional = None
    if notional is None:
        return None
    try:
        return round(stop_dist_pct * float(notional), 2)
    except (TypeError, ValueError):
        return None


def resolve_cluster_heat_usd(ticker, this_trade_risk_usd=None, positions_path=None):
    """SPEC-169 req 4: "in the same line when the name is in a cluster with ANOTHER open
    position" — SUM of risk-at-stop across OPEN positions.json rows sharing an auto-built
    operator cluster (classify.cluster_heat_for, SPEC-99) with `ticker`, plus this
    trade's own risk_usd. None whenever the condition isn't met: no cluster, or a cluster
    with no OTHER open cluster-mate position — a solo name never gets a "cluster heat"
    line just because it has its own risk (that's what risk_usd already says)."""
    try:
        import classify as CL
    except Exception:  # noqa: BLE001
        return None
    try:
        ch = CL.cluster_heat_for(ticker)
    except Exception:  # noqa: BLE001
        return None
    members = {m.upper() for m in (ch or {}).get("members") or []}
    if not members:
        return None
    pos_path = Path(positions_path) if positions_path else (CONFIG_DIR / "positions.json")
    pdata = _read_json(pos_path, {}) or {}
    total = float(this_trade_risk_usd) if this_trade_risk_usd is not None else 0.0
    any_mate = False
    for p in pdata.get("positions") or []:
        tk = (p.get("ticker") or "").upper()
        if tk == (ticker or "").upper() or tk not in members:
            continue
        r = _position_risk_usd(p)
        if r is not None:
            total += r
            any_mate = True
    if not any_mate:
        return None
    return round(total, 2)


def resolve_oi_construction(ticker):
    """SPEC-180 req 4: the GO-ceiling condition's live input. Best-effort — a dead/slow
    sweep or a missing module degrades to None (the card reads that as "not met",
    capping GO at the hypothesis ceiling — never a crash, never a fabricated verdict)."""
    try:
        import oi_construction as OC
    except Exception:  # noqa: BLE001
        return None
    try:
        return OC.build_oi_construction(ticker)
    except Exception:  # noqa: BLE001
        return None


def live_risk_line(ticker, signature, direction, entry=None, stop=None, equity_arg=None,
                   max_lev_arg=None, exit_slippage_pct=None, sizing_cfg=None,
                   resolve_maxsize=True, resolve_oic=False):
    """Resolves every live input (best-effort, never raises) then calls the pure
    `build_risk_line`. This is what scan/brief/classify card renderers call.

    `resolve_maxsize=False` (SPEC-169): skip the maxsize.py exit-side book walk — the
    only network-heavy leg here (equity/max_lev are local-config/no-network). A board
    renderer looping over N candidates (e.g. scan --mode faded_bounce, which runs
    unattended on ops/discovery_tick.sh's cadence) should pass False to avoid N live
    book walks per tick; a single-ticker human-triggered read (brief) leaves it on.

    `resolve_oic=False` (default, SPEC-180): the GO-ceiling condition's oi_construction
    sweep is a FULL venue_map fan-out (~15 venues) — opt-in, not automatic, for the same
    board-renderer-cost reason as `resolve_maxsize`. A caller that wants the live
    GO-ceiling check (a single-ticker deep read) passes True."""
    sizing_cfg = sizing_cfg or load_sizing_cfg()
    equity, equity_source, equity_reason = resolve_equity(equity_arg)
    max_lev = resolve_max_lev(ticker, max_lev_arg)
    maxsize_exit_usd = None
    if resolve_maxsize:
        slip = exit_slippage_pct if exit_slippage_pct is not None else sizing_cfg.get("exit_slippage_pct", 0.5)
        maxsize_exit_usd = resolve_maxsize_exit_usd(ticker, direction, slip)
    oi_construction = resolve_oi_construction(ticker) if resolve_oic else None
    row = build_risk_line(ticker, signature, direction, entry=entry, stop=stop,
                          equity=equity, equity_source=equity_source, equity_reason=equity_reason,
                          max_lev=max_lev, maxsize_exit_usd=maxsize_exit_usd, sizing_cfg=sizing_cfg,
                          oi_construction=oi_construction)
    cluster_heat_usd = resolve_cluster_heat_usd(ticker, this_trade_risk_usd=row.get("risk_usd"))
    if cluster_heat_usd is not None:
        row = build_risk_line(ticker, signature, direction, entry=entry, stop=stop,
                              equity=equity, equity_source=equity_source, equity_reason=equity_reason,
                              max_lev=max_lev, maxsize_exit_usd=maxsize_exit_usd,
                              cluster_heat_usd=cluster_heat_usd, sizing_cfg=sizing_cfg,
                              oi_construction=oi_construction)
    return row


def main():
    ap = argparse.ArgumentParser(description="SPEC-169 risk-card line — read-only")
    ap.add_argument("ticker")
    ap.add_argument("--signature", default="discretionary")
    ap.add_argument("--direction", default=None, help="LONG|SHORT")
    ap.add_argument("--entry", type=float, default=None)
    ap.add_argument("--stop", type=float, default=None)
    ap.add_argument("--equity", type=float, default=None,
                    help="fallback equity $ when the venue read is unavailable")
    ap.add_argument("--max-lev", dest="max_lev", type=float, default=None)
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()
    row = live_risk_line(args.ticker, args.signature, args.direction, entry=args.entry,
                         stop=args.stop, equity_arg=args.equity, max_lev_arg=args.max_lev)
    if args.json:
        print(json.dumps(row))
    else:
        print(row["line"])


if __name__ == "__main__":
    main()
