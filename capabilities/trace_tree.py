#!/usr/bin/env python3
"""trace_tree.py — SPEC-109: recursive hop tracer with a CEX-termination verdict (§8).

The desk's most repeated manual workflow: tracing a distribution/fragmentation tree
hop-by-hop with verify_wallet.py, one address per call (XPIN 2026-07-03 took ~10 manual
rounds to establish "zero CEX termination"). The discriminator that decides these theses
(rotation-pre-markup vs obfuscated exit) is always the same question: does any branch of
the tree terminate at a CEX, and at what size? This makes that one call.

  python3 capabilities/trace_tree.py <root_addr> --token X --chain bsc \\
      [--depth 3] [--min-usd 1000] [--days 30] [--max-fetches 25] [--json]

BFS from root, reusing verify_wallet.build_verify per node (its sell_destinations gives
the outbound edges + destination classification for free — no new fetch/vendor plumbing).
Terminal per node: cex (known_entities/labels/kind=cex|cex-execution) — never recursed;
dex/router (SPEC-94-style infra) — never recursed; resting (no real outs in window);
open (has outs but depth-exhausted or the fetch budget ran out — never silently "clean").
"""
import argparse
import json
import sys
from collections import deque
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(Path(__file__).resolve().parent))
import colors as C
import verify_wallet as VW

DEFAULT_DEPTH = 3
DEFAULT_MIN_USD = 1000.0
DEFAULT_DAYS = 30
DEFAULT_MAX_FETCHES = 25   # Moralis free-tier quota is the constraint (SPEC 66/reference_moralis...)

BRIDGES_PATH = ROOT / "config" / "bridges.json"


def _load_bridges():
    """SPEC-116: {addr_lower: {name, trail_continues}} — the third identity-terminal. Desk-
    editable config/bridges.json; missing/unreadable degrades to the pre-SPEC-116 behavior
    (no bridge terminals, everything recurses like an unknown wallet)."""
    try:
        raw = json.loads(BRIDGES_PATH.read_text()).get("bridges", {})
    except Exception:
        return {}
    return {a.lower(): b for a, b in raw.items() if isinstance(a, str) and a.startswith("0x")}


def _identity_terminal(addr, tracked, labels, bridges):
    """(terminal_kind|None, label, trail_continues) — cex/dex/bridge are identity-terminal
    (never fetched/recursed); None means the node may be fetched and walked like any other
    wallet. Bridge check precedes cex/dex label-hint matching (SPEC-116 §1)."""
    b = bridges.get(addr)
    if b:
        return "bridge", b.get("name"), b.get("trail_continues")
    kind, label = VW._classify_addr(addr, tracked, labels)
    tier = tracked.get(addr, {}).get("tier")
    dest_kind = VW.classify_destination(kind, tier)
    if dest_kind == "cex-execution":
        return "cex", label, None
    if dest_kind in ("dex-router", "dex-execution"):
        return "dex", label, None
    return None, label, None


def _path_to(addr, parent_of):
    path = [addr]
    cur = addr
    while cur in parent_of:
        cur = parent_of[cur]
        path.append(cur)
    path.reverse()
    return path


def _edge_usd(edges, parent, child):
    for e in edges:
        if e["parent"] == parent and e["child"] == child:
            return e["amount_usd"] or 0.0
    return 0.0


def _primary_chain(tok):
    """Best single chain_key to probe bytecode on when the caller didn't pin one via
    --chain (multi-chain trace): tok's declared primary_chain, else its first contract."""
    pc = tok.get("primary_chain")
    if pc:
        return pc
    contracts = tok.get("contracts") or {}
    return next(iter(contracts), "binance-smart-chain")


def _terminal_totals(nodes, edges, kinds):
    """Sum amount_usd/amount_token of the direct parent->node edge for every node whose
    terminal is in `kinds` (SPEC-116 §2 verdict decomposition)."""
    usd = tok = 0.0
    for e in edges:
        child = e["child"]
        n = nodes.get(child)
        if n and n["terminal"] in kinds:
            usd += e["amount_usd"] or 0.0
            tok += e["amount_token"] or 0.0
    return {"usd": round(usd, 2), "token": round(tok, 4)}


