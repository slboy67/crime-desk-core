---
name: feedback_shared_hub_may_be_dex_pool
description: "Shared-destination = operator coordination" is VOID if the shared destination is a public DEX pool/router — verify_wallet kind=unknown can mask exactly that.
metadata:
  type: feedback
---

A "shared sell-hub" only proves operator coordination if the hub is a *private* aggregation wallet. If it's a public PancakeSwap (or any DEX) pool/router, then "wallet A and wallet B both feed it" means nothing more than "both sold on that DEX" — which every seller does. It is NOT coordination evidence.

`verify_wallet` returns `kind: unknown` for unlabeled addresses, and a DEX pool/router shows up exactly that way: unknown kind + high bidirectional tx counts (hundreds–thousands). The high-tx bidirectional fingerprint that I read as "the hub routes into pools" is itself the pool signature. The tool cannot distinguish an operator aggregation wallet from a public contract on its own.

**Why:** On ESPORTS I built "0xbb58 + 0x2609 + 0x7a7ad9 are coordinated operator distributors" almost entirely on shared-destination 0x5bb5. User flagged 0x5bb5 (or a route addr) as a PancakeSwap wallet. If the *hub* is a DEX contract, the coordination inference collapses to "they all sold on Pancake." Only 0xbb58's seeded-from-tracked-safe survives independently.

**How to apply:** Before asserting shared-destination coordination, classify the destination FIRST. A wallet with hundreds+ of inbound AND outbound txns, bidirectional with many counterparties, is a DEX pool/router until proven otherwise — treat it as infrastructure, not an operator hub. Check BscScan contract tab / known router addresses / our entity DB. Coordination needs a *private* shared wallet (low fan-out, no contract code) + behavioral match (TWAP cadence, clip size), not just a common DEX venue. See [[feedback_audit_preloaded_entities_before_trust]] and [[feedback_mm_vs_team_distinction]].
