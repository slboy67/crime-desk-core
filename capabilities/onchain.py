#!/usr/bin/env python3
"""onchain.py — the on-chain analyser (NATIVE, the desk's spine).

Phase-2 rebuild per handoffs/phase2-onchain-analyser.md. On-chain is the spine of
this desk (memory: project_onchain_is_the_spine_perp_came_second); the live top on a
§4 vesting-hedge name is a mega-safe NONCE tick, not a getLogs lifetime audit
(memory: feedback_live_top_signal_is_nonce_not_getlogs_audit — the LAB $21→$7.46 miss).

So the SPINE is the cheap nonce read (eth_getTransactionCount per tracked safe, ~5s
for 28 wallets), diffed against a committed baseline → the bid-pull / distribution
signal. The heavy lifetime layers are budgeted best-effort and NEVER block the verdict.

  build_nonce_state(ticker)  -> the fast live signal (also used by analyse, <30s, SPEC 5)
  build_onchain(ticker)      -> the composed whole-coin picture (SPEC 7), never hangs.
                                SPEC-72: when a tracked safe FIRED (live diff OR a consumed
                                nonce_surveil HIGH alert), resolves its flow (verify_wallet path)
                                + one bounded chain into the operator aggregator's dex_swap_sell
                                USDT leg → DISTRIBUTING/STAGING, never DORMANT/NEUTRAL-CONSTRUCTIVE.
                                SPEC-73: only a LIVE fire (≤6h, TOKEN_OUT_WINDOW_SEC) flips the
                                signal; a fire >6h but <24h old reverts the live signal to DORMANT
                                yet still carries a `recent_distribution` 24h-memory surface +
                                a RECENT-DISTRIBUTION bias (the BSB $5.1M/24h read @ T+8h).
                                SPEC-75: the per-wallet DISTRIBUTION-FIRING attribution is gated on
                                a CONFIRMED tracked-token OUT this window — a nonce-only fire
                                (n_out=0, the BILL MM-PROG-60K 48-nonce churn on non-token activity)
                                is demoted to a `nonce_only_fired` bucket, never the lead; the
                                headline ranks by token-out magnitude; if NO fired safe confirms a
                                token-out the signal downgrades to NONCE-ONLY-WATCH.

  python3 capabilities/onchain.py LAB
  python3 capabilities/onchain.py LAB --json
"""
import argparse
import importlib.util
import json
import os
import re
import sys
import tempfile
import threading
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(HERE))
import colors as C
import moralis_quota   # SPEC 66: per-UTC-day Moralis call meter
import provider_quota  # SPEC-97: per-provider generalization (etherscan etc.)
import local_index      # SPEC-102: tracked-set local SQLite index (read half, no network)
from wallet_state import build_snapshot   # the concurrent live nonce+gas read

STATE = ROOT / "state"
WALLETS = ROOT / "config" / "tracked_wallets.json"
VC_ENTITIES = ROOT / "config" / "vc_entities.json"
KNOWN_ENTITIES = ROOT / "config" / "known_entities.json"

# Tiers EXCLUDED from the escalation signal — they tick constantly and are noise:
#   operational/MM hot wallets, and exchange-side wallets (a CEX/DEX hot wallet firing
#   is not an operator mega-safe move). The meaningful §8 escalation is an operator-
#   controlled safe (team/distribution/treasury/mega) going from dormant → fired.
OP_TIERS = {"op", "hot", "mm", "mm-hot", "operational", "cex", "exchange", "dex", "router", "cex-hot"}
# SPEC 55: untiered candidates (freshly onboarded, the Designer's tiering pass pending)
# fire constantly as their baseline settles. They are NOT a §8 escalation until tiered —
# an all-untiered batch must read BASELINE_SETTLING, never ESCALATION.
UNTIERED_TIERS = {"", "unclassified", "untiered", "candidate", "pending"}
NONCE_BUDGET = 30          # SPEC 5 — the live on-chain read must finish well under this


def _baseline_path(ticker):
    return STATE / f"nonce_baseline_{ticker.upper()}.json"


def _load_baseline(ticker):
    try:
        return json.loads(_baseline_path(ticker).read_text())
    except Exception:
        return None


BASELINE_STALE_SEC = 6 * 3600   # SPEC 9: a delta vs a baseline older than this is catch-up, not a fresh fire


def _save_baseline(ticker, rows, ts, seeded_ts):
    # ATOMIC write (SPEC 30): `brief` runs analyse + onchain concurrently and BOTH advance
    # the same per-ticker baseline file. A plain write_text can be read mid-write → a torn,
    # unparseable baseline → a mis-fired/missed ESCALATION. mkstemp gives a unique temp per
    # writer (collision-safe even for same-PID threads); os.replace swaps it in atomically.
    try:
        STATE.mkdir(parents=True, exist_ok=True)
        payload = json.dumps({
            "ts": ts, "seeded_ts": seeded_ts,
            "nonces": {r["address"]: r["nonce"] for r in rows if r.get("nonce") is not None},
        })
        p = _baseline_path(ticker)
        fd, tmp = tempfile.mkstemp(dir=str(STATE), prefix=f".{p.stem}.", suffix=".tmp")
        try:
            with os.fdopen(fd, "w") as fh:
                fh.write(payload)
            os.replace(tmp, str(p))
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)
    except Exception:
        pass


def _confidence(label):
    """High-confidence = a mapped operator safe; low = an UNMAPPED/feeder wallet."""
    lab = (label or "").upper()
    return "low" if (not lab or lab.startswith("UNMAPPED")) else "high"


# SPEC 24: a nonce increment proves the WALLET transacted, NOT that it moved the TRACKED TOKEN.
# On ultra-high-frequency CEX-MM EOAs the nonce churns constantly regardless of the token, so a
# nonce-delta escalation is noise. For these, require a recent tracked-token OUT to confirm.
HIGH_NONCE_THRESHOLD = 1_000_000      # lifetime nonce ≥ this → nonce-delta is structurally noisy for token attribution
TOKEN_OUT_WINDOW_SEC = 6 * 3600       # tracked-token OUT must be within this to confirm a nonce-escalation


def _parse_ts(ts_iso):
    """ISO-8601 (…Z) → epoch seconds, or None."""
    if not ts_iso:
        return None
    try:
        return datetime.fromisoformat(ts_iso.replace("Z", "+00:00")).timestamp()
    except (ValueError, AttributeError):
        return None


def classify_destination(kind, tier=None):
    """SPEC 28: label a distributor's OUT destination — cex-execution (tokens → a CEX entity = the
    real SELL / the dump firing) vs staging-internal (→ an operator-controlled sink = consolidation,
    apparatus loading) vs dex-execution vs unknown. `kind` is from verify_wallet._classify_addr.
    SPEC-94: a flow to a shared DEX router/AMM-hook contract (0x238a Pancake-Infinity) is routing
    PASS-THROUGH (`dex-router`) — plumbing every BSC token traverses — NEITHER the operator dump
    nor internal staging. It must precede the tracked-safe branch (a router tracked at tier `dex`/
    `router` would otherwise misread as staging-internal, the tier-`op` bug this ticket fixes)."""
    tier = (tier or "").lower()
    if kind == "cex" or tier in {"cex", "exchange", "cex-hot", "hot"}:
        return "cex-execution"
    if kind in {"dex-router", "router"} or tier in {"dex", "router"}:
        return "dex-router"
    if kind == "tracked-safe":
        return "staging-internal"
    if kind == "dex":
        return "dex-execution"
    return "unknown"


def _qualifies_as_staging_sink(v):
    """SPEC 28: a discovered address is a STAGING SINK (apparatus loading, not the dump) if it's
    SEEDED by a tracked operator wallet AND accumulating/holding — not yet routing to a CEX. Such a
    sink is where the real dump will ORIGINATE, so it must enter nonce-watch."""
    if not v.get("available") or not v.get("seeded_staging"):
        return False
    return v.get("out_count", 0) == 0 or v.get("distribution_mode") in (None, "staging-internal")


def onboard_lint(address, chain="binance-smart-chain", contract_ok=False, probe=None):
    """SPEC-67: gate a tracked_wallets addition against on-chain facts. An address that
    probes as a DEX pool — or any contract — must NOT be silently tagged as a safe/hub
    (that is exactly how 0x5bb59bb9 became "TWAP-HUB-6.8M" and poisoned funded_by). When
    the address is a contract, require an explicit `contract_ok=True`; a pool is rejected
    outright unless acknowledged. Returns {ok, reason, probe, note}. A probe that can't
    read the bytecode does NOT block (degrade-explicit: we never invent a verdict)."""
    pf = probe or probe_contract
    try:
        p = pf(address, chain)
    except Exception as e:  # noqa: BLE001
        p = {"available": False, "reason": f"probe error: {str(e)[:80]}"}
    note = None
    if p.get("is_pool"):
        note = (f"SPEC-67 pool probe: DEX pool token0={p.get('token0')} token1={p.get('token1')} "
                f"fee={p.get('fee')} — NOT a safe/hub")
        if not contract_ok:
            return {"ok": False, "reason": "address is a DEX pool (contract_ok required)",
                    "probe": p, "note": note}
    elif p.get("is_contract"):
        note = "SPEC-67 probe: address is a contract (not a pool) — confirm before tiering as a safe"
        if not contract_ok:
            return {"ok": False, "reason": "address is a contract (contract_ok required)",
                    "probe": p, "note": note}
    return {"ok": True, "reason": None, "probe": p, "note": note}


def onboard_staging_sink(token, address, chain="binance-smart-chain", label=None,
                         subtag="staging-sink", tier="distribution", wallets_path=None,
                         contract_ok=False, probe=None):
    """SPEC 28: auto-onboard a discovered staging sink into tracked_wallets.json (additive, dedup) so
    it enters nonce-watch and the board can catch its first CEX-bound (execution) out. Returns True
    if newly added. SPEC-67: linted — a contract/pool address is rejected (returns False) unless
    contract_ok=True; the pool probe is recorded in the entry's _note when one is added anyway."""
    path = Path(wallets_path) if wallets_path else WALLETS
    d = json.loads(path.read_text())
    tok = d.get("tokens", {}).get(token.upper())
    if not tok:
        return False
    if any(w["address"].lower() == address.lower() for w in tok.get("wallets", [])):
        return False
    lint = onboard_lint(address, chain, contract_ok=contract_ok, probe=probe)
    if not lint["ok"]:
        return False
    note = ("SPEC28: auto-onboarded staging sink (seeded + accumulating). Nonce-watched for the "
            "first execution-bound (CEX) out = the dump firing.")
    if lint.get("note"):
        note = f"{note} | {lint['note']}"
    tok.setdefault("wallets", []).append({
        "label": label or f"AUTO-STAGING-SINK-{address[:10]}", "address": address,
        "chain": chain, "tier": tier, "subtag": subtag, "_note": note})
    path.write_text(json.dumps(d, indent=1, ensure_ascii=False))
    return True


def _last_token_out(address, contract, chain_key, decimals=18, days=7):
    """SPEC 24/28: most-recent tracked-token OUT (timestamp + destination) for a wallet. Reuses the
    SPEC-17 provider pool + 90s cache. Returns (last_out_ts_iso | None, last_out_dest | None,
    available). available=False ⇒ provider down (degrade explicitly, NOT 'no token-out')."""
    if not contract or chain_key not in _MORALIS_CHAIN:
        return None, None, False
    try:
        txs, _src, _partial = token_transfers(address, contract, chain_key, days=days, decimals=decimals)
    except Exception:  # noqa: BLE001 — all providers failed (SPEC 17) → unavailable, not "0 outs"
        return None, None, False
    a = address.lower()
    outs = [t for t in txs if (t.get("from_address") or "").lower() == a and t.get("block_timestamp")]
    if not outs:
        return None, None, True
    last = max(outs, key=lambda t: t["block_timestamp"])
    return last["block_timestamp"], ((last.get("to_address") or "").lower() or None), True