def _continuation_check(eoa, token, tok_cfg, own_chain, trail_continues, price, days,
                        budget, sell_destinations=None):
    """SPEC-116 §3 — cheap cross-chain continuation for a traced EOA that bridged: check the
    SAME address on the token's other deployed chains for subsequent outbound/CEX-deposit
    activity. One extra hop, quota-aware (shares the caller's fetch budget). Returns None
    when there's nothing to check (no other chains); never fabricates a clean bill on a
    failed read (§3) — an unavailable read omits `cex_terminated` entirely."""
    if own_chain is None:
        return None   # the main walk already covered every queryable chain — nothing "other" left
    own_key = VW.CHAIN_ALIASES.get(own_chain.lower().strip(), own_chain.lower().strip())
    all_chains = VW.queryable_chains(tok_cfg)
    candidates = [c for c in all_chains if c != own_key]
    if isinstance(trail_continues, list) and trail_continues and trail_continues != ["unknown"]:
        aliased = {VW.CHAIN_ALIASES.get(str(c).lower(), str(c).lower()) for c in trail_continues}
        hinted = [c for c in candidates if c in aliased]
        if hinted:
            candidates = hinted
    if not candidates:
        return None
    chain_checked = candidates[0]
    if budget["fetched"] >= budget["max_fetches"]:
        return {"available": False, "chain_checked": chain_checked, "reason": "fetch budget exhausted"}
    v = VW.build_verify(eoa, token, days=days, price=price, chain=chain_checked)
    budget["fetched"] += 1
    if not v.get("available"):
        return {"available": False, "chain_checked": chain_checked, "reason": v.get("reason")}
    for e in (v.get("sell_destinations") or []):
        if e.get("dest_kind") == "cex-execution":
            amt_usd = round(e["amount"] * price, 2) if price is not None else None
            return {"available": True, "chain_checked": chain_checked, "cex_terminated": True,
                    "amount_usd": amt_usd, "amount_token": round(e["amount"], 4),
                    "label": e.get("label"),
                    "verdict": f"cross-chain CEX termination on {chain_checked}"
                              + (f": ${amt_usd:,.0f}" if amt_usd is not None else "")}
    return {"available": True, "chain_checked": chain_checked, "cex_terminated": False,
            "verdict": f"no further CEX-bound activity on {chain_checked}"}


