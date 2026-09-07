#!/usr/bin/env python3
"""SPEC 68 — shared structural invariants for the wallet-onboarding tests.

Onboarding tests must validate the *shape* of a tracked wallet (present, valid address,
known chain, tier in the allowed enum, unique label/address, required fields) — NEVER the
specific tier of a named production wallet. Wallet tiers are routine desk judgment and get
re-tiered often (4 in two days, e.g. ESPORTS CEX-DEPOSIT-SECONDARY op<->distribution); a
test that hardcodes `tiers.get(ADDR) == "distribution"` turns every re-tier into a suite
failure. Use these helpers for the structural checks; where a test needs tier *semantics*
(e.g. "a distribution-tier funder is seeded-staging"), build a fixture wallet inline via
`temp_token_config` instead of leaning on a live production wallet's tier.
"""
import ast
import json
import os
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# The tier enum actually used across config/tracked_wallets.json. Structural invariant only:
# tests assert a wallet's tier is IN this set, never that it equals a particular value.
ALLOWED_TIERS = {
    "team", "distribution", "op", "passthrough", "staking_lock",
    "bridge", "pool", "cex", "burn", "unclassified",
}

# EVM chains the nonce/concentration framework tracks (Solana is documented separately, never
# a nonce-watch chain — see test_chip_onboarding).
KNOWN_CHAINS = {"ethereum", "binance-smart-chain", "base", "arbitrum", "solana"}

REQUIRED_WALLET_FIELDS = ("label", "address", "chain", "tier")


def load_tokens():
    """Live tracked-wallet config (tokens map)."""
    return json.loads((ROOT / "config" / "tracked_wallets.json").read_text())["tokens"]


def find_wallet(token_cfg, addr):
    for w in token_cfg.get("wallets", []):
        if w["address"].lower() == addr.lower():
            return w
    return None


def is_evm_address(addr):
    if not (isinstance(addr, str) and addr.startswith("0x") and len(addr) == 42):
        return False
    try:
        int(addr, 16)
    except ValueError:
        return False
    return True


def assert_wallet_structural(tc, w, *, expected_chain=None):
    """Re-tier-proof invariants for a single tracked wallet.

    Checks presence of required fields, address validity, chain membership, and that the tier
    is in the allowed enum — but NEVER that the tier equals a specific value.
    """
    tc.assertIsNotNone(w, "wallet not onboarded")
    for f in REQUIRED_WALLET_FIELDS:
        tc.assertIn(f, w, f"wallet missing required field {f!r}: {w}")
        tc.assertTrue(str(w[f]).strip() != "", f"wallet field {f!r} is empty: {w}")
    chain = (w.get("chain") or "").lower()
    tc.assertIn(chain, KNOWN_CHAINS, f"unknown chain {chain!r}: {w}")
    if chain != "solana":
        tc.assertTrue(is_evm_address(w["address"]), f"invalid EVM address: {w['address']}")
    tc.assertIn((w.get("tier") or "").lower(), ALLOWED_TIERS, f"tier not in enum: {w}")
    if expected_chain is not None:
        tc.assertEqual(chain, expected_chain.lower(), f"unexpected chain for {w['address']}")


def assert_unique_within_token(tc, token_cfg, token_name=""):
    wallets = token_cfg.get("wallets", [])
    addrs = [w["address"].lower() for w in wallets]
    tc.assertEqual(len(addrs), len(set(addrs)), f"{token_name} has duplicate wallet addresses")
    labels = [w.get("label") for w in wallets if w.get("label")]
    tc.assertEqual(len(labels), len(set(labels)), f"{token_name} has duplicate wallet labels")


def temp_token_config(token, wallets, *, contracts=None, decimals=18):
    """Write a throwaway tracked_wallets.json holding ONE synthetic token with the given
    fixture wallets, and return its path. Point `verify_wallet.WALLETS` at it to exercise
    tier *semantics* without depending on any live production wallet's tier.
    """
    contracts = contracts or {"binance-smart-chain": "0x" + "c" * 40}
    cfg = {"tokens": {token: {"contracts": contracts, "decimals": decimals, "wallets": wallets}}}
    fd, path = tempfile.mkstemp(prefix="trackedwallets_", suffix=".json")
    with os.fdopen(fd, "w") as fh:
        json.dump(cfg, fh)
    return path


# --- the regression guard: detect "assert a named production wallet's live tier == literal" ---

_ASSERT_EQ_FNS = {"assertEqual", "assertEquals", "assertNotEqual"}


def live_tier_assertions(path):
    """Return [(lineno, source)] for every equality assertion that pins a wallet's tier to a
    specific tier literal — the anti-pattern this spec removes. An assertion is flagged when an
    assertEqual/assertNotEqual has one argument that is a string constant in ALLOWED_TIERS and
    another argument whose source text references a tier (so `assertEqual(tiers.get(x),
    "distribution")` and `assertEqual(w["tier"], "team")` are caught, while engine-output
    assertions like `assertTrue(v["seeded_staging"])` are not).
    """
    src = Path(path).read_text()
    tree = ast.parse(src)
    offenders = []
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and node.func.attr in _ASSERT_EQ_FNS):
            continue
        args = node.args
        tier_literal = any(isinstance(a, ast.Constant) and isinstance(a.value, str)
                           and a.value.lower() in ALLOWED_TIERS for a in args)
        if not tier_literal:
            continue
        refs_tier = False
        for a in args:
            if isinstance(a, ast.Constant):
                continue
            seg = ast.get_source_segment(src, a) or ""
            if "tier" in seg.lower():
                refs_tier = True
        if refs_tier:
            offenders.append((node.lineno, ast.get_source_segment(src, node)))
    return offenders
