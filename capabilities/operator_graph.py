#!/usr/bin/env python3
"""operator_graph.py — SPEC-99: operator-graph auto-clustering (G7).

Operator-cluster discovery has been manual: XTOKEN (BILL/BSB/EDEN/LAB/SKYAI/OPN), the
SKYAI 0xffa8 distributor pool, and the 0xaDFffc33 accumulator were each found by hand, so
§7 operator-heat ("crime coins sharing an MM are ONE position at multiplied size") only
applies when a human remembers the link. This builds the graph from already-tracked local
data (config/tracked_wallets.json) and emits named clusters to
config/operator_clusters.json.

Graph model:
  nodes = wallet addresses
  strong edge = a wallet recurs across >=2 tokens' tracked sets at a "strong" tier
                (op / team / distribution — the desk's own curated judgment, NOT the
                generic public exchange tag; e.g. the XTOKEN aggregator 0x1ab4973a is
                publicly "Bitget 6" but tier=op because the desk verified it's this
                operator's own MM channel — memory: feedback_operator_venue_liquidity_is_suspect)
  weak edge   = a wallet recurs across >=2 tokens ONLY via a cex-tier occurrence, or a
                strong-tier occurrence whose own label self-declares CEX-adjacency (e.g.
                "SKYAI-EOA-CEX-14Mnonce"). Tagged weak:shared_cex_funder — the XPIN lesson
                (memory: reference_xpin_cat_a_lock_linked_accumulation): a common CEX
                withdrawal/hot wallet co-occurring on unrelated tokens is NOT a Sybil tell
                on its own. Weak edges are evidence-only and never merge components.
  no edge     = dex / bridge / staking_lock / pool / burn tiers, "unclassified" tier
                (unverified GOPLUS-TOP* top-holder scan noise), and any wallet whose label
                self-declares shared router/pass-through infra (matches ROUTER or
                MULTI-CAT-A — the 0x73d8/0x238a co-occurrence trap, memory:
                reference_operator_cluster_0x73d8 DISPROVEN). These NEVER form an edge,
                strong or weak, regardless of tier: tracked_wallets.json already contains
                one un-reclassified example (0xb300000b, tiered "op" but its own note says
                "Mirror of 0x238a3588 (router-1)" and "Routes 28+ tokens") — trusting tier
                alone would silently re-create the exact disproven-cluster failure.

Cluster emission: connected components over strong edges only -> named clusters. A name is
seeded from config/clusters.json (the existing manually-curated §7 roster, e.g. XTOKEN-MM)
when a component majority-overlaps a known cluster, so the curated name survives instead of
being replaced by an arbitrary auto-generated id — existing known clusters seed/validate the
output, they don't compete with it. config/operator_clusters.json is written idempotently:
re-running preserves each cluster's id and first_seen_ts, only last_verified_ts advances.

Provider expansion (--expand) is a documented no-op in this build: SPEC-97's provider seam
is available for a future funder/first-tx expansion pass, but wiring it is optional per the
spec and out of scope here; --expand degrades to the same local-only result (no error).

CLI:
  python3 capabilities/operator_graph.py --json            # build, print, don't write
  python3 capabilities/operator_graph.py --write --json    # build AND persist to config/operator_clusters.json

Output envelope: {ok, data: {clusters, weak_edges}, meta: {written, expand, expand_note}}.
"""
import json
import re
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
CONFIG_DIR = ROOT / "config"

TRACKED_WALLETS_PATH = CONFIG_DIR / "tracked_wallets.json"
CLUSTERS_SEED_PATH = CONFIG_DIR / "clusters.json"
OUT_PATH = CONFIG_DIR / "operator_clusters.json"