def build_nonce_state(ticker, ts=None, persist=False):
    """The fast live signal: live nonce snapshot diffed vs the PERSISTED baseline (SPEC 9).

    signal ∈ ESCALATION | BASELINE_SETTLING | LOADING | DORMANT | QUIET ; score in converge
    sign-convention (negative = distribution pressure). ESCALATION = a mapped, normally-
    dormant operator safe firing vs a recent baseline — never a cex/operational/untiered
    fire (SPEC 55). baseline_seeded is true once a snapshot exists; baseline_age/
    baseline_stale degrade confidence on a cold baseline; UNMAPPED feeder ticks are
    low-confidence (not a Stage-5 trigger on their own).

    SPEC 55: a plain READ does NOT advance the committed baseline — it only seeds one if
    none exists. Every read advancing the baseline meant brief and a direct onchain call
    in the same minute diffed against DIFFERENT baselines and disagreed (PLAY
    2-fired-then-DORMANT, UAI). With `persist=False` (the default for reads) consecutive
    reads return the SAME verdict until the SURVEILLANCE SWEEP (build_board, persist=True)
    consumes the fire by advancing the baseline. §0.5: a read is a read, not a commit."""
    ticker = ticker.upper().replace("USDT", "")
    now = ts if ts is not None else time.time()
    t0 = time.time()
    snap = build_snapshot(ticker)
    ms = int((time.time() - t0) * 1000)
    if not snap.get("tracked"):
        return {"ticker": ticker, "tracked": False, "signal": "UNTRACKED", "score": 0,
                "ms": ms, "baseline_seeded": False, "fired_count": 0,
                "primed_unfired_count": 0, "newly_fired": [], "escalation_fired": [],
                "wallets_total": 0, "wallets_read": 0, "wallets_unreadable": 0, "unreadable": []}

    base = _load_baseline(ticker)
    had_baseline = base is not None
    base_nonces = (base or {}).get("nonces", {})
    base_ts = (base or {}).get("ts")
    seeded_ts = (base or {}).get("seeded_ts") or now   # preserve original seed time across updates
    baseline_age = (now - base_ts) if (had_baseline and base_ts) else None
    baseline_stale = bool(baseline_age is not None and baseline_age > BASELINE_STALE_SEC)

    # SPEC 24: the token contract for the tracked-token-out confirmation (BSC = the live chain).
    tok = json.loads(WALLETS.read_text()).get("tokens", {}).get(ticker, {})
    contract_bsc = (tok.get("contracts", {}) or {}).get("binance-smart-chain")
    decimals = tok.get("decimals", 18)

    newly_fired = []
    for w in snap["wallets"]:
        if not w.get("rpc_ok") or w.get("nonce") is None:
            continue
        prev = base_nonces.get(w["address"])
        if prev is not None and w["nonce"] > prev:
            tier_l = (w["tier"] or "").lower()
            op = tier_l in OP_TIERS
            # SPEC 55: tier decides whether a fire can set the headline. operational/cex →
            # "noise"; an UNTIERED candidate → "untiered" (settling, gated behind the
            # Designer's tiering pass); a mapped operator safe → "high"/"low" by label.
            conf = ("noise" if op else
                    "untiered" if tier_l in UNTIERED_TIERS else
                    _confidence(w["label"]))
            newly_fired.append({"label": w["label"], "address": w["address"], "tier": w["tier"],
                                "chain": w["chain"], "nonce_prev": prev, "nonce_now": w["nonce"],
                                "operational": op, "confidence": conf})

    # SPEC 24: a MAPPED operator safe firing is the §8 candidate — but a nonce-delta only proves the
    # WALLET moved, not the TRACKED TOKEN. For HIGH-NONCE wallets (CEX-MM EOAs that churn constantly)
    # require a recent tracked-token OUT to CONFIRM the escalation; a fired high-nonce wallet whose
    # token-out is stale is NONCE-CHURN (noise), NOT distribution. Low-nonce dormant safes still
    # escalate on the nonce alone (the §8 early-warning edge — the cascade outruns the visible out).
    # SPEC 28: classify each fired wallet's last token-out DESTINATION — cex-execution (the dump
    # firing) vs staging-internal (apparatus loading/consolidation), so the board distinguishes them.
    tracked_map = {w["address"].lower(): {"label": w.get("label"), "tier": (w.get("tier") or "").lower()}
                   for w in snap["wallets"]}
    labels = _entity_labels()

    def _dest_kind(dest):
        if not dest:
            return None
        d = dest.lower()
        if d in tracked_map:
            return classify_destination("tracked-safe", tracked_map[d]["tier"])
        lab = labels.get(d)
        if lab and any(h in lab.lower() for h in ("cex", "binance", "bybit", "gate", "bitget",
                                                  "okx", "kucoin", "mexc", "htx", "kraken", "coinbase")):
            return "cex-execution"
        return "unknown"

    confirmed, churn, unconfirmed = [], [], []
    for f in [c for c in newly_fired if c["confidence"] == "high"]:
        ck = f["chain"]
        contract = contract_bsc if ck == "binance-smart-chain" else (tok.get("contracts", {}) or {}).get(ck)
        if f["nonce_now"] < HIGH_NONCE_THRESHOLD:
            f["high_nonce"] = False                       # genuine dormant safe → nonce alone escalates
            last_out, dest, avail = _last_token_out(f["address"], contract, ck, decimals)
            f["last_token_out_ts"], f["token_out_recent"] = last_out, None
            f["last_out_dest"], f["dest_kind"] = dest, _dest_kind(dest)
            confirmed.append(f)
            continue
        f["high_nonce"] = True
        last_out, dest, avail = _last_token_out(f["address"], contract, ck, decimals)
        f["last_token_out_ts"], f["last_out_dest"], f["dest_kind"] = last_out, dest, _dest_kind(dest)
        if not avail:                                     # SPEC 24: degrade-explicit, don't silently (sup)press
            f["token_out_recent"] = None
            unconfirmed.append(f)
        else:
            out_ts = _parse_ts(last_out)
            recent = out_ts is not None and (now - out_ts) <= TOKEN_OUT_WINDOW_SEC
            f["token_out_recent"] = recent
            (confirmed if recent else churn).append(f)

    escalation = confirmed                                # only token-out-confirmed fires the §8 escalation
    # SPEC 28: an escalation routing tokens to a CEX = EXECUTION (the dump firing); to an internal
    # operator sink = STAGING (apparatus loading). Distinct §8 severities.
    execution_escalation = [f for f in confirmed if f.get("dest_kind") in ("cex-execution", "dex-execution")]
    staging_escalation = [f for f in confirmed if f.get("dest_kind") == "staging-internal"]
    escalation_kind = "execution" if execution_escalation else ("staging" if staging_escalation else None)
    low_conf_fired = [f for f in newly_fired if f["confidence"] == "low"]
    # SPEC 55: untiered-candidate fires (the Designer's tiering pass pending). An all-
    # untiered batch settling its baseline is NOT a §8 event — it reads BASELINE_SETTLING.
    untiered_fired = [f for f in newly_fired if f["confidence"] == "untiered"]
    all_untiered = bool(newly_fired) and len(untiered_fired) == len(newly_fired)
    dist_present = any((w["tier"] or "").lower() not in OP_TIERS for w in snap["wallets"])

    if not had_baseline:
        signal, score = "QUIET", 0                       # first run: just seeded, no diff yet
    elif escalation and not baseline_stale:
        signal, score = "ESCALATION", -30                # mapped safe fired + confirmed token-out vs fresh baseline
    elif escalation and baseline_stale:
        signal, score = "ESCALATION", -15                # catch-up vs a cold baseline → degrade, re-confirm
    elif unconfirmed:
        signal, score = "ESCALATION-UNCONFIRMED", -15    # high-nonce fired but token-out read unavailable (provider down)
    elif churn:
        signal, score = "NONCE-CHURN", 0                 # high-nonce churned but tracked-token OUT is stale → noise, NOT distribution
    elif all_untiered:
        signal, score = "BASELINE_SETTLING", 0           # SPEC 55: untiered candidates only → tiering pass gates §8 meaning
    elif low_conf_fired:
        signal, score = "LOADING", -10                   # only unmapped feeders moved
    elif snap["primed_unfired_count"] > 0 and dist_present:
        signal, score = "LOADING", -10
    elif dist_present and snap["dormant_count"] > 0:
        signal, score = "DORMANT", 10
    else:
        signal, score = "QUIET", 0

    # SPEC-145: a wallet whose RPC read failed (build_snapshot's rpc_ok=False) was previously
    # silently dropped here — the signal read QUIET/DORMANT identically whether nothing moved
    # or a third of the tracked set was simply unreadable. Count it and, when it would make
    # a clean-looking QUIET/DORMANT lie about coverage, override the signal so no caller can
    # mistake "half-blind" for "quiet" (CLAUDE.md §3).
    unreadable = [{"address": w["address"], "chain": w.get("chain"),
                   "reason": w.get("reason") or "rpc_ok=false"}
                  for w in snap["wallets"] if not w.get("rpc_ok")]
    wallets_unreadable = len(unreadable)
    wallets_total = snap["n_wallets"]
    wallets_read = wallets_total - wallets_unreadable
    if wallets_unreadable > 0 and signal in ("QUIET", "DORMANT"):
        signal = "PARTIAL"

    # SPEC 55: advance the committed baseline only when persisting (the surveillance sweep),
    # or to SEED a missing one. A plain read leaves the baseline put so repeated reads agree.
    if persist or not had_baseline:
        _save_baseline(ticker, snap["wallets"], now, seeded_ts)

    result = {
        "ticker": ticker, "tracked": True, "ms": ms, "signal": signal, "score": score,
        "baseline_seeded": had_baseline,
        "baseline_age": round(baseline_age) if baseline_age is not None else None,
        "baseline_stale": baseline_stale,
        "fired_count": snap["fired_count"], "primed_unfired_count": snap["primed_unfired_count"],
        "dormant_count": snap["dormant_count"], "n_wallets": snap["n_wallets"],
        "newly_fired": newly_fired, "escalation_fired": escalation, "low_conf_fired": low_conf_fired,
        "untiered_fired": untiered_fired,                              # SPEC 55: candidates pending the tiering pass
        "nonce_churn": churn, "escalation_unconfirmed": unconfirmed,   # SPEC 24
        "escalation_kind": escalation_kind,                            # SPEC 28: execution | staging | None
        "execution_escalation": execution_escalation, "staging_escalation": staging_escalation,
        "wallets_total": wallets_total, "wallets_read": wallets_read,  # SPEC-145 coverage
        "wallets_unreadable": wallets_unreadable, "unreadable": unreadable,
        "wallets": snap["wallets"],
    }
    return result


def _entity_labels():
    """{addr_lower: entity_name} from config/vc_entities.json + known_entities.json."""
    labels = {}
    try:
        ents = json.loads(VC_ENTITIES.read_text()).get("entities", {})
        for name, e in (ents.items() if isinstance(ents, dict) else []):
            for addr in (e.get("addresses", []) if isinstance(e, dict) else []):
                if isinstance(addr, str):
                    labels[addr.lower()] = name
    except Exception:
        pass
    try:
        known = json.loads(KNOWN_ENTITIES.read_text())
        # addresses live under known["entities"] as {addr: {"label","name"}} — older flat
        # {addr: name} also tolerated. The previous loader walked the top level (keys
        # _source/_built/entities) and required string values, so it loaded ZERO addresses
        # → CEX hot wallets (Gate.io etc.) dropped to "unknown" instead of cex-withdrawal.
        ents = known.get("entities", known) if isinstance(known, dict) else {}
        for addr, info in (ents.items() if isinstance(ents, dict) else []):
            if not (isinstance(addr, str) and addr.startswith("0x")):
                continue
            nm = (info.get("name") or info.get("label")) if isinstance(info, dict) else info
            if nm:
                labels.setdefault(addr.lower(), str(nm))
    except Exception:
        pass
    return labels


def _vc_overlap(ticker):
    """NATIVE: do any tracked wallets match a known VC/entity address? (config join, fast)."""
    try:
        tok = json.loads(WALLETS.read_text()).get("tokens", {}).get(ticker.upper())
        if not tok:
            return {"available": True, "matches": []}
        tracked = {w["address"].lower(): w.get("label") for w in tok.get("wallets", [])}
        labels = _entity_labels()
        matches = [{"address": a, "wallet_label": tracked[a], "entity": labels[a]}
                   for a in tracked if a in labels]
        return {"available": True, "matches": matches}
    except Exception as e:  # noqa: BLE001
        return {"available": False, "reason": str(e)[:120]}


_MORALIS = None


def _moralis():
    global _MORALIS
    if _MORALIS is None:
        spec = importlib.util.spec_from_file_location("moralis", ROOT / "_oldrepo/scripts/moralis.py")
        _MORALIS = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(_MORALIS)
    return _MORALIS

# Moralis chain alias per tracked-wallet chain key
_MORALIS_CHAIN = {"binance-smart-chain": "bsc", "ethereum": "eth", "base": "base",
                  "polygon": "polygon", "arbitrum": "arbitrum", "optimism": "optimism",
                  "avalanche": "avalanche"}   # SPEC 56


class MoralisError(Exception):
    """Raised when a Moralis read fails (rate-limit/transient) after retries — callers must
    degrade EXPLICITLY (available:false / degraded:true), never silently return 0/empty."""


class MoralisQuotaExhausted(MoralisError):
    """The Moralis 401 'plan consumed' response — the daily budget is truly gone, not a
    transient rate-limit (SPEC-123). Retrying is futile and was the root cause of the
    2026-07-14 incident: retry+backoff on every 401 across a wallet sweep chewed through the
    orchestrator's per-capability timeout, so the operator only ever saw a bare
    `timeout running verify_wallet` with the real 401 signal discarded. Raised immediately,
    no retry, so callers surface QUOTA_EXHAUSTED in their reason/error field instead."""


_QUOTA_EXHAUSTED_MARKERS = ("validation service blocked", "included usage has been consumed")


def _moralis_quota_exhausted_marker():
    return "QUOTA_EXHAUSTED (moralis, resets 00:00 UTC)"


def _is_moralis_quota_exhausted(err):
    s = str(err).lower()
    return "401" in s and any(m in s for m in _QUOTA_EXHAUSTED_MARKERS)


# --- throttle + short-TTL cache so high-frequency polling doesn't trip the free-tier limit ---
_MORALIS_LOCK = threading.Lock()
_MORALIS_LAST = [0.0]
_MORALIS_MIN_INTERVAL = 0.20   # s between live Moralis calls (smooths concurrent bursts)
_MORALIS_TTL = 90              # s — repeated reads of the same wallet within this window are cached
_MORALIS_CACHE = {}


def _moralis_tokentx(address, contract=None, chain="bsc", days=180, max_pages=2, retries=2):
    """Throttled + cached + retried Moralis erc20-transfers read. Returns the tx list, or
    raises MoralisError on persistent rate-limit/error (so the caller degrades explicitly)."""
    key = (address.lower(), (contract or "").lower(), chain, days, max_pages)
    hit = _MORALIS_CACHE.get(key)
    if hit and (time.time() - hit[0]) < _MORALIS_TTL:
        return hit[1]
    m = _moralis()
    last_err = None
    for attempt in range(retries + 1):
        with _MORALIS_LOCK:                       # space out live calls across threads
            wait = _MORALIS_MIN_INTERVAL - (time.time() - _MORALIS_LAST[0])
            if wait > 0:
                time.sleep(wait)
            _MORALIS_LAST[0] = time.time()
        try:
            txs = m.tokentx(address, contract=contract, chain=chain, days=days, max_pages=max_pages)
            moralis_quota.record_call()   # SPEC 66: meter the LIVE call (cache hits don't count)
            _MORALIS_CACHE[key] = (time.time(), txs)
            return txs
        except Exception as e:  # noqa: BLE001
            if _is_moralis_quota_exhausted(e):
                # SPEC-123: fail fast — no retry/backoff, the quota won't come back mid-run.
                raise MoralisQuotaExhausted(
                    f"{_moralis_quota_exhausted_marker()}: {str(e)[:160]}") from e
            last_err = e
            if attempt < retries:
                time.sleep(1.0 * (attempt + 1))   # backoff before retry
    raise MoralisError(f"moralis tokentx failed after {retries + 1} tries: {str(last_err)[:140]}")


# ───────────────────────── SPEC 17: free-RPC nonce + provider-fallback transfers ─────────────────────────
# Free public-RPC pools (no key, no per-call quota). Config rpc first, then public fallbacks.
# BSC RPCs need a browser UA (parts-bin lesson).
_PUBLIC_RPCS = {
    "binance-smart-chain": ["https://bsc-dataseed.binance.org", "https://bsc.publicnode.com",
                            "https://binance.llamarpc.com"],
    "ethereum": ["https://ethereum-rpc.publicnode.com", "https://eth.llamarpc.com"],
    # SPEC 56: nonce surveillance follows primary_chain — avalanche is a first-class chain
    "avalanche": ["https://api.avax.network/ext/bc/C/rpc",
                  "https://avalanche-c-chain-rpc.publicnode.com"],
}
TRANSFER_TOPIC = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"


def _rpc_pool(chain_key):
    cfg = {}
    try:
        cfg = json.loads(WALLETS.read_text()).get("rpcs", {})
    except Exception:
        pass
    pool = [cfg[chain_key]] if cfg.get(chain_key) else []
    return pool + _PUBLIC_RPCS.get(chain_key, [])