def trace_tree(root_addr, token, chain=None, depth=DEFAULT_DEPTH, min_usd=DEFAULT_MIN_USD,
               days=DEFAULT_DAYS, max_fetches=DEFAULT_MAX_FETCHES):
    """BFS hop-trace from root_addr. Pure w.r.t. its inputs except for the on-chain reads
    (all routed through verify_wallet, monkeypatchable exactly like test_verify_wallet.py)."""
    root = (root_addr or "").strip().lower()
    token = token.upper().replace("USDT", "")
    tok = json.loads(VW.WALLETS.read_text()).get("tokens", {}).get(token)
    if not tok:
        return {"available": False, "root": root, "token": token,
                "reason": f"{token} not in tracked config (need its contract to query transfers)"}

    tracked = {w["address"].lower(): {"label": w.get("label"), "tier": (w.get("tier") or "").lower()}
               for w in tok.get("wallets", [])}
    labels = VW._entity_labels()
    bridges = _load_bridges()
    price, price_source = VW._live_price(token)

    nodes = {}          # addr -> {terminal, label, depth, ...}
    edges = []          # [{parent, child, amount_token, amount_usd, label}]
    parent_of = {}
    visited = set()
    skipped = []
    fetched = 0
    truncated = False
    queue = deque([(root, 0)])

    while queue:
        addr, d = queue.popleft()
        if addr in visited:
            continue
        visited.add(addr)

        term_kind, label, trail_continues = _identity_terminal(addr, tracked, labels, bridges)
        if term_kind:
            node = {"terminal": term_kind, "label": label, "depth": d}
            if term_kind == "bridge":
                node["trail_continues"] = trail_continues
            nodes[addr] = node
            continue

        if d >= depth:
            nodes[addr] = {"terminal": "open", "label": label, "depth": d}
            continue

        if fetched >= max_fetches:
            nodes[addr] = {"terminal": "open", "label": label, "depth": d}
            skipped.append(addr)
            truncated = True
            continue

        # SPEC-116 §4: an unlabeled contract (pool/router/bridge-adapter the seed lists
        # missed) must never be traversed as if it were a hop wallet — probe before fetch.
        if addr not in tracked:
            try:
                probe = VW.probe_contract(addr, chain or _primary_chain(tok))
            except Exception:  # noqa: BLE001 — a probe failure never blocks the trace
                probe = {"available": False}
            if probe.get("available") and probe.get("is_contract"):
                nodes[addr] = {"terminal": "unresolved_contract", "label": label, "depth": d}
                continue

        v = VW.build_verify(addr, token, days=days, price=price, chain=chain)
        fetched += 1
        if not v.get("available"):
            nodes[addr] = {"terminal": "resting", "label": label, "depth": d,
                          "degraded": True, "reason": v.get("reason")}
            continue

        real_edges = []
        for e in (v.get("sell_destinations") or []):
            amt_usd = round(e["amount"] * price, 2) if price is not None else None
            gate = amt_usd if amt_usd is not None else e["amount"]
            if gate < min_usd:
                continue
            real_edges.append((e, amt_usd))

        if not real_edges:
            nodes[addr] = {"terminal": "resting", "label": label, "depth": d}
            continue

        nodes[addr] = {"terminal": None, "label": label, "depth": d}
        for e, amt_usd in real_edges:
            child = (e["address"] or "").lower()
            if not child or child == addr:
                continue
            edges.append({"parent": addr, "child": child, "amount_token": round(e["amount"], 4),
                          "amount_usd": amt_usd, "label": e.get("label")})
            parent_of.setdefault(child, addr)
            queue.append((child, d + 1))

    cex_nodes = [a for a, n in nodes.items() if n["terminal"] == "cex"]
    bridge_nodes = [a for a, n in nodes.items() if n["terminal"] == "bridge"]
    dex_nodes = [a for a, n in nodes.items() if n["terminal"] == "dex"]
    resting_nodes = [a for a, n in nodes.items() if n["terminal"] == "resting"]
    open_nodes = [a for a, n in nodes.items() if n["terminal"] == "open"]
    unresolved_contract_nodes = [a for a, n in nodes.items() if n["terminal"] == "unresolved_contract"]
    deepest = max((n["depth"] for n in nodes.values()), default=0)

    # SPEC-116 §3: continuation check for every bridge hop — the parent that sent to the
    # bridge is by construction a traced EOA (contract-adapters are gated above and never
    # reach here as a fetch-and-recurse node).
    budget = {"fetched": fetched, "max_fetches": max_fetches}
    for b in bridge_nodes:
        eoa = parent_of.get(b)
        if not eoa:
            continue
        cont = _continuation_check(eoa, token, tok, chain, nodes[b].get("trail_continues"),
                                   price, days, budget)
        if cont is not None:
            nodes[b]["continuation"] = cont
    fetched = budget["fetched"]

    branches = []
    for c in cex_nodes:
        path = _path_to(c, parent_of)
        amt = _edge_usd(edges, parent_of.get(c), c)
        branches.append({"path": path, "amount_usd": amt, "label": nodes[c].get("label")})

    bridge_branches = []
    for b in bridge_nodes:
        path = _path_to(b, parent_of)
        amt = _edge_usd(edges, parent_of.get(b), b)
        bridge_branches.append({"path": path, "amount_usd": amt, "label": nodes[b].get("label"),
                                "trail_continues": nodes[b].get("trail_continues"),
                                "continuation": nodes[b].get("continuation")})

    terminated = {
        "cex": _terminal_totals(nodes, edges, {"cex"}),
        "bridge": _terminal_totals(nodes, edges, {"bridge"}),
        "dex": _terminal_totals(nodes, edges, {"dex"}),
        "unresolved": _terminal_totals(nodes, edges, {"resting", "open", "unresolved_contract"}),
    }

    if cex_nodes:
        total_usd = sum(b["amount_usd"] for b in branches)
        best = max(branches, key=lambda b: b["amount_usd"])

        def _tag(i, p):
            return p[:8] + "…" if i == 0 else (nodes.get(p, {}).get("label") or p[:8] + "…")

        path_str = "→".join(_tag(i, p) for i, p in enumerate(best["path"]))
        verdict = (f"CEX_TERMINATED (branch {path_str}, ${total_usd:,.0f} total "
                  f"across {len(branches)} branch{'es' if len(branches) != 1 else ''})")
    else:
        total_in_tree = sum((e["amount_usd"] or 0.0) for e in edges)
        verdict = (f"NO_CEX_TERMINATION ({len(resting_nodes)} leaf wallets resting, "
                  f"deepest depth {deepest}, ${total_in_tree:,.0f} still in tree)")

    if bridge_nodes:
        bridge_usd = terminated["bridge"]["usd"]
        verdict += (f"  BRIDGE_TERMINATED: ${bridge_usd:,.0f} across {len(bridge_nodes)} "
                   f"bridge{'s' if len(bridge_nodes) != 1 else ''} — trail continues cross-chain")

    if open_nodes:
        verdict += f"  OPEN_BRANCHES: {len(open_nodes)}"

    return {
        "available": True, "root": root, "token": token, "chain": chain,
        "depth": depth, "min_usd": min_usd, "days": days,
        "nodes": nodes, "edges": edges,
        "fetched": fetched, "max_fetches": max_fetches,
        "truncated": truncated, "skipped": sorted(set(skipped)),
        "cex_terminated": bool(cex_nodes), "bridge_terminated": bool(bridge_nodes),
        "dex_terminated": bool(dex_nodes), "branches": branches, "bridge_branches": bridge_branches,
        "terminated": terminated, "unresolved_contract_nodes": sorted(unresolved_contract_nodes),
        "verdict": verdict, "price_usd": price, "price_source": price_source,
    }