# Tiers that form a strong (cluster-forming) edge when a wallet recurs across tokens.
STRONG_EDGE_TIERS = {"op", "team", "distribution"}
# A recurring cex-tier wallet is a weak edge at most (shared-CEX-funder != Sybil).
WEAK_EDGE_TIER = "cex"
# Tiers that never form an edge, strong or weak — dex routers/bridges/lockers/pools/burn.
NO_EDGE_TIERS = {"dex", "bridge", "staking_lock", "pool", "burn"}
# A label matching this is self-declared shared router/pass-through infra — never an edge,
# regardless of tier (the 0xb300000b landmine: tiered "op" but its own note says it mirrors
# the disproven 0x238a multi-Cat-A router).
ROUTER_LABEL_RE = re.compile(r"ROUTER|MULTI-CAT-A", re.IGNORECASE)


def _load_json(path, default):
    try:
        return json.loads(Path(path).read_text())
    except (OSError, json.JSONDecodeError):
        return default


def _is_router_label(label):
    return bool(ROUTER_LABEL_RE.search(label or ""))


def _is_cex_segment(label):
    """A label whose own hyphen-delimited segments include "CEX" (e.g.
    "SKYAI-EOA-CEX-14Mnonce") self-declares exchange-adjacency even at a strong tier."""
    return "CEX" in (label or "").upper().split("-")


def _addr_occurrences(tracked):
    occ = {}
    for ticker, info in (tracked.get("tokens") or {}).items():
        for w in info.get("wallets", []) or []:
            addr = (w.get("address") or "").lower()
            if not addr:
                continue
            occ.setdefault(addr, []).append({
                "ticker": ticker, "label": w.get("label"), "tier": w.get("tier"),
            })
    return occ


def build_edges(tracked=None):
    """Returns (strong_edges, weak_edges). strong_edges merge tokens into clusters;
    weak_edges are evidence-only and never merge components."""
    tracked = tracked if tracked is not None else _load_json(TRACKED_WALLETS_PATH, {"tokens": {}})
    occ = _addr_occurrences(tracked)
    strong, weak = [], []
    for addr, occs in sorted(occ.items()):
        tickers = sorted({o["ticker"] for o in occs})
        if len(tickers) < 2:
            continue
        if any(_is_router_label(o["label"]) for o in occs):
            continue  # shared router/pass-through infra — no edge at all
        strong_occs = [o for o in occs
                       if o["tier"] in STRONG_EDGE_TIERS and not _is_cex_segment(o["label"])]
        strong_tickers = sorted({o["ticker"] for o in strong_occs})
        if len(strong_tickers) >= 2:
            strong.append({
                "address": addr, "tokens": strong_tickers,
                "tier": strong_occs[0]["tier"],
                "evidence": [f"{o['ticker']}:{o['label']}({o['tier']})" for o in strong_occs],
            })
        elif any(o["tier"] == WEAK_EDGE_TIER or _is_cex_segment(o["label"]) for o in occs):
            weak.append({
                "address": addr, "tokens": tickers, "tag": "weak:shared_cex_funder",
                "evidence": [f"{o['ticker']}:{o['label']}({o['tier']})" for o in occs],
            })
        # else: NO_EDGE_TIERS / unclassified-only recurrence — no edge, not even weak.
    return strong, weak


class _UnionFind:
    def __init__(self):
        self.parent = {}

    def find(self, x):
        self.parent.setdefault(x, x)
        while self.parent[x] != x:
            self.parent[x] = self.parent[self.parent[x]]
            x = self.parent[x]
        return x

    def union(self, a, b):
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.parent[ra] = rb


def connected_components(strong_edges):
    """Union tokens connected by strong edges; components with < 2 members are dropped
    (a single-token 'cluster' isn't a cluster)."""
    uf = _UnionFind()
    all_tokens = set()
    for e in strong_edges:
        toks = e["tokens"]
        all_tokens.update(toks)
        for t in toks[1:]:
            uf.union(toks[0], t)
    groups = {}
    for t in all_tokens:
        groups.setdefault(uf.find(t), set()).add(t)
    components = []
    for members in groups.values():
        if len(members) < 2:
            continue
        edges = [e for e in strong_edges if set(e["tokens"]) & members]
        components.append({"tokens": sorted(members), "edges": edges})
    components.sort(key=lambda c: c["tokens"])
    return components


