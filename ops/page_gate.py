#!/usr/bin/env python3
"""page_gate.py — SPEC 66: per-wallet page cooldown for the surveil notification layer.

BILL paged 6x in one afternoon — the same apparatus wallets re-fire every sweep because
they are genuinely active every interval, and an alert that repeats hourly trains the
human to ignore alerts. This gate keeps state/page_cooldowns.json keyed by wallet address:
a wallet that already PAGED within N hours (default 6) logs but does not re-notify, UNLESS
  (a) the new fire is a TIER UPGRADE — token_out reaching a higher-rank destination than
      the one it last paged on (e.g. staging-internal → cex-execution), or
  (b) it is the wallet's FIRST fire after >24h quiet (a notable re-emergence).
The LOG line is always written by the caller (surveil.sh) — this gate throttles only the
PAGE. Pure decision over an injectable clock + state path so tests run offline.

CLI (surveil.sh wiring):
  echo '<orchestrator onchain_board envelope json>' | python3 ops/page_gate.py gate
    -> prints 'PAGE' or 'SUPPRESS' on the first line, then a per-wallet decision summary.
"""
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
STATE = ROOT / "state"
COOLDOWN_PATH = STATE / "page_cooldowns.json"

DEFAULT_COOLDOWN_HOURS = 6
QUIET_HOURS = 24   # a first fire after this much silence pierces the cooldown

# Destination tiers ranked by escalation severity (memory: §8 — token_out to a CEX is the
# real exit; staging-internal is upstream positioning). A rank INCREASE vs the last paged
# rank is a tier upgrade and pierces the cooldown.
_DEST_RANK = {
    None: 0, "": 0,
    "tracked-safe": 1, "internal": 1,
    "staging-internal": 2, "staging-sink": 2, "staging": 2,
    "dex-execution": 3,
    "cex-execution": 4, "cex": 4,
    # SPEC-93: funding-surveil reuses this throttle keyed by ticker; the deep-neg band tier
    # is the "dest" so a WATCH→HIGH deepen pierces the cooldown as a tier-upgrade.
    "watch": 1, "high": 2,
}


def _rank(dest_kind):
    return _DEST_RANK.get((dest_kind or "").lower(), 1)


def _iso(now):
    return now.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_ts(s):
    if not s:
        return None
    try:
        return datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except Exception:
        return None


def _read(state_path):
    try:
        return json.loads(Path(state_path).read_text())
    except Exception:
        return {}


def _write(state_path, d):
    p = Path(state_path)
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(d, indent=2))
    except Exception:
        pass   # the gate must never break the sweep that drives it


def decide(fire, state, now, cooldown_hours=DEFAULT_COOLDOWN_HOURS):
    """Pure decision for one fired wallet against the persisted state dict.

    Returns (page: bool, reason: str, new_state_entry: dict). Does NOT mutate `state`.
    """
    key = (fire.get("address") or fire.get("label") or "").lower()
    cur_rank = _rank(fire.get("dest_kind"))
    prior = state.get(key) or {}
    last_page = _parse_ts(prior.get("last_page_ts"))
    paged_rank = int(prior.get("paged_dest_rank", -1))

    if last_page is None:
        page, reason = True, "first-fire"
    elif cur_rank > paged_rank >= 0:
        page, reason = True, f"tier-upgrade(rank {paged_rank}->{cur_rank})"
    else:
        elapsed_h = (now - last_page).total_seconds() / 3600.0
        if elapsed_h >= QUIET_HOURS:
            page, reason = True, "re-emergence-after-quiet"
        elif elapsed_h >= cooldown_hours:
            page, reason = True, f"cooldown-elapsed({elapsed_h:.1f}h)"
        else:
            page, reason = False, f"cooldown-active({elapsed_h:.1f}h<{cooldown_hours}h)"

    if page:
        entry = {"last_page_ts": _iso(now), "paged_dest_rank": cur_rank,
                 "last_dest_kind": fire.get("dest_kind"), "label": fire.get("label")}
    else:
        # keep the last PAGE markers; only refresh the last-seen breadcrumb
        entry = dict(prior)
        entry["last_seen_ts"] = _iso(now)
    return page, reason, entry


def should_page(fire, now=None, state_path=COOLDOWN_PATH, cooldown_hours=DEFAULT_COOLDOWN_HOURS):
    """Decide + persist for a single fire. Returns True iff this fire should PAGE."""
    now = now or datetime.now(timezone.utc)
    state = _read(state_path)
    key = (fire.get("address") or fire.get("label") or "").lower()
    page, _reason, entry = decide(fire, state, now, cooldown_hours=cooldown_hours)
    state[key] = entry
    _write(state_path, state)
    return page


def gate_envelope(envelope, now=None, state_path=COOLDOWN_PATH, cooldown_hours=DEFAULT_COOLDOWN_HOURS):
    """Gate an orchestrator onchain_board envelope (alerts[].escalation_fired[]).

    Returns (page_overall, decisions) where decisions is a list of per-wallet records
    {ticker,label,address,dest_kind,page,reason}. page_overall is True iff ANY wallet
    pierced the cooldown — every wallet is still REPORTED (the log is complete; only the
    PAGE is throttled). State is updated once for all fires in this sweep.
    """
    now = now or datetime.now(timezone.utc)
    data = (envelope or {}).get("data", envelope) or {}
    state = _read(state_path)
    decisions, page_overall = [], False
    for alert in data.get("alerts", []) or []:
        ticker = alert.get("ticker")
        for fire in alert.get("escalation_fired", []) or []:
            key = (fire.get("address") or fire.get("label") or "").lower()
            page, reason, entry = decide(fire, state, now, cooldown_hours=cooldown_hours)
            state[key] = entry
            page_overall = page_overall or page
            decisions.append({"ticker": ticker, "label": fire.get("label"),
                              "address": fire.get("address"), "dest_kind": fire.get("dest_kind"),
                              "page": page, "reason": reason})
    _write(state_path, state)
    return page_overall, decisions


def _cli_gate(cooldown_hours=DEFAULT_COOLDOWN_HOURS):
    try:
        env = json.loads(sys.stdin.read() or "{}")
    except Exception as e:   # noqa: BLE001
        print("SUPPRESS")
        print(f"  (unparseable envelope: {str(e)[:80]})", file=sys.stderr)
        return 0
    page, decisions = gate_envelope(env, cooldown_hours=cooldown_hours)
    print("PAGE" if page else "SUPPRESS")
    for d in decisions:
        mark = "🔔" if d["page"] else "🔕"
        print(f"  {mark} ${d['ticker']} {d['label']} [{d['dest_kind']}] — {d['reason']}",
              file=sys.stderr)
    return 0


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "gate":
        # SPEC-154: --cooldown-hours lets a caller with a different repeat-page tolerance
        # (funding_surveil.sh wants 24h, not surveil.sh's 6h wallet-activity default) share
        # this same throttle without forking it.
        cd = DEFAULT_COOLDOWN_HOURS
        if len(sys.argv) > 3 and sys.argv[2] == "--cooldown-hours":
            try:
                cd = float(sys.argv[3])
            except ValueError:
                pass
        sys.exit(_cli_gate(cooldown_hours=cd))
    print("usage: page_gate.py gate [--cooldown-hours N]  (reads onchain_board envelope on stdin)",
          file=sys.stderr)
    sys.exit(2)