def _rpc_at(url, method, params, timeout=12):
    """Single-endpoint JSON-RPC (UA required for BSC). Returns the result field or None."""
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params}).encode()
    try:
        req = urllib.request.Request(url, data=body,
                                     headers={"Content-Type": "application/json", "User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            d = json.loads(r.read())
        return d.get("result")
    except Exception:
        return None


def _rpc(chain_key, method, params, timeout=12):
    """JSON-RPC across the free pool (UA required for BSC). Returns the first result or None."""
    for url in _rpc_pool(chain_key):
        r = _rpc_at(url, method, params, timeout=timeout)
        if r is not None:
            return r
    return None


# SPEC 58 — ERC-20 view calls (balanceOf / totalSupply) for a REAL balance, not net-flow.
BALANCEOF_SELECTOR = "0x70a08231"     # balanceOf(address)
TOTALSUPPLY_SELECTOR = "0x18160ddd"   # totalSupply()
NAME_SELECTOR = "0x06fdde03"          # name()
DECIMALS_SELECTOR = "0x313ce567"      # decimals()


def _erc20_uint(contract, chain_key, selector, addr_arg=None, decimals=18, providers=2):
    """Cross-checked ERC-20 view call (§3 second-source rule). Queries up to `providers`
    distinct RPCs in the pool; returns (value, sources, agree). value is None when no provider
    answered (caller surfaces unavailable, never a fabricated 0). agree is None on a single
    successful source (cross-check not possible), True/False on two."""
    pool = _rpc_pool(chain_key)
    if not pool:
        return None, [], False
    data = selector + (("0" * 24 + addr_arg[2:].lower()) if addr_arg else "")
    vals, srcs = [], []
    for url in pool:
        r = _rpc_at(url, "eth_call", [{"to": contract, "data": data}, "latest"])
        try:
            v = int(r, 16) / (10 ** decimals)
        except (TypeError, ValueError):
            continue
        vals.append(v)
        srcs.append(url)
        if len(vals) >= providers:
            break
    if not vals:
        return None, [], False
    agree = (abs(vals[0] - vals[1]) <= max(1e-9, 1e-6 * abs(vals[0]))) if len(vals) >= 2 else None
    return vals[0], srcs, agree


def balance_of(address, contract, chain_key, decimals=18):
    """SPEC 58 — the REAL on-chain balance (one cross-checked balanceOf), with a supply share
    off a cross-checked totalSupply. Returns an explicit dict; {available:False} on non-EVM /
    no-RPC-pool / all-providers-fail (never a fabricated 0 — a zero read is a §3 data FAILURE)."""
    val, srcs, agree = _erc20_uint(contract, chain_key, BALANCEOF_SELECTOR, address, decimals)
    if val is None:
        return {"available": False, "chain": chain_key, "reason": f"balanceOf unreadable on {chain_key}"}
    sup, _ssrc, _sagree = _erc20_uint(contract, chain_key, TOTALSUPPLY_SELECTOR, None, decimals)
    pct = round(val / sup * 100, 4) if sup else None
    return {"available": True, "chain": chain_key, "value": val, "pct_supply": pct,
            "total_supply": sup, "cross_checked": len(srcs) >= 2, "agree": agree, "sources": srcs}


def _call_string(contract, chain_key, selector):
    """eth_call returning ABI-encoded dynamic string → decoded str or None (revert /
    unreadable / non-string return)."""
    r = _rpc(chain_key, "eth_call", [{"to": contract, "data": selector}, "latest"])
    if not isinstance(r, str) or not r.startswith("0x"):
        return None
    h = r[2:]
    if len(h) < 128:
        return None
    try:
        length = int(h[64:128], 16)
        raw = bytes.fromhex(h[128:128 + length * 2])
        s = raw.decode("utf-8", errors="ignore").strip("\x00").strip()
        return s or None
    except (ValueError, IndexError):
        return None


def erc20_meta(contract, chain_key):
    """SPEC-165 — RPC-only ERC20 identity (name/decimals/totalSupply), NO CoinGecko
    dependency. Fresh BSC->Alpha names have no CoinGecko entry on day one (DEBIT,
    KORU); `onboard` with an explicit contract reads identity straight off-chain.
    Never raises: each field degrades to None independently on a revert/unreadable
    call, `available` is True as soon as any one of them resolved."""
    name = _call_string(contract, chain_key, NAME_SELECTOR)
    decimals = _call_uint(contract, chain_key, DECIMALS_SELECTOR)
    raw_supply = _call_uint(contract, chain_key, TOTALSUPPLY_SELECTOR)
    dec = decimals if decimals is not None else 18
    total_supply = (raw_supply / (10 ** dec)) if raw_supply is not None else None
    return {"available": bool(name is not None or decimals is not None or raw_supply is not None),
            "name": name, "decimals": dec, "total_supply": total_supply}


def nonce_of(address, chain_key="binance-smart-chain"):
    """Free-RPC tx-count = liveness/change signal (SPEC 17 layer 1). Zero provider quota.
    Returns int or None. Use for 'still selling? / went quiet?' instead of a heavy read."""
    r = _rpc(chain_key, "eth_getTransactionCount", [address, "latest"])
    try:
        return int(r, 16)
    except (TypeError, ValueError):
        return None


# ── SPEC-67: contract / DEX-pool probe ───────────────────────────────────────────
# A tracked-wallet LABEL must never override on-chain FACTS. The live false positive:
# verify_wallet called a real ESPORTS swap-buyer "operator-seeded" because its top
# funded_by source (0x5bb59bb9) carried a desk label "TWAP-HUB-6.8M" — but that address
# is the PancakeSwap V3 WBNB/ESPORTS 0.01% POOL (token0=WBNB, token1=ESPORTS, fee=100).
# Its 956 inbound clips were ordinary swap fills. Any tracked pool/router poisons every
# future funded_by the same way. Probe the bytecode + pool getters BEFORE trusting a tier.
# Memory: probe_contracts_before_labeling_wallets.
POOL_TOKEN0_SELECTOR = "0x0dfe1681"   # token0()
POOL_TOKEN1_SELECTOR = "0xd21220a7"   # token1()
POOL_FEE_SELECTOR = "0xddca3f43"      # fee()
_PROBE_CACHE_PATH = STATE / "contract_probe_cache.json"
_EOA_CODE = {"0x", "0x0", "", "0x00"}


def code_of(address, chain_key="binance-smart-chain"):
    """eth_getCode for an address. '0x'/'0x0' (empty) = EOA; longer = deployed contract
    bytecode. Returns the hex string, or None when no provider answered (degrade-explicit —
    a missing read is NOT 'it's an EOA', §3)."""
    return _rpc(chain_key, "eth_getCode", [address, "latest"])


def _call_address(contract, chain_key, selector):
    """eth_call returning a single address word → checksum-less lowercase 0x… or None
    (revert / zero / unreadable)."""
    r = _rpc(chain_key, "eth_call", [{"to": contract, "data": selector}, "latest"])
    if not isinstance(r, str):
        return None
    h = r[2:] if r.startswith("0x") else r
    if len(h) < 40:
        return None
    addr = "0x" + h[-40:]
    try:
        return addr if int(addr, 16) != 0 else None
    except ValueError:
        return None


def _call_uint(contract, chain_key, selector):
    r = _rpc(chain_key, "eth_call", [{"to": contract, "data": selector}, "latest"])
    try:
        return int(r, 16)
    except (TypeError, ValueError):
        return None


def _load_probe_cache():
    try:
        return json.loads(_PROBE_CACHE_PATH.read_text())
    except Exception:  # noqa: BLE001
        return {}


def _save_probe_cache(cache):
    try:
        STATE.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=str(STATE), prefix=".contract_probe_cache.", suffix=".tmp")
        with os.fdopen(fd, "w") as f:
            json.dump(cache, f)
        os.replace(tmp, str(_PROBE_CACHE_PATH))
    except Exception:  # noqa: BLE001 — a cache-write failure must never block a verdict
        pass


def probe_contract(address, chain_key="binance-smart-chain", use_cache=True):
    """SPEC-67: is `address` a contract, and specifically a DEX pool? Returns a dict:
      {available, is_contract, is_pool, token0, token1, fee, kind}
    A contract whose token0()/token1() BOTH resolve is a `dex-pool` (kind set) — it can
    never count as an operator safe/hub regardless of any desk label. EOAs and non-pool
    contracts report is_pool:false. available:False on an unreadable bytecode read (we did
    NOT prove it's an EOA — never seed-classify off a failed probe). Cached per address in
    state/ (bytecode is immutable → long TTL)."""
    addr = (address or "").lower()
    if not addr:
        return {"available": False, "address": addr, "chain": chain_key,
                "is_contract": None, "is_pool": False, "reason": "no address"}
    cache = _load_probe_cache() if use_cache else {}
    ckey = f"{chain_key}:{addr}"
    if use_cache and ckey in cache:
        return cache[ckey]
    code = code_of(address, chain_key)
    if code is None:
        # bytecode unreadable — degrade EXPLICIT, do NOT cache, do NOT treat as EOA
        return {"available": False, "address": addr, "chain": chain_key,
                "is_contract": None, "is_pool": False, "reason": "eth_getCode unreadable"}
    is_contract = code not in _EOA_CODE
    out = {"available": True, "address": addr, "chain": chain_key,
           "is_contract": is_contract, "is_pool": False,
           "token0": None, "token1": None, "fee": None, "kind": None}
    if is_contract:
        t0 = _call_address(address, chain_key, POOL_TOKEN0_SELECTOR)
        t1 = _call_address(address, chain_key, POOL_TOKEN1_SELECTOR)
        out["token0"], out["token1"] = t0, t1
        out["fee"] = _call_uint(address, chain_key, POOL_FEE_SELECTOR)
        out["is_pool"] = bool(t0 and t1)
        out["kind"] = "dex-pool" if out["is_pool"] else "contract"
    else:
        out["kind"] = "eoa"
    if use_cache:
        cache[ckey] = out
        _save_probe_cache(cache)
    return out


def _getlogs_transfers(address, contract, chain_key, decimals=18, recent_blocks=50000, max_chunks=3):
    """Provider FALLBACK (SPEC 17 layer 3): recent-window token transfers for one address via
    free-RPC eth_getLogs (no key). Bounded to a recent window (partial, not lifetime). Returns
    Moralis-shaped tx dicts. Raises on total failure."""
    bn_hex = _rpc(chain_key, "eth_blockNumber", [])
    if not bn_hex:
        raise MoralisError("getlogs fallback: eth_blockNumber failed")
    latest = int(bn_hex, 16)
    blk = _rpc(chain_key, "eth_getBlockByNumber", [hex(latest), False]) or {}
    try:
        latest_ts = int(blk.get("timestamp"), 16)
    except (TypeError, ValueError):
        latest_ts = None
    padded = "0x" + "0" * 24 + address[2:].lower()
    chunk = recent_blocks // max_chunks
    rows, block_ts = [], {}
    for i in range(max_chunks):
        hi = latest - i * chunk
        lo = hi - chunk + 1
        for topics in ([TRANSFER_TOPIC, padded, None], [TRANSFER_TOPIC, None, padded]):
            logs = _rpc(chain_key, "eth_getLogs",
                        [{"address": contract, "fromBlock": hex(lo), "toBlock": hex(hi), "topics": topics}])
            if not isinstance(logs, list):
                continue
            for lg in logs:
                try:
                    frm = "0x" + lg["topics"][1][-40:]
                    to = "0x" + lg["topics"][2][-40:]
                    val = int(lg["data"], 16) / (10 ** decimals)
                    b = int(lg["blockNumber"], 16)
                except (KeyError, ValueError, IndexError, TypeError):
                    continue
                ts = None
                if latest_ts is not None:
                    ts = latest_ts - int((latest - b) * 1.5)   # ~1.5s/block approx (recent only)
                rows.append({"from_address": frm, "to_address": to, "value_decimal": val,
                             "block_timestamp": (__import__("datetime").datetime.utcfromtimestamp(ts).strftime("%Y-%m-%dT%H:%M:%S.000Z") if ts else None),
                             "token_symbol": None, "_approx_ts": True})
    # dedup by (block, from, to, val)
    seen, out = set(), []
    for r in sorted(rows, key=lambda x: x.get("block_timestamp") or "", reverse=True):
        k = (r["block_timestamp"], r["from_address"], r["to_address"], r["value_decimal"])
        if k not in seen:
            seen.add(k)
            out.append(r)
    return out


# ───────────────────────── SPEC-97: chain-scoped provider seam ─────────────────────────
# The wallet-scoped token-flow read behind an ordered, PER-CHAIN provider list. v3 premise
# (live-verified 2026-07-02): Etherscan V2 free tier is ETH-ONLY (chainid=56 returns "Free
# API access is not supported for this chain"), so Etherscan registers for ethereum only:
#   ETH: etherscan → moralis → getlogs        BSC (+rest): moralis → getlogs (unchanged)
# A provider declares the chains it serves; an unsupported chain skips it silently (no
# wasted call, no error). register_provider() is the socket SPEC-101 (Bitquery) and
# SPEC-102 (local indexer) plug BSC providers into without touching this file again.

class EtherscanError(Exception):
    """A real Etherscan failure (NOTOK / HTTP / timeout) — falls through to the next provider."""


class ProviderSkip(Exception):
    """Provider not applicable for this call (no key, contract-less getlogs) — skip silently."""


ETHERSCAN_V2_BASE = "https://api.etherscan.io/v2/api"
# SPEC-192 #2: free-tier chain ids beyond ethereum (live-verified 2026-09-02,
# reports/RESEARCH-2026-09-02-onchain-chain-coverage.md — keyless probe on each
# returned "Missing/Invalid API Key", the free-tier discriminator, vs BSC/Base/
# Optimism/Avalanche's "Paid Tier Only" refusal, which is why those four stay OFF this
# map on purpose). chainid per https://docs.etherscan.io/supported-chains.
_ETHERSCAN_CHAINID = {"ethereum": 1, "arbitrum-one": 42161, "polygon-pos": 137,
                      "mantle": 5000, "berachain": 80094, "monad": 143, "sonic": 146,
                      "sei-v2": 1329, "abstract": 2741, "hyperevm": 999,
                      "world-chain": 480}
_ETHERSCAN_LOCK = threading.Lock()
_ETHERSCAN_LAST = [0.0]
# SPEC-192 #2: the docs state 3 calls/s for the free tier (not 5) — this was paced at
# 4 rps (0.25s) assuming the wrong cap; fixed to honor 3 cps now that the key is shared
# across ten chains instead of one.
_ETHERSCAN_MIN_INTERVAL = 0.34
_ETHERSCAN_TTL = 90              # s — same short-TTL cache discipline as _moralis_tokentx
_ETHERSCAN_CACHE = {}
_ETHERSCAN_ERR_LOGGED = set()    # log each distinct NOTOK reason once, not per call


def _etherscan_key():
    try:
        return json.loads((ROOT / "config" / "secrets.json").read_text()).get("etherscan_api_key")
    except Exception:  # noqa: BLE001
        return None


def _etherscan_call(url, timeout=15):
    """One live Etherscan GET → parsed JSON (separate so tests patch the wire, not the logic)."""
    req = urllib.request.Request(url, headers={"User-Agent": "onchain/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())


def _etherscan_map_row(r, decimals_default=18):
    """Etherscan tokentx wire row → the Moralis-shaped dict every consumer already reads."""
    try:
        dec = int(r.get("tokenDecimal") or decimals_default)
    except (TypeError, ValueError):
        dec = decimals_default
    try:
        val = int(r.get("value") or 0) / (10 ** dec)
    except (TypeError, ValueError):
        val = None
    ts_iso = None
    try:
        ts_iso = datetime.utcfromtimestamp(int(r["timeStamp"])).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    except (KeyError, TypeError, ValueError):
        pass
    return {"from_address": (r.get("from") or "").lower() or None,
            "to_address": (r.get("to") or "").lower() or None,
            "value": r.get("value"), "value_decimal": val,
            "block_timestamp": ts_iso, "token_symbol": r.get("tokenSymbol"),
            "token_decimals": r.get("tokenDecimal"),
            "transaction_hash": r.get("hash"), "address": r.get("contractAddress")}


def _etherscan_tokentx(address, contract, chain_key, days=180, decimals=18, retries=1):
    """Throttled + cached + metered Etherscan-V2 tokentx read (ETH-scoped). Returns
    Moralis-shaped tx dicts windowed to `days`. Raises ProviderSkip when no key,
    EtherscanError on a real failure. The envelope quirk that matters (§3): status:"0"
    "No transactions found" is an EMPTY BOOK (a datum), never an error/fallback."""
    key = _etherscan_key()
    if not key:
        raise ProviderSkip("no etherscan_api_key")
    chainid = _ETHERSCAN_CHAINID[chain_key]
    ckey = (address.lower(), (contract or "").lower(), chainid, days)
    hit = _ETHERSCAN_CACHE.get(ckey)
    if hit and (time.time() - hit[0]) < _ETHERSCAN_TTL:
        return hit[1]
    params = {"chainid": chainid, "module": "account", "action": "tokentx",
              "address": address, "page": 1, "offset": 200, "sort": "desc", "apikey": key}
    if contract:
        params["contractaddress"] = contract
    url = ETHERSCAN_V2_BASE + "?" + "&".join(f"{k}={v}" for k, v in params.items())
    last_err = None
    for attempt in range(retries + 1):
        with _ETHERSCAN_LOCK:                     # space out live calls across threads
            wait = _ETHERSCAN_MIN_INTERVAL - (time.time() - _ETHERSCAN_LAST[0])
            if wait > 0:
                time.sleep(wait)
            _ETHERSCAN_LAST[0] = time.time()
        try:
            d = _etherscan_call(url)
            provider_quota.record_call("etherscan")   # meter the LIVE call (cache hits don't)
            break
        except Exception as e:  # noqa: BLE001 — HTTP/timeout/bad JSON
            last_err = e
            if attempt < retries:
                time.sleep(1.0 * (attempt + 1))
    else:
        raise EtherscanError(f"etherscan unreachable: {str(last_err)[:120]}")
    status_s, msg = str(d.get("status")), str(d.get("message") or "")
    res = d.get("result")
    if status_s == "1" and isinstance(res, list):
        rows = res
    elif "no transactions found" in f"{msg} {res}".lower():
        rows = []                                  # empty result, NOT an error (§3)
    else:
        reason = f"{msg}: {str(res)[:120]}"
        if reason not in _ETHERSCAN_ERR_LOGGED:    # e.g. "Free API access is not supported…"
            _ETHERSCAN_ERR_LOGGED.add(reason)
            print(f"WARN SPEC-97: etherscan NOTOK — {reason}", file=sys.stderr)
        raise EtherscanError(f"etherscan NOTOK: {reason}")
    cutoff = time.time() - days * 86400
    out = []
    for r in rows:
        m = _etherscan_map_row(r, decimals)
        e = _parse_ts(m.get("block_timestamp"))
        if e is not None and e < cutoff:
            break                                  # rows are desc — past the window, stop
        out.append(m)
    _ETHERSCAN_CACHE[ckey] = (time.time(), out)
    return out


_LOCAL_INDEX_STALE_WARNED = set()   # one-time-per-contract stderr note, like _ETHERSCAN_ERR_LOGGED


def _p_local_index(address, contract, chain_key, days, decimals):
    """SPEC-102: the tracked-set local SQLite index — pure read, no network, no quota. A
    contract outside the tracked set, an un-ingested range, or a stale head all raise
    ProviderSkip (fall through to the vendor chain, exactly like every other provider here);
    a stale (as opposed to never-ingested) head gets a one-time stderr note per contract so
    the fallback is visible, mirroring the Etherscan NOTOK precedent."""
    r = local_index.query_or_stale(address, contract, chain_key, days=days, decimals=decimals)
    if not r.get("available"):
        if r.get("stale_index") and contract not in _LOCAL_INDEX_STALE_WARNED:
            _LOCAL_INDEX_STALE_WARNED.add(contract)
            print(f"WARN SPEC-102: local_index stale for {contract} on {chain_key} "
                 f"({r.get('age_s', 0):.0f}s old) — falling through to vendor chain", file=sys.stderr)
        raise ProviderSkip(r.get("reason") or "local_index unavailable")
    return r["txs"], False


def _p_etherscan(address, contract, chain_key, days, decimals):
    return _etherscan_tokentx(address, contract, chain_key, days=days, decimals=decimals), False


# SPEC-152: Blockscout keyless fallback adapter in the SPEC-97 seam. Live-verified
# 2026-08-20 (orchestrator, keyless curl, real tracked address): eth.blockscout.com and
# base.blockscout.com answer HTTP 200 with an Etherscan-compatible {message, result} body;
# bsc.blockscout.com is 404 and blockscout.com/bsc/mainnet is 503 — NO official BSC
# instance exists, so BSC stays on Moralis + local_index per the G2 v3 decision. Solves
# two things instead: a second free ETH provider beside Etherscan-V2's shared quota, and
# Base coverage where the desk was blind (SPEC-145's first live run: 4 unreadable HOME
# wallets on Base, "all 1 provider(s) failed"). The instance map lives in config so a new
# VERIFIED instance (never an unofficial community one — feedback_live_verify_vendor_api_
# tiers_before_spec) is a config add, not a code change.
_BLOCKSCOUT_CONFIG_PATH = ROOT / "config" / "blockscout.json"
# NOTE: config/blockscout.json is AUTHORITATIVE in production — _blockscout_instances()
# reads it first and only falls back to this dict when the file is absent/unparseable
# (tests, or a stripped-down deployment). Keep the two in sync; a chain added only here
# is inert everywhere the config file exists (SPEC-192 #3 was live-caught this way —
# see config/blockscout.json's own _doc field).
_BLOCKSCOUT_DEFAULT_INSTANCES = {"ethereum": "https://eth.blockscout.com/api",
                                 "base": "https://base.blockscout.com/api",
                                 # SPEC-192 #3: live-verified 2026-09-02 keyless HTTP 200
                                 # with the same Etherscan-compatible envelope. No
                                 # official instance exists for BSC/berachain/monad/
                                 # sonic/avalanche — left absent from this map, which
                                 # is the desk's existing "named BLIND" convention
                                 # (ProviderSkip names the chain, never a silent drop).
                                 # optimism.blockscout.com 301-redirects to
                                 # explorer.optimism.io — pointed straight at the
                                 # canonical instance to skip the extra hop.
                                 "arbitrum-one": "https://arbitrum.blockscout.com/api",
                                 "optimistic-ethereum": "https://explorer.optimism.io/api",
                                 "polygon-pos": "https://polygon.blockscout.com/api",
                                 "hemi": "https://explorer.hemi.xyz/api"}
# SPEC-192 #3: chains with a keyless Blockscout v2 HOLDERS endpoint (`/api/v2/tokens/
# {addr}/holders`) that also answers reliably — Base's is flaky (500s on big tokens,
# live-verified) so it is DELIBERATELY excluded here even though its flow instance
# above works; a Base holders call degrades loudly via BlockscoutError, never silently.
_BLOCKSCOUT_HOLDERS_CHAINS = {"ethereum", "arbitrum-one", "optimistic-ethereum",
                              "polygon-pos", "hemi"}


class BlockscoutError(Exception):
    """A real Blockscout failure (non-OK / HTTP / timeout) — falls through, exactly like
    EtherscanError."""


def _blockscout_instances():
    try:
        d = json.loads(_BLOCKSCOUT_CONFIG_PATH.read_text())
        if isinstance(d, dict):
            return {k: v for k, v in d.items() if not k.startswith("_") and isinstance(v, str)}
    except (OSError, json.JSONDecodeError):
        pass
    return dict(_BLOCKSCOUT_DEFAULT_INSTANCES)


def _blockscout_call(url, timeout=15):
    """One live Blockscout GET → parsed JSON (separate so tests patch the wire, not the
    logic — same pattern as _etherscan_call)."""
    req = urllib.request.Request(url, headers={"User-Agent": "onchain/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())


def _blockscout_tokentx(address, contract, chain_key, days=180, decimals=18, retries=1):
    """Blockscout is Etherscan-compatible (module=account&action=tokentx, same
    {status,message,result} envelope) — reuses _etherscan_map_row rather than a third
    mapper for the same wire format (req 2). §3 doctrine (req 5): a non-OK
    status/message/HTTP failure raises (provider failed, falls through); "No transactions
    found" is an EMPTY BOOK, never an error."""
    base = _blockscout_instances().get(chain_key)
    if not base:
        raise ProviderSkip(f"no blockscout instance for {chain_key}")
    params = {"module": "account", "action": "tokentx", "address": address, "page": 1,
              "offset": 200, "sort": "desc"}
    if contract:
        params["contractaddress"] = contract
    url = base + "?" + "&".join(f"{k}={v}" for k, v in params.items())
    last_err = None
    for attempt in range(retries + 1):
        try:
            d = _blockscout_call(url)
            break
        except Exception as e:  # noqa: BLE001 — HTTP/timeout/bad JSON, retried once (req 6)
            last_err = e
            if attempt < retries:
                time.sleep(1.0 * (attempt + 1))
    else:
        raise BlockscoutError(f"blockscout unreachable: {str(last_err)[:120]}")
    status_s, msg = str(d.get("status")), str(d.get("message") or "")
    res = d.get("result")
    if (status_s == "1" or msg.upper() == "OK") and isinstance(res, list):
        rows = res
    elif "no transactions found" in f"{msg} {res}".lower():
        rows = []                                  # empty result, NOT an error (§3)
    else:
        raise BlockscoutError(f"blockscout NOTOK: {msg}: {str(res)[:120]}")
    cutoff = time.time() - days * 86400
    out = []
    for r in rows:
        m = _etherscan_map_row(r, decimals)
        e = _parse_ts(m.get("block_timestamp"))
        if e is not None and e < cutoff:
            break                                  # rows are desc — past the window, stop
        out.append(m)
    return out


def _p_blockscout(address, contract, chain_key, days, decimals):
    return _blockscout_tokentx(address, contract, chain_key, days=days, decimals=decimals), False


def blockscout_cross_check(address, contract, chain_key, days=180, decimals=18, tolerance_pct=10.0):
    """SPEC-152 req 4: the DELIBERATE cross-check path — distinct from token_transfers()'s
    first-success-wins fallback, which never calls a second provider once one succeeds.
    Runs the seam's normal winner AND Blockscout directly, and diffs the row counts. A
    mismatch beyond `tolerance_pct` emits a caveat naming BOTH providers and BOTH values —
    "a fallback that quietly disagrees with the primary is worse than none." Returns
    (txs, source, caveat); caveat is None when they agree, one side is unreadable, or the
    chain isn't in Blockscout's verified set."""
    try:
        txs, source, _partial = token_transfers(address, contract, chain_key, days=days, decimals=decimals)
    except Exception:  # noqa: BLE001 — the primary path failing entirely isn't this fn's job
        txs, source = None, None
    if chain_key not in _blockscout_instances() or source == "blockscout":
        return txs, source, None
    try:
        bs_txs, _partial = _p_blockscout(address, contract, chain_key, days, decimals)
    except Exception:  # noqa: BLE001 — blockscout unreadable → no cross-check, not a caveat
        return txs, source, None
    if txs is None:
        return bs_txs, "blockscout", None
    n1, n2 = len(txs), len(bs_txs)
    if max(n1, n2) == 0:
        return txs, source, None
    diff_pct = abs(n1 - n2) / max(n1, n2) * 100
    if diff_pct > tolerance_pct:
        caveat = (f"blockscout/{source} row-count mismatch: blockscout={n2} rows, "
                  f"{source}={n1} rows (diff {diff_pct:.0f}% > {tolerance_pct:.0f}% tolerance)")
        return txs, source, caveat
    return txs, source, None


def _p_moralis(address, contract, chain_key, days, decimals):
    return _moralis_tokentx(address, contract=contract, chain=_MORALIS_CHAIN[chain_key], days=days), False


def _p_getlogs(address, contract, chain_key, days, decimals):
    if not contract:
        raise ProviderSkip("getlogs needs a contract (an address-less log scan is unbounded)")
    return _getlogs_transfers(address, contract, chain_key, decimals=decimals), True

# Ordered registry — earlier entries are consulted first; `chains` scopes a provider
# (None = every chain), `enabled` gates it at call time (key present etc.). local_index is
# FIRST (req 3, SPEC-102) — a tracked, fresh contract serves off SQLite with zero network/
# quota; anything else (untracked, un-ingested, stale) falls through unchanged.
_PROVIDERS = [
    {"name": "local_index", "chains": set(local_index.RPC_PROVIDER_TABLE), "fetch": _p_local_index},
    {"name": "etherscan", "chains": set(_ETHERSCAN_CHAINID), "fetch": _p_etherscan},
    # SPEC-152: FALLBACK, after local_index/Etherscan-V2, before Moralis on its (verified)
    # chains — quota relief, so it must actually sit in front of the metered provider.
    {"name": "blockscout", "chains": {"ethereum", "base", "arbitrum-one",
                                     "optimistic-ethereum", "polygon-pos", "hemi"},
     "fetch": _p_blockscout},
    {"name": "moralis", "chains": set(_MORALIS_CHAIN), "fetch": _p_moralis},
    {"name": "getlogs", "chains": None, "fetch": _p_getlogs},
]


def register_provider(name, fetch, chains=None, before=None, enabled=None):
    """SPEC-97: the socket for SPEC-101/102 — register a token-flow provider.
    fetch(address, contract, chain_key, days, decimals) -> (moralis-shaped txs, partial).
    `chains` scopes it (None = all); `before` names the provider to insert ahead of
    (e.g. before="moralis" puts a local BSC index in front of the metered vendor)."""
    entry = {"name": name, "chains": set(chains) if chains else None, "fetch": fetch}
    if enabled is not None:
        entry["enabled"] = enabled
    idx = len(_PROVIDERS)
    if before is not None:
        idx = next((i for i, p in enumerate(_PROVIDERS) if p["name"] == before), idx)
    _PROVIDERS.insert(idx, entry)
    return entry


def unregister_provider(name):
    _PROVIDERS[:] = [p for p in _PROVIDERS if p["name"] != name]


def providers_for(chain_key):
    """The ordered providers serving `chain_key` — an unsupported/disabled provider is
    skipped silently (no wasted call, no error)."""
    out = []
    for p in _PROVIDERS:
        if p["chains"] is not None and chain_key not in p["chains"]:
            continue
        en = p.get("enabled")
        try:
            if en is not None and not en():
                continue
        except Exception:  # noqa: BLE001 — a broken enable-probe = disabled, never a crash
            continue
        out.append(p)
    return out


def token_transfers(address, contract, chain_key, days=180, decimals=18):
    """The wallet-scoped token-flow read behind the SPEC-97 chain-scoped provider seam:
    ETH: etherscan → moralis → getlogs; BSC/rest: moralis → getlogs (SPEC 17 behavior).
    Returns (txs, source, partial). Raises MoralisError only when ALL providers fail —
    a MoralisQuotaExhausted (SPEC-123) takes priority over a later generic failure, since
    'the data layer is blind until reset' is more actionable than a downstream provider's
    unrelated error."""
    last_err = None
    quota_err = None
    for p in providers_for(chain_key):
        try:
            txs, partial = p["fetch"](address, contract, chain_key, days, decimals)
            return txs, p["name"], partial
        except ProviderSkip:
            continue                       # not applicable (no key / no contract) — silent
        except MoralisQuotaExhausted as e:
            quota_err = e
            continue                       # still worth trying a non-moralis fallback
        except Exception as e:  # noqa: BLE001 — real failure → fall through to the next provider
            last_err = e
            continue
    if quota_err is not None:
        raise quota_err
    raise MoralisError(f"all providers failed: {str(last_err)[:120]}")


def _safe_history_and_flows(ticker, days=90):
    """Lifetime/recent token-flow read per distribution/team safe via Moralis (SPEC 6).
    Moralis covers BSC (the free BscScan/Etherscan-V2 key cannot). Per-wallet calls run
    concurrently and are bounded, so this never hangs. Outbound destinations are tagged
    against known entities (CEX/VC) for the bid-pull/distribution read."""
    ticker = ticker.upper()
    try:
        tok = json.loads(WALLETS.read_text()).get("tokens", {}).get(ticker)
        if not tok:
            return ({"available": True, "wallets": []}, {"available": True, "recent": []})
        contract = (tok.get("contracts", {}) or {}).get("binance-smart-chain")
        safes = [w for w in tok.get("wallets", [])
                 if (w.get("tier") or "").lower() not in OP_TIERS
                 and w.get("chain") in _MORALIS_CHAIN][:10]   # bounded
        labels = _entity_labels()

        def one(w):
            chain = _MORALIS_CHAIN[w["chain"]]
            c = contract if w["chain"] == "binance-smart-chain" else (tok.get("contracts", {}) or {}).get(w["chain"])
            txs = _moralis_tokentx(w["address"], contract=c, chain=chain, days=days, max_pages=2)
            out = [t for t in txs if (t.get("from_address") or "").lower() == w["address"].lower()]
            hist = {"label": w["label"], "tier": w["tier"], "chain": w["chain"],
                    "tx_count": len(txs), "out_count": len(out),
                    "last_out_ts": (out[0].get("block_timestamp") if out else None)}
            flows = []
            for t in out[:5]:
                to = (t.get("to_address") or "").lower()
                flows.append({"from_label": w["label"], "to": t.get("to_address"),
                              "to_entity": labels.get(to), "token": t.get("token_symbol"),
                              "value": t.get("value_decimal") or t.get("value"),
                              "ts": t.get("block_timestamp")})
            return hist, flows

        with ThreadPoolExecutor(max_workers=6) as ex:
            results = list(ex.map(one, safes))
        hist = [h for h, _ in results]
        flows = [f for _, fl in results for f in fl]
        flows.sort(key=lambda x: x["ts"] or "", reverse=True)
        return ({"available": True, "wallets": hist, "source": "moralis"},
                {"available": True, "recent": flows[:15], "source": "moralis"})
    except Exception as e:  # noqa: BLE001
        unavail = {"available": False, "degraded": True, "reason": str(e)[:160]}
        return unavail, unavail


# SPEC 12 — supply concentration via GoPlus Token Security (free, no key, all EVM chains)
_GOPLUS_CHAIN = {"ethereum": "1", "binance-smart-chain": "56", "base": "8453",
                 "polygon": "137", "arbitrum": "42161", "optimism": "10",
                 "avalanche": "43114",   # SPEC 56
                 # SPEC-192 #1: non-EVM + long-tail-EVM chains GoPlus serves keyless
                 # (live-verified 2026-09-02, reports/RESEARCH-2026-09-02-onchain-chain-
                 # coverage.md — chain keys match config/tracked_wallets.json's own
                 # contract-dict vocabulary, i.e. coingecko asset_platform ids). Solana
                 # and Sui are NAME-segment endpoints (`/api/v1/<name>/token_security`),
                 # not the numeric `/token_security/<id>` path every other row uses —
                 # `_goplus_result` dispatches on membership in `_GOPLUS_NAME_CHAINS`.
                 "solana": "solana", "sui": "sui", "monad": "143", "berachain": "80094",
                 "bitlayer": "200901", "sonic": "146", "robinhood": "4663",
                 "mantle": "5000", "abstract": "2741", "world-chain": "480"}
# NOT on GoPlus (verified via its own /api/v1/supported_chains, SPEC-192 #1) — left
# BLIND, named: sei ("sei-v2" in config), hemi, ton ("the-open-network"), cardano,
# algorand. Hemi holders come from Blockscout instead (see _BLOCKSCOUT_HOLDERS_CHAINS).
_GOPLUS_NAME_CHAINS = {"solana", "sui"}


def _get_json(url, timeout=15, retries=2):
    """GET → JSON with a short retry — GoPlus free tier throttles bursts (transient 429)."""
    for attempt in range(retries + 1):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "onchain/1.0"})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.loads(r.read())
        except Exception:
            if attempt < retries:
                time.sleep(1.2)
    return None


# SPEC 34: coingecko platform-id → _GOPLUS_CHAIN key (so UNTRACKED names resolve a contract).
_CG_PLATFORM_TO_GOPLUS = {
    "ethereum": "ethereum", "binance-smart-chain": "binance-smart-chain",
    "base": "base", "polygon-pos": "polygon", "arbitrum-one": "arbitrum",
    "optimistic-ethereum": "optimism", "avalanche": "avalanche",   # SPEC 56
    # SPEC-192 #1: identity entries — config already stores these as coingecko
    # asset_platform ids, so no translation is needed (unlike the legacy EVM rows above).
    "solana": "solana", "sui": "sui", "monad": "monad", "berachain": "berachain",
    "bitlayer": "bitlayer", "sonic": "sonic", "robinhood": "robinhood",
    "mantle": "mantle", "abstract": "abstract", "world-chain": "world-chain",
}
# fallback chain preference when a name is deployed on several GoPlus-supported chains
_GOPLUS_CHAIN_PREF = ["binance-smart-chain", "ethereum", "avalanche", "base", "arbitrum",
                      "polygon", "optimism", "solana", "sui", "mantle", "berachain",
                      "bitlayer", "sonic", "robinhood", "abstract", "world-chain"]


def _goplus_result(contract, chain):
    """SPEC-192 #1: one GoPlus token_security call -> the raw `result[<key>]` dict for
    `contract` on `chain`, or {} on no-result/unsupported chain. Solana/Sui use a
    name-segment path (`/api/v1/<name>/token_security`) with a case-SENSITIVE address
    (base58 pubkey / Move `pkg::module::TYPE` coin-type) — every other (EVM) chain uses
    the numeric `/token_security/<chain_id>` path with a lowercased hex address. Shared
    by `goplus_chain_stats` and `_goplus_concentration` so both read the same shape."""
    gid = _GOPLUS_CHAIN.get(chain)
    if not gid:
        return {}
    if gid in _GOPLUS_NAME_CHAINS:
        key = contract
        url = f"https://api.gopluslabs.io/api/v1/{gid}/token_security?contract_addresses={key}"
    else:
        key = contract.lower()
        url = f"https://api.gopluslabs.io/api/v1/token_security/{gid}?contract_addresses={key}"
    d = _get_json(url)
    result = (d or {}).get("result") or {}
    if key in result:
        return result[key]
    for k, v in result.items():   # a case-normalization quirk must never read as "no result"
        if k.lower() == key.lower():
            return v
    return {}


def goplus_chain_stats(contract, chain):
    """SPEC 56: per-chain supply/holder stats (the primary-chain ranking input). One
    GoPlus token_security read; {} on failure — the ranker treats it as a dark chain."""
    res = _goplus_result(contract, chain)
    if not res:
        return {}
    out = {}
    try:
        out["total_supply"] = float(res.get("total_supply") or 0)
    except (TypeError, ValueError):
        out["total_supply"] = None
    hc = res.get("holder_count")
    out["holder_count"] = int(hc) if str(hc).isdigit() else None
    return out


def _cg_platforms_to_goplus(platforms):
    """{cg_platform: addr} → {goplus_chain_key: addr}, keeping only GoPlus-EVM-supported chains."""
    out = {}
    for plat, addr in (platforms or {}).items():
        key = _CG_PLATFORM_TO_GOPLUS.get(plat)
        if key and key in _GOPLUS_CHAIN and addr:
            out[key] = addr
    return out


def _coingecko_contracts(ticker):
    """SPEC 34: resolve a token's contract(s) from the coingecko/pull5 map (no tracked-config dep)."""
    try:
        from pull5 import coingecko_layer
        return _cg_platforms_to_goplus((coingecko_layer(ticker) or {}).get("contracts"))
    except Exception:  # noqa: BLE001
        return {}


def _blockscout_v2_base(base):
    """Etherscan-compat base (…/api) -> the v2 REST base (…/api/v2) for the SAME
    instance. Bare `.../api` suffix is the only shape `_blockscout_instances()` stores."""
    return (base[:-4] if base.endswith("/api") else base) + "/api/v2"


def _blockscout_holders(contract, chain_key, limit=10):
    """SPEC-192 #3: Blockscout v2 REST holders (`/api/v2/tokens/{addr}/holders`,
    50/page) — the ONLY keyless holders source on chains GoPlus doesn't cover (Hemi:
    GoPlus `token_security` returns 'main chain not supported'). Same output shape as
    `_goplus_concentration` (source='blockscout') so a caller never needs to branch on
    which provider answered. `_BLOCKSCOUT_HOLDERS_CHAINS` deliberately excludes Base
    (holders 500s on big tokens, live-verified) even though its flow instance works —
    a Base holders call degrades loudly here, never silently."""
    if chain_key not in _BLOCKSCOUT_HOLDERS_CHAINS:
        return {"available": False,
               "reason": f"no verified Blockscout HOLDERS instance for {chain_key!r}"}
    base = _blockscout_instances().get(chain_key)
    if not base:
        return {"available": False,
               "reason": f"no blockscout instance configured for {chain_key!r}"}
    v2 = _blockscout_v2_base(base)
    addr = contract.lower()
    meta = _get_json(f"{v2}/tokens/{addr}") or {}
    holders_d = _get_json(f"{v2}/tokens/{addr}/holders")
    if not holders_d or not isinstance(holders_d.get("items"), list):
        return {"available": False,
               "reason": "blockscout holders unavailable (dead instance / 500 / malformed body)"}
    try:
        total_supply = float(meta.get("total_supply") or 0)
    except (TypeError, ValueError):
        total_supply = 0.0
    hc = meta.get("holders_count")
    hs = []
    for h in holders_d["items"][:limit]:
        addr_h = (h.get("address") or {}).get("hash")
        try:
            val = float(h.get("value") or 0)
        except (TypeError, ValueError):
            val = 0.0
        pct = round(val / total_supply * 100, 3) if total_supply else None
        hs.append({"address": addr_h, "percent": pct, "tag": None, "is_contract": False,
                   "is_locked": False, "is_burn": _is_burn(addr_h)})
    tops = [h["percent"] for h in hs if h["percent"] is not None]
    return {"available": True, "source": "blockscout", "chain": chain_key,
            "holder_count": int(hc) if str(hc).isdigit() else hc,
            "top1_pct": max(tops) if tops else None,
            "top10_pct": round(sum(tops[:10]), 3) if tops else None,
            "burn_pct": None,
            "top1_ex_burn_pct": max([t for t, h in zip(tops, hs) if not h["is_burn"]], default=None),
            "holders": hs}


def _pick_goplus_chain(contracts):
    for k in _GOPLUS_CHAIN_PREF:
        if k in contracts:
            return k
    return next(iter(contracts), None)


_BURN_ADDRS = {"0x0000000000000000000000000000000000000000",
               "0x000000000000000000000000000000000000dead"}


def _is_burn(address):
    """SPEC 37: a well-known burn/null sink (0x0…0 or 0x…dEaD) — removed supply, NOT a holder."""
    a = (address or "").lower()
    return a in _BURN_ADDRS or a.endswith("000000000000dead")


def _goplus_concentration(contract, chain):
    """GoPlus top-holder read for one contract/chain. Coverage-labeled, never raises/hangs.
    SPEC 37: burned supply (0x…dEaD) is flagged `is_burn` and excluded from the operator
    concentration read (`top1_ex_burn_pct`, `burn_pct`) so a big burn ≠ a big operator whale."""
    if chain not in _GOPLUS_CHAIN:
        return {"available": False, "reason": f"GoPlus does not support chain {chain!r}"}
    res = _goplus_result(contract, chain)
    if not res:
        return {"available": False, "reason": "GoPlus no result"}
    hs = []
    for h in res.get("holders", []):
        # SPEC-192 #1: Solana rows key the address as `account`, not `address`.
        addr = h.get("address") or h.get("account")
        try:
            pct = round(float(h.get("percent", "0")) * 100, 3)
        except (TypeError, ValueError):
            pct = None
        hs.append({"address": addr, "percent": pct, "tag": h.get("tag") or None,
                   "is_contract": bool(int(h.get("is_contract", 0) or 0)),
                   "is_locked": bool(int(h.get("is_locked", 0) or 0)),
                   "is_burn": _is_burn(addr)})
    tops = [h["percent"] for h in hs if h["percent"] is not None]
    burn_pct = round(sum(h["percent"] for h in hs if h["is_burn"] and h["percent"]), 3)
    tops_ex_burn = [h["percent"] for h in hs if h["percent"] is not None and not h["is_burn"]]
    hc = res.get("holder_count")
    return {"available": True, "source": "goplus", "chain": chain,
            "holder_count": int(hc) if str(hc).isdigit() else hc,
            "top1_pct": max(tops) if tops else None,
            "top10_pct": round(sum(tops[:10]), 3) if tops else None,
            "burn_pct": burn_pct or None,
            "top1_ex_burn_pct": max(tops_ex_burn) if tops_ex_burn else None,
            "holders": hs[:10]}


_SPLIT_SUPPLY_PCT = 20.0   # SPEC 56: a 2nd chain carrying >= this much supply is read too


def _concentration_primary(tok):
    """SPEC 56: concentration read driven by the onboard-resolved chain ranking.

    Reads the primary chain (+ a 2nd chain when supply splits >= _SPLIT_SUPPLY_PCT),
    tags every holder with its chain and a supply-scaled `global_pct`, and carries a
    `coverage` block naming exactly what the engine could NOT see (unsupported chains +
    unreadable_supply_pct) — never a silent zero on a FOLKS-shaped token."""
    contracts = tok.get("contracts", {}) or {}
    ranked = tok.get("chains_ranked") or []
    supported = [c for c in ranked if c.get("supported") and c.get("chain") in contracts]
    unsupported = [{"chain": c["chain"], "supply_pct": c.get("supply_pct")}
                   for c in ranked if not c.get("supported")]
    primary = tok.get("primary_chain")
    read = [c for c in supported if not c.get("bridge_stub")
            and (c.get("supply_pct") or 0) >= _SPLIT_SUPPLY_PCT][:2]
    if not read:
        read = [c for c in supported if c["chain"] == primary] or supported[:1]
    if not read:
        return {"available": False,
                "reason": f"no readable chain among ranked ({[c.get('chain') for c in ranked]})"}

    merged, chains_read, hc_total = [], [], 0
    first = None
    for c in read:
        gp_key = c.get("goplus_chain") or _CG_PLATFORM_TO_GOPLUS.get(c["chain"])
        if not gp_key:
            continue
        r = _goplus_concentration(contracts[c["chain"]], gp_key)
        if not r.get("available"):
            continue
        first = first or r
        chains_read.append(c["chain"])
        share = (c.get("supply_pct") or 100.0) / 100.0 if len(read) > 1 else 1.0
        hc = r.get("holder_count")
        hc_total += hc if isinstance(hc, int) else 0
        for h in r.get("holders", []):
            h = dict(h)
            h["chain"] = c["chain"]
            h["global_pct"] = (round(h["percent"] * share, 3)
                               if h.get("percent") is not None else None)
            merged.append(h)
    if not first:
        return {"available": False, "reason": f"goplus unavailable on {[c['chain'] for c in read]}"}

    coverage = {"primary_chain": primary, "chains_read": chains_read,
                "unsupported": unsupported,
                "unreadable_supply_pct": tok.get("unreadable_supply_pct")}
    if len(chains_read) == 1:
        # single-chain read: keep chain-local percentages (today's semantics) + coverage
        out = dict(first)
        out["chain"] = chains_read[0]
        out["holders"] = [dict(h, chain=chains_read[0]) for h in first.get("holders", [])]
        out.update({"tracked": True, "discovery": False, "coverage": coverage})
        return out
    merged.sort(key=lambda h: -(h.get("global_pct") or 0))
    tops = [h["global_pct"] for h in merged if h.get("global_pct") is not None]
    burn_pct = round(sum(h["global_pct"] for h in merged if h.get("is_burn") and h.get("global_pct")), 3)
    tops_ex_burn = [h["global_pct"] for h in merged if h.get("global_pct") is not None and not h.get("is_burn")]
    return {"available": True, "source": "goplus", "chain": primary,
            "holder_count": hc_total or None,
            "top1_pct": (max(tops) if tops else None),
            "top10_pct": (round(sum(tops[:10]), 3) if tops else None),
            "burn_pct": burn_pct or None,
            "top1_ex_burn_pct": (max(tops_ex_burn) if tops_ex_burn else None),
            "holders": merged[:10],
            "tracked": True, "discovery": False, "coverage": coverage}


def _concentration(ticker):
    """Top-holder concentration (GoPlus; covers BSC which free BscScan can't). Tracked names use
    their config contract; UNTRACKED names fall back to the coingecko/pull5 contract map (SPEC 34,
    concentration ONLY — nonce/distribution still need onboarding) flagged discovery:true. Never
    raises/hangs."""
    try:
        tok = json.loads(WALLETS.read_text()).get("tokens", {}).get(ticker.upper())
        if tok:
            contracts = tok.get("contracts", {}) or {}
            # SPEC 56: a token with a resolved primary_chain reads where the supply LIVES
            # (split supply ≥20% on a second chain → merge the top 2, chain-tagged)
            if tok.get("primary_chain") and tok.get("chains_ranked"):
                return _concentration_primary(tok)
            chain = "binance-smart-chain" if "binance-smart-chain" in contracts else next(iter(contracts), None)
            if not chain or chain not in _GOPLUS_CHAIN:
                return {"available": False, "reason": f"no GoPlus-supported contract ({list(contracts)})"}
            r = _goplus_concentration(contracts[chain], chain)
            if r.get("available"):
                r["tracked"], r["discovery"] = True, False
            return r
        # SPEC 34: UNTRACKED → coingecko-contract fallback (the cheap, high-value §0.6 chip read).
        contracts = _coingecko_contracts(ticker)
        if not contracts:
            return {"available": False, "reason": "untracked; no coingecko contract / no GoPlus-supported chain"}
        chain = _pick_goplus_chain(contracts)
        r = _goplus_concentration(contracts[chain], chain)
        if r.get("available"):
            r["tracked"], r["discovery"] = False, True
        else:
            r["reason"] = f"untracked; {r.get('reason', 'goplus no result')}"
        return r
    except Exception as e:  # noqa: BLE001
        return {"available": False, "reason": str(e)[:120]}


# ── SPEC-101: holder-enumeration provider seam — Bitquery primary, GoPlus fallback ──
# The ticket's problem statement names Moralis as today's holder-enumeration source
# ("stays on the Moralis free tier"). Live-checking this codebase (memory:
# feedback_live_verify_vendor_api_tiers_before_spec — verify vendor-tier claims before a
# spec bets on them) found no Moralis-based holder read anywhere: _goplus_concentration
# (SPEC 12/56, free, keyless, already wired through _concentration/onboard seeding) is the
# ACTUAL current enumeration path. This seam keeps the ticket's real deliverable — an
# ordered, data-driven provider registry Bitquery can slot into ahead of the existing free
# read — while substituting the true current fallback (GoPlus) for the assumed one
# (Moralis); see REVIEW-REQUEST-SPEC-101 for the drift note.
#
# Envelope: {available, source, partial, holders:[{address,balance,share_pct}],
# top10_share_pct}. `balance` is None on the GoPlus path (it only reports %, not raw
# balances) — never fabricated.

class BitqueryError(Exception):
    """A real Bitquery failure (HTTP/GraphQL error/timeout) — falls through to GoPlus."""


class EnumerationError(Exception):
    """A holder provider ran but returned no usable result — falls through."""


BITQUERY_URL = "https://streaming.bitquery.io/graphql"
# Bitquery EVM schema network keys (scoped BSC+ETH per spec; add chains here, not elsewhere).
_BITQUERY_CHAIN = {"binance-smart-chain": "bsc", "ethereum": "eth"}
_HOLDER_DEFAULT_N = 50
_HOLDER_CFG_PATH = ROOT / "config" / "holder_enumeration.json"
_HOLDER_CFG_DEFAULTS = {"cache_ttl_h": 6, "daily_budget": 800}
_BITQUERY_CACHE = {}


def _holder_cfg():
    cfg = dict(_HOLDER_CFG_DEFAULTS)
    try:
        d = json.loads(_HOLDER_CFG_PATH.read_text())
        cfg.update({k: v for k, v in d.items() if k in _HOLDER_CFG_DEFAULTS})
    except (OSError, json.JSONDecodeError):
        pass
    return cfg


def _bitquery_key():
    try:
        return json.loads((ROOT / "config" / "secrets.json").read_text()).get("bitquery_api_key")
    except Exception:  # noqa: BLE001
        return None


def _bitquery_call(query, variables, key, timeout=15):
    """One live Bitquery GraphQL POST → parsed JSON (separate so tests patch the wire)."""
    body = json.dumps({"query": query, "variables": variables}).encode()
    req = urllib.request.Request(
        BITQUERY_URL, data=body,
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {key}",
                 "User-Agent": "onchain/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())


# EVM BalanceUpdates aggregate — the current (2026) Bitquery V2 pattern for token-holder
# balances. NOTE: Bitquery's V1/V2/EAP split moves fast (spec's own warning) and this could
# not be live-verified (no bitquery_api_key in config/secrets.json) — see REVIEW-REQUEST.
_BITQUERY_HOLDERS_QUERY = """
query ($token: String!, $network: evm_network!, $limit: Int!) {
  EVM(dataset: combined, network: $network) {
    BalanceUpdates(
      where: {Currency: {SmartContract: {is: $token}}}
      orderBy: {descendingByField: "balance"}
      limit: {count: $limit}
    ) {
      BalanceUpdate { Address }
      balance: sum(of: BalanceUpdate_Amount)
    }
  }
}
"""


def _bitquery_holders(contract, chain_key, top_n=_HOLDER_DEFAULT_N, retries=1):
    """Throttled + cached + metered Bitquery holder-enumeration read. Returns
    {holders:[{address,balance,share_pct}], top10_share_pct}. Raises ProviderSkip when no
    key or an unsupported chain, BitqueryError on a real failure."""
    key = _bitquery_key()
    if not key:
        raise ProviderSkip("no bitquery_api_key")
    network = _BITQUERY_CHAIN.get(chain_key)
    if not network:
        raise ProviderSkip(f"bitquery unsupported chain {chain_key}")
    contract = contract.lower()
    cfg = _holder_cfg()
    ttl = cfg["cache_ttl_h"] * 3600
    ckey = (contract, network, top_n)
    hit = _BITQUERY_CACHE.get(ckey)
    if hit and (time.time() - hit[0]) < ttl:
        return hit[1]
    variables = {"token": contract, "network": network, "limit": top_n}
    last_err = None
    for attempt in range(retries + 1):
        try:
            d = _bitquery_call(_BITQUERY_HOLDERS_QUERY, variables, key)
            provider_quota.record_call("bitquery")   # meter the LIVE call (cache hits don't)
            break
        except Exception as e:  # noqa: BLE001 — HTTP/timeout/bad JSON
            last_err = e
            if attempt < retries:
                time.sleep(1.0 * (attempt + 1))
    else:
        raise BitqueryError(f"bitquery unreachable: {str(last_err)[:120]}")
    if d.get("errors"):
        raise BitqueryError(f"bitquery GraphQL errors: {str(d['errors'])[:160]}")
    rows = (((d.get("data") or {}).get("EVM") or {}).get("BalanceUpdates")) or []
    parsed, total = [], 0.0
    for r in rows:
        addr = ((r.get("BalanceUpdate") or {}).get("Address") or "").lower() or None
        if not addr:
            continue
        try:
            bal = float(r.get("balance") or 0)
        except (TypeError, ValueError):
            bal = 0.0
        parsed.append((addr, bal))
        total += bal
    holders = [{"address": a, "balance": b, "share_pct": round(b / total * 100, 3) if total else None}
              for a, b in parsed]
    out = {"holders": holders,
          "top10_share_pct": (round(sum(h["share_pct"] for h in holders[:10]
                                        if h["share_pct"] is not None), 3) if holders else None)}
    _BITQUERY_CACHE[ckey] = (time.time(), out)
    return out


def _p_bitquery_holders(contract, chain_key, top_n):
    r = _bitquery_holders(contract, chain_key, top_n=top_n)
    return r["holders"], r.get("top10_share_pct"), False


def _p_goplus_holders(contract, chain_key, top_n):
    """The existing free/keyless GoPlus top-holder read (SPEC 12/56), adapted to the
    enumerate_holders envelope. GoPlus caps at 10 holders (`_goplus_concentration`) — a
    caller asking for more than that gets `partial:True`, never a silently short list."""
    if chain_key not in _GOPLUS_CHAIN:
        raise ProviderSkip(f"goplus unsupported chain {chain_key}")
    r = _goplus_concentration(contract, chain_key)
    if not r.get("available"):
        raise EnumerationError(r.get("reason") or "goplus unavailable")
    holders = [{"address": h["address"], "balance": None, "share_pct": h["percent"]}
              for h in r.get("holders", []) if h.get("address")]
    partial = top_n > len(holders)
    return holders[:top_n], r.get("top10_pct"), partial


# Ordered registry — earlier entries are consulted first. Mirrors the token_transfers seam
# (register_provider/providers_for) one level up, scoped to holder enumeration.
_HOLDER_PROVIDERS = [
    {"name": "bitquery", "chains": set(_BITQUERY_CHAIN), "fetch": _p_bitquery_holders},
    {"name": "goplus", "chains": set(_GOPLUS_CHAIN), "fetch": _p_goplus_holders},
]


def register_holder_provider(name, fetch, chains=None, before=None):
    """The socket a future vendor (or SPEC-102's local indexer) plugs a holder-enumeration
    provider into without touching this file again. fetch(contract, chain_key, top_n) ->
    (holders, top10_share_pct, partial)."""
    entry = {"name": name, "chains": set(chains) if chains else None, "fetch": fetch}
    idx = len(_HOLDER_PROVIDERS)
    if before is not None:
        idx = next((i for i, p in enumerate(_HOLDER_PROVIDERS) if p["name"] == before), idx)
    _HOLDER_PROVIDERS.insert(idx, entry)
    return entry


def unregister_holder_provider(name):
    _HOLDER_PROVIDERS[:] = [p for p in _HOLDER_PROVIDERS if p["name"] != name]


def holder_providers_for(chain_key):
    return [p for p in _HOLDER_PROVIDERS if p["chains"] is None or chain_key in p["chains"]]


def enumerate_holders(contract, chain_key, top_n=None):
    """SPEC-101: the contract-scoped top-holder enumeration read, provider-seamed
    (Bitquery -> GoPlus -> unavailable). Radar/concentration consumers can call this with no
    interface change — it returns the same shape `_goplus_concentration` already produces
    plus `balance`/`source`/`partial`. Never raises."""
    top_n = top_n if top_n is not None else _HOLDER_DEFAULT_N
    last_err = None
    for p in holder_providers_for(chain_key):
        try:
            holders, top10, partial = p["fetch"](contract, chain_key, top_n)
            return {"available": True, "source": p["name"], "partial": partial,
                    "holders": holders, "top10_share_pct": top10}
        except ProviderSkip:
            continue                       # not applicable (no key / unsupported chain)
        except Exception as e:  # noqa: BLE001 — real failure → fall through
            last_err = e
            continue
    return {"available": False, "reason": f"all holder providers failed: {str(last_err)[:120]}"}


# ── SPEC-72: fired-safe → operator-aggregator distribution resolution ───────────
# The brief's nonce signal goes DORMANT/newly_fired:[] once the surveillance sweep has
# CONSUMED a fire (advanced the baseline), so a later read is blind to a tracked safe that
# is actively feeding the operator's distribution aggregator (the 2026-06-15 BSB
# QUIET-14.75M → staging → XTOKEN-BILL-AGGREGATOR $2.87M dex_swap_sell miss). This resolves a
# FIRED safe's flow (the verify_wallet path) and follows the staging chain ONE bounded run
# into the aggregator's dex_swap_sell USDT settlement leg — quota-gated on an actual fire.
# Memory: feedback_resolve_fired_wallet_tx_before_classifying, feedback_check_stable_leg…
_NONCE_FIRE_RE = re.compile(r"\[([^\]]*)\]")
_RESOLVE_MAX_SAFES = 2        # bound the resolution to the fired safes (quota discipline)
_RESOLVE_MAX_HOPS = 2         # bound the staging follow into the aggregator (no unbounded recursion)
_RESOLVE_DAYS = 14            # recent window — the fire is a live distribution, not a lifetime audit
# SPEC-73: a separate, LONGER-memory surface for "this name distributed recently" — distinct from
# the 6h live-escalation gate (TOKEN_OUT_WINDOW_SEC, left unchanged). A fire >6h but <24h old no
# longer flips the LIVE signal to DISTRIBUTING (the live diff has aged out), but it must still
# carry a recent_distribution line + a non-NEUTRAL bias (the 2026-06-15 BSB $5.1M/24h @ T+8h miss).
RECENT_DIST_WINDOW_SEC = 24 * 3600   # the 24h distribution-memory window
_RECENT_DIST_MIN_USD = 50_000        # below this, a recent fire is dust — stays NEUTRAL
_DRAINED_DUST = 0.0                  # balanceOf <= this after distributing = drained to zero


def _to_epoch(ts):
    """Normalize a fire timestamp (ISO-8601 string OR epoch number OR None) → epoch seconds.
    None (a live diff carries no ts) → None, treated by callers as 'now / live'."""
    if ts is None:
        return None
    if isinstance(ts, (int, float)):
        return float(ts)
    return _parse_ts(ts)


def _fmt_usd(v):
    """Compact USD magnitude for a headline/bias line ($5.1M / $214K / $90)."""
    v = abs(v or 0)
    if v >= 1e6:
        return f"${v / 1e6:.1f}M"
    if v >= 1e3:
        return f"${v / 1e3:.0f}K"
    return f"${v:.0f}"


def _verify_wallet(address, token, days=_RESOLVE_DAYS):
    """SPEC-72: the verify_wallet flow-resolution path, lazily imported (verify_wallet imports
    onchain → a module-load import would be circular). Returns the build_verify dict; an explicit
    {available:False} on any failure (resolution NEVER raises into the composed verdict)."""
    try:
        from verify_wallet import build_verify
        return build_verify(address, token, days=days)
    except Exception as e:  # noqa: BLE001
        return {"available": False, "reason": f"verify unavailable: {str(e)[:80]}"}


# SPEC-89: the nonce/signal layer reads STAGING, not actual token transfers — so a QUIET
# signal can hide a top holder (or cluster escrow) actively DISTRIBUTING (the BLESS 0x73d8 /
# H 0x28e2ea cases: onchain QUIET, verify_wallet DISTRIBUTING). verify_wallet the top holders
# so the chips read answers WHO holds AND WHICH WAY they are moving — the nonce layer answers
# neither. [[feedback_onchain_quiet_is_false_negative_verify_wallet_before_long]]
_TOP_HOLDER_N = 3   # default top-N holders to verify (§8 "default 3–5"); quota-frugal
# GoPlus holder `tag`s that mean "not an operator wallet" — verifying a CEX hot wallet or an
# LP pool burns quota and false-flags DISTRIBUTING (they move constantly); skip by tag.
_NONOPERATOR_TAGS = ("binance", "bybit", "gate", "kraken", "bitget", "okx", "kucoin", "mexc",
                     "htx", "coinbase", "exchange", "cex", "pancake", "uniswap", "router",
                     "pool", "swap", " lp", "dex")


def _top_holder_distribution(ticker, concentration, top_n=_TOP_HOLDER_N, verify_fn=None):
    """SPEC-89 — verify_wallet the top N holders so a QUIET nonce signal can't hide a top
    holder / cluster escrow that is actively DISTRIBUTING (the exit-liquidity trap a deep-neg
    squeeze-LONG would walk into). Moralis-quota-aware: degrades to checked:False (never blocks,
    never burns calls on an empty/exhausted read). The cluster escrow is captured via its
    top-holder position (it holds large supply); CEX/DEX-tagged + burn holders are skipped.

    Returns {checked, distributing, holders:[{address,source,verdict,last_out_ts,
    sell_destinations}], n_checked, reason?}."""
    verify_fn = verify_fn or _verify_wallet
    conc = concentration or {}
    holders = conc.get("holders") or []
    # candidate set: the largest non-burn, non-exchange/pool holders (the operator-relevant chips)
    candidates = []
    for h in holders:
        addr = h.get("address")
        if not addr or h.get("is_burn") or _is_burn(addr):
            continue
        tag = (h.get("tag") or "").lower()
        if tag and any(t in tag for t in _NONOPERATOR_TAGS):
            continue
        candidates.append(addr)
        if len(candidates) >= top_n:
            break
    if not candidates:
        return {"checked": False, "distributing": False, "n_checked": 0, "holders": [],
                "reason": "no operator-relevant top holders to verify"}
    # quota gate: a top-holder sweep is the heaviest Moralis path — never start it exhausted.
    try:
        if moralis_quota.status().get("exhausted"):
            return {"checked": False, "distributing": False, "n_checked": 0, "holders": [],
                    "reason": "moralis quota exhausted — distribution unverified (re-check, §3)"}
    except Exception:  # noqa: BLE001 — a meter read never blocks the verdict
        pass
    out, distributing = [], False
    for addr in candidates:
        try:
            v = verify_fn(addr, ticker)
        except Exception:  # noqa: BLE001 — one wallet's failure never sinks the sweep
            continue
        if not v.get("available"):
            continue
        verdict = v.get("verdict")
        # Key on verify_wallet's headline VERDICT (it already prioritizes ACCUMULATING > DISTRIBUTING,
        # so a net-buyer with a stray recent out is NOT flagged as a distribution trap — that would
        # wrongly kill a valid operator-aligned long). "matches the direct verify_wallet" (DoD).
        is_dist = verdict == "DISTRIBUTING"
        out.append({"address": addr, "verdict": verdict,
                    "last_out_ts": v.get("last_out_ts"),
                    "distributing": is_dist,
                    "sell_destinations": (v.get("sell_destinations") or [])[:3]})
        if is_dist:
            distributing = True
    return {"checked": bool(out), "distributing": distributing, "n_checked": len(out),
            "holders": out}


def _fired_safe_addresses(ticker, nonce_state, alerts_fn=None):
    """SPEC-72: the tracked operator safes that FIRED this window for `ticker` — the gate for a
    distribution resolution (quota: resolve only what actually fired). Union of two sources:
      - LIVE: nonce_state['escalation_fired'] (a fresh diff caught the tick), and
      - CONSUMED: an unconsumed nonce_surveil HIGH alert (the sweep advanced the baseline → a
        later read sees newly_fired:[]; the address is recovered by mapping the alert's fired
        LABEL back to the tracked wallet).
    Returns [{address, label, tier, chain, fired_ts}], deduped by address; [] when nothing fired.
    SPEC-73: fired_ts carries WHEN the fire happened — None for a LIVE diff (treated as now), the
    consumed alert's ts otherwise — so the caller can split the 6h live window from the 24h memory."""
    out, seen = [], set()
    for f in (nonce_state.get("escalation_fired") or []):
        a = (f.get("address") or "").lower()
        if a and a not in seen:
            seen.add(a)
            out.append({"address": f.get("address"), "label": f.get("label"),
                        "tier": f.get("tier"), "chain": f.get("chain") or "binance-smart-chain",
                        "fired_ts": None})
    try:
        if alerts_fn is None:
            import inbox
            alerts_fn = inbox.alerts_for
        alerts = alerts_fn(ticker) or {}
    except Exception:  # noqa: BLE001 — inbox read never blocks the composed verdict
        alerts = {}
    events = [e for e in (alerts.get("events") or [])
              if e.get("source") == "nonce_surveil" and e.get("severity") == "HIGH"]
    if not events:
        return out
    by_label = {}
    try:
        tok = json.loads(WALLETS.read_text()).get("tokens", {}).get(ticker.upper(), {})
        for w in tok.get("wallets", []):
            if w.get("label"):
                by_label.setdefault(w["label"], w)
    except Exception:  # noqa: BLE001
        by_label = {}
    for e in events:
        for grp in _NONCE_FIRE_RE.findall(e.get("msg") or ""):
            for part in grp.split(","):
                label = part.split("(")[0].strip()
                w = by_label.get(label)
                if not w:
                    continue
                a = (w.get("address") or "").lower()
                if a and a not in seen:
                    seen.add(a)
                    out.append({"address": w["address"], "label": w["label"],
                                "tier": w.get("tier"), "chain": w.get("chain") or "binance-smart-chain",
                                "fired_ts": e.get("ts")})
    return out


def _follow_to_aggregator(dest_addr, dest_label, ticker, verify_fn, days=_RESOLVE_DAYS,
                          max_hops=_RESOLVE_MAX_HOPS):
    """SPEC-72: from a fired safe's staging-internal destination, follow the operator's internal
    forwarding chain a BOUNDED number of hops until a dex_swap_sell USDT settlement leg surfaces
    (the aggregator that is actually executing the distribution). Returns the aggregator dict, or
    None if no dex_swap_sell is reached within `max_hops` (no unbounded recursion)."""
    cur_addr, cur_label = dest_addr, dest_label
    for _ in range(max_hops):
        if not cur_addr:
            return None
        v = verify_fn(cur_addr, ticker, days=days)
        if not isinstance(v, dict) or not v.get("available"):
            return None
        st = v.get("settlement") or {}
        if st.get("mode") == "dex_swap_sell":
            return {"address": cur_addr, "label": cur_label, "mode": "dex_swap_sell",
                    "usd_in": st.get("usd_in"), "amount_in": st.get("amount_in"),
                    "asset": st.get("asset"), "n_settlements": st.get("n_settlements")}
        nxt = next((d for d in (v.get("sell_destinations") or [])
                    if d.get("dest_kind") == "staging-internal" and d.get("address")), None)
        if not nxt:
            return None
        cur_addr, cur_label = nxt["address"], nxt.get("label")
    return None


def _flow_confirms_token_out(v, distributed_usd):
    """SPEC-75: did this fired wallet actually move the TRACKED TOKEN OUT this window? A nonce
    jump alone (the 2026-06-15 BILL MM-PROG-60K 48-nonce churn on non-BILL activity, n_out=0)
    is NOT distribution and must never be named the lead distributor. Confirmed when the wallet
    has a tracked-token OUT (out_count>0), a net token OUTFLOW (net_flow_window<0), or a priced
    distribution leg (own/aggregator dex_swap_sell → distributed_usd>0)."""
    if (v.get("out_count") or 0) > 0:
        return True
    net = v.get("net_flow_window")
    if isinstance(net, (int, float)) and net < 0:
        return True
    if (distributed_usd or 0) > 0:
        return True
    return False


def _flow_distributed_usd(v, safe_agg):
    """SPEC-73: the USD a fired safe actually pushed out this window — its OWN dex_swap_sell
    settlement leg if it sold directly, else the aggregator it feeds (the staging chain's
    dex_swap_sell), else the magnitude of its net token-out priced (the fallback)."""
    own = v.get("settlement") or {}
    if own.get("mode") == "dex_swap_sell" and own.get("usd_in"):
        return abs(own["usd_in"])
    if safe_agg and safe_agg.get("usd_in"):
        return abs(safe_agg["usd_in"])
    nfu = v.get("net_flow_window_usd")
    return abs(nfu) if isinstance(nfu, (int, float)) else None


def _resolve_fired_distribution(ticker, fired_safes, verify_fn=None, days=_RESOLVE_DAYS,
                                max_safes=_RESOLVE_MAX_SAFES, now=None):
    """SPEC-72: resolve each FIRED safe's flow (verify_wallet path — net token-out,
    sell_destinations, dest_kind) and, when it routes to an operator-controlled (staging-internal)
    sink, follow ONE bounded chain into the aggregator's dex_swap_sell settlement leg.

    SPEC-73: per flow also resolve `distributed_usd` (the USD it pushed out — its own or its
    aggregator's dex_swap_sell leg), `drained_to_zero` (the safe's real balanceOf is now ~0), and
    carry `fired_ts`; `live` is True when ANY fire is within the 6h live window (or has no ts =
    live diff) — only a live fire flips the LIVE signal; older-but-<24h fires feed the memory.
    Returns a {resolved, flows, aggregator, distributing, staging, live} summary, or None."""
    if not fired_safes:
        return None
    verify_fn = verify_fn or _verify_wallet
    now = now if now is not None else time.time()
    flows, aggregator, seen = [], None, set()
    for s in fired_safes[:max_safes]:
        addr = (s.get("address") or "").lower()
        if not addr or addr in seen:
            continue
        seen.add(addr)
        v = verify_fn(s["address"], ticker, days=days)
        if not isinstance(v, dict) or not v.get("available"):
            continue
        top = (v.get("sell_destinations") or [None])[0]
        # a staging-internal dest = the operator's consolidation sink → follow to the aggregator
        safe_agg = None
        if top and top.get("dest_kind") == "staging-internal":
            safe_agg = _follow_to_aggregator(top.get("address"), top.get("label"),
                                             ticker, verify_fn, days=days)
        bal = v.get("balance_now") or {}
        drained = bool(bal.get("available") and bal.get("value") is not None
                       and bal.get("value") <= _DRAINED_DUST)
        distributed_usd = _flow_distributed_usd(v, safe_agg)
        flow = {"safe_label": s.get("label"), "safe_address": s["address"],
                "safe_tier": s.get("tier"), "verdict": v.get("verdict"),
                "net_token_out": v.get("net_flow_window"), "net_token_out_usd": v.get("net_flow_window_usd"),
                "out_count": v.get("out_count"),   # SPEC-75: tracked-token OUTs this window (n_out)
                "distribution_mode": v.get("distribution_mode"),
                "dest": (top or {}).get("address"), "dest_label": (top or {}).get("label"),
                "dest_kind": (top or {}).get("dest_kind"),
                "fired_ts": s.get("fired_ts"),
                "distributed_usd": distributed_usd,
                # SPEC-75: confirmed = the wallet actually moved the tracked token OUT this window.
                # A nonce-only fire (n_out=0, no flow) is NOT a distributor and never the lead.
                "confirmed_out": _flow_confirms_token_out(v, distributed_usd),
                "drained_to_zero": drained}
        flows.append(flow)
        if aggregator is None and safe_agg:
            aggregator = safe_agg
    if not flows:
        return None
    # SPEC-75: rank the FIRING wallets by confirmed token-out / settlement magnitude so the
    # headline is the wallet that actually moved size, not the biggest nonce jump.
    confirmed_flows = sorted([f for f in flows if f.get("confirmed_out")],
                             key=lambda f: -abs(f.get("distributed_usd") or f.get("net_token_out") or 0))
    nonce_only_flows = [f for f in flows if not f.get("confirmed_out")]
    # the aggregate verdict reflects CONFIRMED flows only — a nonce-only fire can't drive it.
    distributing = any(f.get("verdict") in ("DISTRIBUTING", "SEEDED-STAGING") for f in confirmed_flows)
    staging = bool(aggregator) or any(f.get("dest_kind") == "staging-internal" for f in confirmed_flows)
    live = any((_to_epoch(f.get("fired_ts")) is None
                or (now - _to_epoch(f.get("fired_ts"))) <= TOKEN_OUT_WINDOW_SEC) for f in confirmed_flows)
    return {"resolved": True, "flows": flows, "aggregator": aggregator,
            "confirmed_flows": confirmed_flows, "nonce_only_flows": nonce_only_flows,
            "any_confirmed": bool(confirmed_flows),
            "distributing": distributing, "staging": staging, "live": live}


def _recent_distribution(distribution, now, window_sec=RECENT_DIST_WINDOW_SEC):
    """SPEC-73: aggregate the resolved fired-safe flows into a 24h distribution-MEMORY surface,
    separate from the live signal. Includes a flow when its fire is within `window_sec` (a live
    diff, fired_ts None, always counts). Returns {window_h, total_usd, n_safes, drained_to_zero,
    last_fire_ts}, or None when nothing recent / the total is dust. Reuses the SPEC-72
    resolutions already computed — no extra verify_wallet calls, no extra quota."""
    if not distribution or not distribution.get("resolved"):
        return None
    recent, last_e, last_raw = [], None, None
    for f in (distribution.get("flows") or []):
        e = _to_epoch(f.get("fired_ts"))
        if e is not None and (now - e) > window_sec:
            continue                                  # aged out of the 24h memory
        recent.append(f)
        if e is not None and (last_e is None or e > last_e):
            last_e, last_raw = e, f.get("fired_ts")
    if not recent:
        return None
    total = round(sum(abs(f.get("distributed_usd") or 0) for f in recent))
    if total < _RECENT_DIST_MIN_USD:
        return None                                   # dust → no recent-distribution surface
    drained = [f.get("safe_label") for f in recent if f.get("drained_to_zero") and f.get("safe_label")]
    return {"window_h": round(window_sec / 3600), "total_usd": total,
            "n_safes": len(recent), "drained_to_zero": drained, "last_fire_ts": last_raw}


def _recent_dist_line(rd, signal):
    """SPEC-73: the bias line for a recent-but-not-live distribution wave."""
    n = rd["n_safes"]
    seg = f"{n} safe{'s' if n != 1 else ''}"
    dz = len(rd.get("drained_to_zero") or [])
    if dz:
        seg += f", {dz} drained to zero"
    return (f"RECENT-DISTRIBUTION ~{_fmt_usd(rd['total_usd'])}/{rd['window_h']}h ({seg}) — "
            f"§8 Stage-5 distributed recently (live signal {signal})")


def _bias(score, signal, escalation_fired=None, recent_distribution=None):
    # SPEC-72: a resolved fired-safe → operator-aggregator distribution (signal flipped off
    # DORMANT) — name the safe(s); this is §8 Stage-5 consolidation/dump, never NEUTRAL-CONSTRUCTIVE.
    if signal in ("DISTRIBUTING", "STAGING"):
        names = ", ".join(f.get("label") for f in (escalation_fired or []) if f.get("label"))
        who = names or "tracked safe"
        verb = "DISTRIBUTING" if signal == "DISTRIBUTING" else "STAGING"
        return f"{verb} (tracked safe {who} → operator aggregator — §8 Stage-5 consolidation/dump)"
    if signal == "ESCALATION":
        # SPEC 55: name the REAL fired safe(s) — escalation_fired already excludes the
        # cex/operational/untiered noise, so the headline can't be driven by a CEX wallet.
        names = ", ".join(f.get("label") for f in (escalation_fired or []) if f.get("label"))
        who = names or "mapped operator safe"
        return f"DISTRIBUTION FIRING ({who} nonce ticked — bid-pull/distribution, §8 Stage-5)"
    if signal == "NONCE-ONLY-WATCH":
        # SPEC-75: fired safes ticked nonces but NONE confirmed a tracked-token OUT this window
        # (the BILL MM-PROG-60K 48-nonce churn on non-token activity) → NOT distribution; watch
        # for the token-out before calling it firing.
        return ("NONCE-ONLY WATCH (fired safes ticked nonces but moved no tracked token this "
                "window — not distribution; watch for the token-out, §8)")
    if signal == "BASELINE_SETTLING":
        return ("BASELINE SETTLING (untiered candidates fired — Designer tiering pass "
                "pending; NOT a §8 escalation)")
    # SPEC-73: DORMANT-now but distributed-recently — the live signal aged past the 6h window,
    # but the name pushed out millions this same day. Surface it (overrides NEUTRAL-CONSTRUCTIVE).
    if recent_distribution and recent_distribution.get("total_usd", 0) >= _RECENT_DIST_MIN_USD:
        return _recent_dist_line(recent_distribution, signal)
    if score <= -10:
        return "LEAN BEARISH (distribution loading)"
    if score >= 10:
        return "NEUTRAL-CONSTRUCTIVE (safes dormant / supply locked — NEUTRAL at a fresh-ATH deep-neg, not bullish, §4)"
    return "NEUTRAL"


def _unsupported_chains(ticker):
    """SPEC 58 — state, independent of the GoPlus read, how much supply lives on chains the
    engine CANNOT see (non-EVM: algorand/sei/monad on FOLKS). Reads the onboard config so the
    number survives even when the concentration read returns n/a (the FOLKS live miss).

    Returns {unsupported_chains:[{chain, supply_pct}], unreadable_supply_pct}. The config
    carries supply_pct=null for non-EVM chains; when exactly one unsupported chain is genuinely
    non-EVM (a non-0x contract — algorand's ASA id, vs monad/sei EVM bridge stubs) the unreadable
    remainder is attributed to it — that is where the unseen supply lives."""
    try:
        tok = json.loads(WALLETS.read_text()).get("tokens", {}).get(ticker.upper())
    except Exception:  # noqa: BLE001
        tok = None
    if not tok:
        return {"unsupported_chains": [], "unreadable_supply_pct": None}
    ranked = tok.get("chains_ranked") or []
    contracts = tok.get("contracts", {}) or {}
    unsupported = [{"chain": c["chain"], "supply_pct": c.get("supply_pct")}
                   for c in ranked if not c.get("supported")]
    unreadable = tok.get("unreadable_supply_pct")

    def _is_evm(addr):
        return isinstance(addr, str) and addr.lower().startswith("0x")
    if unsupported and unreadable is not None:
        native = [u for u in unsupported if not _is_evm(contracts.get(u["chain"]))]
        if len(native) == 1 and native[0]["supply_pct"] is None:
            native[0]["supply_pct"] = unreadable
    return {"unsupported_chains": unsupported, "unreadable_supply_pct": unreadable}


def _attribute_confirmed_fires(nonces, distribution):
    """SPEC-75: gate the per-wallet distributor attribution on a CONFIRMED tracked-token OUT this
    window. Re-rank escalation_fired so confirmed distributors lead by token-out magnitude, demote
    nonce-only fires (n_out=0 — ticked a nonce but moved nothing, the 2026-06-15 BILL MM-PROG-60K
    48-nonce churn) into a separate nonce_only_fired bucket, and never name a nonce-only wallet the
    lead. A fired safe the resolver did NOT cover (quota cap / provider down) is KEPT but ranked
    after the confirmed ones (degrade-explicit — never dropped blindly, never the headline).
    Returns (escalation_fired, nonce_only_fired, none_confirmed) where none_confirmed is True only
    when every fired safe was resolved AND none confirmed a token-out (→ downgrade off FIRING)."""
    fired_list = list(nonces.get("escalation_fired") or [])
    if not (distribution and distribution.get("resolved")):
        return fired_list, [], False
    by_addr = {(f.get("address") or "").lower(): f for f in fired_list}
    confirmed = distribution.get("confirmed_flows") or []      # already magnitude-sorted by the resolver
    nonce_only = distribution.get("nonce_only_flows") or []
    resolved_addrs = {(f.get("safe_address") or "").lower() for f in (confirmed + nonce_only)}

    def _entry(flow):
        # prefer the spine's escalation_fired entry (carries nonce_prev/nonce_now for the render
        # line); fall back to a synthesised entry from the resolved flow.
        a = (flow.get("safe_address") or "").lower()
        base = dict(by_addr.get(a) or {"label": flow.get("safe_label"),
                                       "address": flow.get("safe_address"), "tier": flow.get("safe_tier")})
        base.update({"confirmed_out": flow.get("confirmed_out"),
                     "distributed_usd": flow.get("distributed_usd"),
                     "net_flow_window": flow.get("net_token_out"),
                     "dest_kind": flow.get("dest_kind"), "verdict": flow.get("verdict")})
        return base

    confirmed_entries = [_entry(f) for f in confirmed]
    nonce_only_entries = [_entry(f) for f in nonce_only]
    unresolved = [e for e in fired_list if (e.get("address") or "").lower() not in resolved_addrs]
    escalation_fired = confirmed_entries + unresolved
    none_confirmed = bool((confirmed or nonce_only) and not confirmed and not unresolved)
    return escalation_fired, nonce_only_entries, none_confirmed


def _freshness_layer(ticker, thd):
    """SPEC-98: the rotation-freshness verdict for the composed read. Only computed when a
    SHORT thesis is LIVE on the token AND there is a distribution context (a checked
    top-holder sweep) — that is when the false-pause trap can cost money. Never raises,
    never hangs the composed read; None = out of scope / failed."""
    try:
        import rotation_freshness as RF
        if not RF._live_short_thesis(ticker):
            return None
        if not (thd or {}).get("checked"):
            return None
        outs = [h.get("last_out_ts") for h in (thd.get("holders") or []) if h.get("last_out_ts")]
        return RF.build_freshness(ticker, last_out_ts=(max(outs) if outs else None),
                                  live_short_thesis=True)
    except Exception:  # noqa: BLE001
        return None


def build_onchain(ticker, depth="fast"):
    """SPEC 7 — the composed whole-coin picture. Nonce spine always returns fast;
    the heavier layers run concurrently under budget and are coverage-labeled. Never hangs."""
    ticker = ticker.upper().replace("USDT", "")
    with ThreadPoolExecutor(max_workers=4) as ex:
        f_nonce = ex.submit(build_nonce_state, ticker)
        f_vc = ex.submit(_vc_overlap, ticker)
        f_hist = ex.submit(_safe_history_and_flows, ticker)
        f_conc = ex.submit(_concentration, ticker)
        nonces = f_nonce.result()
        vc = f_vc.result()
        try:
            concentration = f_conc.result(timeout=30)
        except Exception as e:  # noqa: BLE001
            concentration = {"available": False, "reason": f"timeout/{e}"[:120]}
        try:
            safe_history, recent_flows = f_hist.result(timeout=60)
        except Exception as e:  # noqa: BLE001
            safe_history = recent_flows = {"available": False, "reason": f"timeout/{e}"[:120]}

    score = nonces.get("score", 0)
    signal = nonces.get("signal", "QUIET")

    # SPEC-72: a FIRED tracked safe feeding the operator's distribution aggregator is a §8
    # Stage-5 escalation the nonce signal alone misses once the sweep has consumed the fire
    # (newly_fired:[] → DORMANT). Gate the resolution on an actual fire (live diff OR a consumed
    # nonce_surveil HIGH alert) — quota-safe: a genuinely-dormant token resolves nothing.
    now = time.time()
    distribution = None
    try:
        fired = _fired_safe_addresses(ticker, nonces)
        if fired:
            # bounded under a timeout so build_onchain keeps its no-hang contract even when a
            # fired safe's flow read is slow (provider degraded → getLogs fallback).
            with ThreadPoolExecutor(max_workers=1) as dex:
                distribution = dex.submit(
                    lambda: _resolve_fired_distribution(ticker, fired, now=now)).result(timeout=25)
    except Exception:  # noqa: BLE001 — resolution never breaks (or hangs) the composed read
        distribution = None
    # SPEC-73: the 24h distribution-MEMORY surface (separate from the live signal) — reuses the
    # SPEC-72 resolutions already computed (no extra quota), gated on safes that actually fired.
    recent_distribution = _recent_distribution(distribution, now)
    # SPEC-72/73: only a LIVE fire (within the 6h window) flips the live signal off DORMANT;
    # a recent-but-not-live wave stays DORMANT here and is carried by recent_distribution instead.
    if (distribution and distribution.get("resolved") and distribution.get("live")
            and signal not in ("ESCALATION", "ESCALATION-UNCONFIRMED")):
        if distribution.get("aggregator") or distribution.get("distributing"):
            signal, score = "DISTRIBUTING", -30
        elif distribution.get("staging"):
            signal, score = "STAGING", -20
        if signal in ("DISTRIBUTING", "STAGING"):
            # req 2: surface the fired safe(s) in escalation_fired so the brief newly_fired is
            # non-empty and staging_vs_execution resolves off their dest_kind.
            existing = {(f.get("address") or "").lower() for f in (nonces.get("escalation_fired") or [])}
            resolved_fires = [{"label": f["safe_label"], "address": f["safe_address"],
                               "tier": f.get("safe_tier"), "nonce_prev": None, "nonce_now": None,
                               "dest_kind": f.get("dest_kind"), "verdict": f.get("verdict"),
                               "net_flow_window": f.get("net_token_out"), "source": "spec72-resolved"}
                              for f in distribution["flows"]
                              if (f.get("safe_address") or "").lower() not in existing]
            nonces["escalation_fired"] = (nonces.get("escalation_fired") or []) + resolved_fires
            nonces["signal"], nonces["score"] = signal, score

    # SPEC-75: gate the per-wallet distributor attribution on a CONFIRMED tracked-token OUT this
    # window. Rank confirmed fires by token-out magnitude (the headline = the wallet that actually
    # moved size, not the biggest nonce jump); demote nonce-only fires (n_out=0) to a separate
    # bucket; if NO fired safe confirms a token-out, downgrade off DISTRIBUTION-FIRING to a
    # nonce-only watch. The token-level aggregate stays honest — ≥1 confirmed out keeps the label.
    nonces["nonce_only_fired"] = []
    if distribution and distribution.get("resolved"):
        escalation_fired, nonce_only_fired, none_confirmed = _attribute_confirmed_fires(nonces, distribution)
        nonces["escalation_fired"] = escalation_fired
        nonces["nonce_only_fired"] = nonce_only_fired
        if none_confirmed and signal in ("ESCALATION", "ESCALATION-UNCONFIRMED", "DISTRIBUTING", "STAGING"):
            signal, score = "NONCE-ONLY-WATCH", 0
            nonces["signal"], nonces["score"] = signal, score

    # SPEC-89: the nonce signal reads STAGING, not actual token transfers — so verify_wallet the
    # top holders so a bare QUIET/DORMANT can't hide a top holder DISTRIBUTING (BLESS 0x73d8 /
    # H 0x28e2ea). A distributing top holder flips the headline off QUIET — a deep-neg squeeze
    # long-screen must never read "not distributing" from the nonce layer alone (§4/§8).
    try:
        thd = _top_holder_distribution(ticker, concentration)
    except Exception:  # noqa: BLE001 — never break/hang the composed read
        thd = {"checked": False, "distributing": False, "n_checked": 0, "holders": [],
               "reason": "top-holder check errored"}
    signal_caveat = None
    _QUIET_SIGNALS = {"QUIET", "DORMANT", "BASELINE_SETTLING", "NONCE-ONLY-WATCH", "NONCE-CHURN"}
    if thd.get("checked") and thd.get("distributing"):
        signal_caveat = "top-holder distribution present (verify_wallet)"
        if signal in _QUIET_SIGNALS:
            # QUIET must mean "verified quiet", not "nonce layer saw nothing" (SPEC-89).
            signal, score = "DISTRIBUTING", min(score, -20)

    # SPEC-98: rotation-aware distribution freshness (FRESH/FROZEN/ROTATED — the VELVET
    # false-pause read). Tracked leg = the freshest last_out_ts among the verified top
    # holders; the rotation legs (tape dump-legs / fresh-wallet→CEX) only run when that
    # clock is quiet. Gated on a LIVE SHORT thesis (that is when the banking decision this
    # verdict protects happens) + a distribution context, so a routine read spends nothing.
    freshness = _freshness_layer(ticker, thd)

    uns = _unsupported_chains(ticker)   # SPEC 58: the unseen-supply statement, GoPlus-independent
    quota = moralis_quota.status()       # SPEC 66: per-UTC-day Moralis gas gauge
    return {
        "ticker": ticker,
        # SPEC 66: at >80% of the daily Moralis budget the bias carries a [QUOTA n%] marker
        # so the wall is visible before flow reads start silently degrading.
        "bias": (f"{quota['prefix']} " if quota["prefix"] else "") + _bias(score, signal, nonces.get("escalation_fired"), recent_distribution),
        "moralis_calls_today": quota["calls"],   # the meter, surfaced for the orchestrator
        "moralis_quota": quota,
        "score": score,
        "signal": signal,
        "distribution": distribution,   # SPEC-72: resolved fired-safe → aggregator flow (None if no fire)
        "recent_distribution": recent_distribution,   # SPEC-73: 24h distribution-memory surface (None if none)
        "top_holder_distribution": thd,        # SPEC-89: verify_wallet sweep of the top holders
        "distribution_checked": thd.get("checked", False),   # SPEC-89: QUIET = "verified quiet" only when True
        "signal_caveat": signal_caveat,        # SPEC-89: set when a top holder is DISTRIBUTING
        "distribution_freshness": freshness,   # SPEC-98: FRESH/FROZEN/ROTATED (None when not in scope)
        "unsupported_chains": uns["unsupported_chains"],
        "unreadable_supply_pct": uns["unreadable_supply_pct"],
        "nonces": {k: nonces[k] for k in ("tracked", "signal", "score", "ms", "baseline_seeded",
                                          "fired_count", "primed_unfired_count", "dormant_count",
                                          "newly_fired", "escalation_fired", "untiered_fired",
                                          "nonce_churn", "escalation_unconfirmed",
                                          "nonce_only_fired") if k in nonces},   # SPEC-75
        "concentration": concentration,
        "safe_history": safe_history,
        "recent_flows": recent_flows,
        "vc_overlap": vc,
        "coverage": {
            "nonces": nonces.get("tracked", False),
            "safe_history": safe_history.get("available", False),
            "vc_overlap": vc.get("available", False),
            "concentration": concentration.get("available", False),
        },
    }


def coverage_alerts(chain_totals, wallets_total, wallets_unreadable, frac_threshold=0.25):
    """SPEC-145 req 4 — pure decision: is the sweep's own READ COVERAGE itself news? Any
    chain where EVERY tracked wallet was unreadable this sweep, or unreadable wallets exceed
    `frac_threshold` of the whole tracked set, is a HIGH event — a surveillance outage that
    reads as a clean QUIET/DORMANT is exactly the failure CLAUDE.md §3 names ("a zero/null
    print is a data FAILURE, not a datum"). chain_totals: {chain: {"total", "unreadable"}}."""
    events = []
    for chain in sorted(chain_totals):
        c = chain_totals[chain]
        if c["total"] > 0 and c["unreadable"] == c["total"]:
            events.append({"severity": "HIGH", "kind": "coverage_collapse", "chain": chain,
                           "msg": f"{chain}: all {c['total']} tracked wallet(s) unreadable — "
                                  f"surveillance BLIND on this chain"})
    if not events and wallets_total > 0 and wallets_unreadable / wallets_total > frac_threshold:
        frac = wallets_unreadable / wallets_total
        events.append({"severity": "HIGH", "kind": "coverage_collapse", "chain": None,
                       "msg": f"{wallets_unreadable}/{wallets_total} tracked wallets unreadable "
                              f"({frac:.0%}) — surveillance outage"})
    return events


def build_board(tickers=None):
    """Surveillance sweep — run the nonce signal across watchlist tokens that have a
    wallet map (cheap RPC, no Moralis). Persists each token's baseline, so consecutive
    sweeps catch a dormant-safe fire. alerts = ESCALATION (the actionable §8 signal)."""
    tracked = set(json.loads(WALLETS.read_text()).get("tokens", {}).keys())
    if tickers:
        names = [t.upper() for t in tickers if t.upper() in tracked]
    else:
        try:
            wl = [t["ticker"] for t in json.loads((ROOT / "config" / "watchlist.json").read_text())["tokens"]]
        except Exception:
            wl = sorted(tracked)
        names = [t for t in wl if t in tracked]

    with ThreadPoolExecutor(max_workers=3) as ex:
        # SPEC 55: the surveillance sweep is the loop that legitimately ADVANCES baselines
        # (persist=True) — it consumes each fire so the next sweep catches the next one.
        # Plain reads (onchain/brief) leave the baseline put, so same-minute reads agree.
        rows = list(ex.map(lambda n: build_nonce_state(n, persist=True), names))

    board = [{"ticker": r["ticker"], "signal": r.get("signal"), "score": r.get("score"),
              "fired_count": r.get("fired_count"), "primed_unfired_count": r.get("primed_unfired_count"),
              "escalation_fired": r.get("escalation_fired", []),
              "escalation_kind": r.get("escalation_kind"),                  # SPEC 28: execution | staging
              "nonce_churn": r.get("nonce_churn", []),                      # SPEC 24: fired-but-stale-token-out (noise)
              "escalation_unconfirmed": r.get("escalation_unconfirmed", []),
              "wallets_total": r.get("wallets_total", 0), "wallets_read": r.get("wallets_read", 0),
              "wallets_unreadable": r.get("wallets_unreadable", 0),         # SPEC-145
              "unreadable": r.get("unreadable", []),
              "baseline_seeded": r.get("baseline_seeded")}
             for r in rows]
    alerts = [b for b in board if b["signal"] == "ESCALATION"]

    # SPEC-145: aggregate read coverage across the WHOLE sweep, per chain — a per-token count
    # alone hides a chain-wide RPC outage (every wallet on one chain unreadable across every
    # tracked token at once). build_board propagates coverage to the top level (req 2).
    chain_totals = {}
    for r in rows:
        for w in r.get("wallets", []):
            c = chain_totals.setdefault(w.get("chain"), {"total": 0, "unreadable": 0})
            c["total"] += 1
            if not w.get("rpc_ok"):
                c["unreadable"] += 1
    wallets_total = sum(b["wallets_total"] for b in board)
    wallets_unreadable = sum(b["wallets_unreadable"] for b in board)
    unreadable_all = [dict(u, ticker=b["ticker"]) for b in board for u in b["unreadable"]]

    return {"scanned": len(board), "alerts": alerts,
            "loading": [b["ticker"] for b in board if b["signal"] == "LOADING"],
            "nonce_churn": [b["ticker"] for b in board if b["signal"] == "NONCE-CHURN"],   # SPEC 24
            "wallets_total": wallets_total, "wallets_read": wallets_total - wallets_unreadable,
            "wallets_unreadable": wallets_unreadable, "unreadable": unreadable_all,
            "coverage_alerts": coverage_alerts(chain_totals, wallets_total, wallets_unreadable),
            "board": sorted(board, key=lambda b: b.get("score") or 0)}


def render_human(o):
    sig = o["signal"]
    col = "red" if sig == "ESCALATION" else "yellow" if o["score"] < 0 else "green" if o["score"] > 0 else "grey"
    print(C.c(f"═══ {o['ticker']} ON-CHAIN ═══  {sig}", "bold", col))
    print(f"  {o['bias']}  (score {o['score']:+d})")
    n = o["nonces"]
    if not n.get("tracked"):
        print("  (untracked — no wallet map)")
    else:
        print(f"  nonces: {n['fired_count']} fired · {n['primed_unfired_count']} primed-unfired · "
              f"{n['dormant_count']} dormant  ({n['ms']}ms)" + ("  [baseline seeded]" if n.get("baseline_seeded") else ""))
        for f in n.get("escalation_fired", []):
            print(C.c(f"  🔴 ESCALATION: {f['label']} ({f['tier']}) nonce {f['nonce_prev']}→{f['nonce_now']} — dormant safe FIRED", "bold", "red"))
    sh = o["safe_history"]
    print(f"  safe_history: " + (f"{len(sh['wallets'])} BSC safes read" if sh.get("available") else C.c(f"unavailable — {sh.get('reason','')[:80]}", "grey")))
    vc = o["vc_overlap"]
    if vc.get("available") and vc.get("matches"):
        print(f"  vc_overlap: " + ", ".join(f"{m['wallet_label']}={m['entity']}" for m in vc["matches"][:5]))
    cc = o["concentration"]
    if cc.get("available"):
        print(f"  concentration: top1 {cc.get('top1_pct')}% · top10 {cc.get('top10_pct')}% · {cc.get('holder_count')} holders ({cc.get('chain')})")
    else:
        print(C.c("  concentration: unavailable — " + str(cc.get("reason", ""))[:60], "grey"))
    # SPEC 58: how much supply the engine CANNOT see (non-EVM chains) — belongs in the headline
    if o.get("unsupported_chains"):
        unseen = ", ".join(f"{u['chain']}"
                           + (f" {u['supply_pct']:g}%" if u.get("supply_pct") is not None else "")
                           for u in o["unsupported_chains"])
        ur = o.get("unreadable_supply_pct")
        urs = f"{ur:g}% of supply UNSEEN" if ur is not None else "supply share unknown"
        print(C.c(f"  ⚠ unsupported chains ({urs}): {unseen}", "bold", "yellow"))


def render_board(b):
    print(C.c(f"═══ ON-CHAIN SURVEILLANCE — {b['scanned']} tracked names ═══", "bold", "cyan"))
    if b["alerts"]:
        for a in b["alerts"]:
            fired = ", ".join(f"{f['label']}({f['nonce_prev']}→{f['nonce_now']})" for f in a["escalation_fired"])
            print(C.c(f"  🔴 ESCALATION {a['ticker']}: dormant safe fired [{fired}] — §8 bid-pull/top", "bold", "red"))
    else:
        print(C.c("  ⚪ no ESCALATION — no dormant safe fired since last sweep", "grey"))
    if b["loading"]:
        print(C.c(f"  🟠 loading (primed-unfired): {', '.join(b['loading'])}", "yellow"))


def main():
    ap = argparse.ArgumentParser(description="On-chain analyser (nonce-spine + composed picture)")
    ap.add_argument("ticker", nargs="?", default=None)
    ap.add_argument("--board", action="store_true", help="surveillance sweep across all tracked watchlist names")
    ap.add_argument("--depth", choices=["fast", "nonce"], default="fast",
                    help="fast=composed picture; nonce=just the live nonce signal")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--color", action="store_true")
    ap.add_argument("--no-color", action="store_true")
    args = ap.parse_args()
    if args.color:
        C.set_enabled(True)
    elif args.no_color:
        C.set_enabled(False)

    if args.board:
        data = build_board()
        print(json.dumps(data)) if args.json else render_board(data)
        return
    if not args.ticker:
        ap.error("ticker required (or use --board)")
    data = build_nonce_state(args.ticker) if args.depth == "nonce" else build_onchain(args.ticker)
    if args.json:
        print(json.dumps(data))
    elif args.depth == "nonce":
        print(json.dumps(data, indent=2))
    else:
        render_human(data)


if __name__ == "__main__":
    main()