def render_human(r):
    if not r.get("available"):
        print(f"# {r.get('token')} {r.get('root', '')[:10]}… — unavailable: {r.get('reason')}")
        return
    print(C.c(f"═══ trace {r['root']} ({r['token']}) ═══", "bold", "cyan"))
    print(f"  depth={r['depth']} min_usd=${r['min_usd']:,.0f} days={r['days']} "
         f"fetched {r['fetched']}/{r['max_fetches']}" + (C.c("  [TRUNCATED]", "bold", "yellow") if r["truncated"] else ""))
    col = "red" if r["cex_terminated"] else "yellow"
    print(C.c(f"  {r['verdict']}", "bold", col))
    if r["skipped"]:
        print(C.c(f"  skipped (budget): {', '.join(r['skipped'])}", "grey"))


def main():
    ap = argparse.ArgumentParser(description="Recursive hop tracer with CEX-termination verdict (§8)")
    ap.add_argument("root_addr")
    ap.add_argument("--token", required=True)
    ap.add_argument("--chain", default=None, help="restrict to one chain (eth/bsc/base); default = all deployed")
    ap.add_argument("--depth", type=int, default=DEFAULT_DEPTH)
    ap.add_argument("--min-usd", type=float, default=DEFAULT_MIN_USD, dest="min_usd")
    ap.add_argument("--days", type=int, default=DEFAULT_DAYS)
    ap.add_argument("--max-fetches", type=int, default=DEFAULT_MAX_FETCHES, dest="max_fetches")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--color", action="store_true")
    ap.add_argument("--no-color", action="store_true")
    args = ap.parse_args()
    if args.color:
        C.set_enabled(True)
    elif args.no_color:
        C.set_enabled(False)

    r = trace_tree(args.root_addr, args.token, chain=args.chain, depth=args.depth,
                   min_usd=args.min_usd, days=args.days, max_fetches=args.max_fetches)
    if args.json:
        print(json.dumps(r))
    else:
        render_human(r)


if __name__ == "__main__":
    main()