def _seed_name(tokens, seed):
    """Reuse an existing curated cluster name (config/clusters.json) when this component
    majority-overlaps it (>=2 shared members); otherwise generate a deterministic id from
    the sorted member list so re-runs never churn ids."""
    tokset = set(tokens)
    best_name, best_overlap = None, 0
    for name, c in (seed or {}).get("clusters", {}).items():
        members = {m.upper() for m in c.get("members", [])}
        overlap = len(members & tokset)
        if overlap > best_overlap:
            best_overlap, best_name = overlap, name
    if best_name and best_overlap >= 2:
        return best_name
    return "CLUSTER-" + "-".join(tokens)


def build_clusters(tracked=None, seed=None, now=None):
    """Returns (clusters_dict, weak_edges). clusters_dict maps cluster id ->
    {id, members, wallets, evidence, last_verified_ts}."""
    now = now if now is not None else time.time()
    strong_edges, weak_edges = build_edges(tracked)
    seed = seed if seed is not None else _load_json(CLUSTERS_SEED_PATH, {})
    components = connected_components(strong_edges)
    clusters = {}
    for comp in components:
        name = _seed_name(comp["tokens"], seed)
        wallets = {}
        for e in comp["edges"]:
            w = wallets.setdefault(e["address"], {"tier": e["tier"], "evidence": []})
            w["evidence"].extend(e["evidence"])
        clusters[name] = {
            "id": name,
            "members": comp["tokens"],
            "wallets": [{"address": a, **v} for a, v in sorted(wallets.items())],
            "evidence": comp["edges"],
            "last_verified_ts": now,
        }
    return clusters, weak_edges


def cluster_for_ticker(ticker, clusters):
    """(name, members) for the cluster containing ticker, or (None, [])."""
    ticker = (ticker or "").upper()
    for name, c in (clusters or {}).items():
        members = [m.upper() for m in c.get("members", [])]
        if ticker in members:
            return name, members
    return None, []


def write_clusters(path=None, tracked=None, seed=None, now=None):
    """Builds clusters and persists them idempotently: an existing cluster id's
    first_seen_ts survives re-runs, only last_verified_ts advances."""
    path = Path(path) if path else OUT_PATH
    now = now if now is not None else time.time()
    clusters, weak_edges = build_clusters(tracked, seed, now)
    existing = _load_json(path, {})
    existing_clusters = existing.get("clusters", {}) if isinstance(existing, dict) else {}
    for name, c in clusters.items():
        prev = existing_clusters.get(name)
        c["first_seen_ts"] = prev["first_seen_ts"] if prev and prev.get("first_seen_ts") is not None else now
    out = {
        "_doc": "SPEC-99 auto-built operator graph (capabilities/operator_graph.py) — do "
                "not hand-edit; re-run to refresh. See config/clusters.json for the "
                "manually-curated §7 roster this seeds names from.",
        "clusters": clusters,
        "weak_edges": weak_edges,
        "built_ts": now,
    }
    path.write_text(json.dumps(out, indent=2, sort_keys=True, default=str))
    return out


def main():
    import argparse
    ap = argparse.ArgumentParser(description="SPEC-99 operator-graph auto-clustering")
    ap.add_argument("--write", action="store_true", help="persist to config/operator_clusters.json")
    ap.add_argument("--expand", action="store_true",
                     help="provider-backed expansion (SPEC-97 seam) — no-op in this build")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    expand_note = ("provider expansion not wired in this build — local-only result returned"
                    if args.expand else None)

    if args.write:
        out = write_clusters()
        clusters, weak_edges = out["clusters"], out["weak_edges"]
        written = True
    else:
        clusters, weak_edges = build_clusters()
        written = False

    result = {"ok": True, "data": {"clusters": clusters, "weak_edges": weak_edges},
              "meta": {"written": written, "expand": bool(args.expand), "expand_note": expand_note}}
    if args.json:
        print(json.dumps(result, default=str))
    else:
        for name, c in clusters.items():
            print(f"{name}: {'+'.join(c['members'])}")
        if weak_edges:
            print(f"(weak edges: {len(weak_edges)}, evidence-only, no cluster)")


if __name__ == "__main__":
    main()
