---
name: arkham-playwright-access
description: "Playwright MCP shares the user's logged-in browser session for arkm.com — use intel.arkm.com/explorer/address/{addr} + browser_evaluate to extract entity labels, holdings, and AI predictions directly."
metadata: 
  node_type: memory
  type: reference
  originSessionId: e94ca703-b256-430c-897b-c7124651d8f5
---

The Playwright MCP browser session shares the user's Arkham login. Confirmed 2026-05-20 — navigating to `https://intel.arkm.com/explorer/address/{ADDR}` shows authenticated content (real holdings, entity labels, AI predictions, top counterparties).

**Critical distinction Arkham makes:**
- **Formal entity labels** (e.g. "Kraken Deposit Address", "Wintermute", "Binance Hot Wallet") — human-verified, high confidence
- **AI MODEL predictions** (e.g. "DWF Labs?" with question mark, LOWER CONFIDENCE flag, "DISPUTE" button) — algorithmic guesses, NOT verified

When propagating intel from secondary sources (Telegram trackers, X analysts), check whether the entity label is a formal Arkham label OR an AI prediction before treating it as identified. The question-mark suffix + "LOWER CONFIDENCE" + "PREDICTION BY AN AI MODEL" annotations are explicit AI-flag markers.

**Useful pattern (browser_evaluate JS):**

```js
() => {
  const allText = document.body.innerText;
  return {
    title: document.title,
    firstChunks: allText.split('\n').slice(0, 40).join(' | ').slice(0, 1500),
    h1: document.querySelector('h1')?.textContent
  };
}
```

This pulls the entity label, holdings, AI predictions, and counterparty list visible above the fold. Faster than scraping snapshot YAML.

**Workflow:**
1. `mcp__playwright__browser_navigate` to the address URL
2. `mcp__playwright__browser_evaluate` with the JS above to extract structured info
3. Cross-check label confidence (formal vs AI prediction)
4. Save entity tags to `config/known_entities.json` if formal, or to wallet `_note` field if AI-predicted (with the "AI-predicted, unverified" annotation)

Related: [[feedback-mm-vs-team-distinction]], [[feedback-flows-blind-to-native-gas]]
