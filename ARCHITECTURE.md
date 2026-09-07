# ARCHITECTURE — Crime-Pump Desk

How this project is built and operated. The trading logic lives in `CLAUDE.md` (the brain); **this file governs structure** — how work is split, how tools are called, how context stays clean. When the two conflict on *process*, this file wins.

Status: adopted 2026-06-02 after the desk grew "vibe-coded." Inventory at adoption: **56 scripts / 13,180 lines, 36 already JSON-clean, 17 noisy, 1 dead.**
**2026-06-10 update: the orchestrator layer is BUILT** — `orchestrator.py` + `capabilities.json` route ~25 registered capabilities; the migration phases in §7 are done. The coder pipeline (dispatch → isolated worktree → premerge → gated merge) is specified in §10.

---

## 1. The one rule everything serves: context is water in the desert

Response quality correlates with the *exclusivity* of what's in context. Every line of raw, unfiltered tool output that reaches the main agent is a tax on every subsequent decision. So:

- **The main agent never sees raw tool stdout.** It sees distilled JSON, always.
- **Decision-tree work is code, not agents.** If a task is "given these inputs, apply rules, return an answer," it is a deterministic script — never a subagent. (Subagents that ran decision trees were the #1 token waste.)
- **Subagents/workflows are reserved for genuine open-ended fan-out** — real research, adversarial verification, broad multi-file sweeps. Never for classification, filtering, or contract calls.

---

## 2. The layers

```
  DESIGNER  (main agent / this session)
     │   judgment, architecture, trading decisions. Clean context.
     │   calls capabilities by JSON contract — never reads raw stdout.
     ▼
  orchestrator.py  +  capabilities.json           ← built 2026-06 (the registry)
     │   routes a {capability, args} call → runs the script →
     │   applies a filter → returns ONLY distilled JSON.
     ▼
  capabilities/  (the ~50 existing scripts)        ← already mostly here
     │   each: deterministic · JSON I/O · TDD'd · documented.
     ▼
  raw APIs / RPC / chain                            ← noise stops here
```

**Coder session** is a *separate* context that builds/maintains the bottom two layers one script at a time, eating the noisy build work so the Designer's context stays exclusive.

---

## 3. The orchestrator contract

One entry point. The main agent calls:

```
python3 orchestrator.py <capability> '<json-args>'
# e.g.
python3 orchestrator.py classify '{"ticker":"LAB"}'
python3 orchestrator.py triage '{}'
```

- **stdout is ALWAYS a single JSON object** — `{ok, data, meta}` on success, `{ok:false, error}` on failure. Never prose, never a wall of text.
- The orchestrator reads `capabilities.json` (the live registry — 37 capabilities as of 2026-07), runs the underlying script, pipes its output through the capability's **filter**, validates against the capability's **out-contract**, and returns `data`.
- If a capability is already JSON-clean (36 of them), the filter is identity. If it's a noisy legacy script, the filter is a small Claude-written function that extracts the structured fields from stdout. **Filters are the bridge; the end-state folds each filter into the script as a native `--json`.**

---

## 4. Registry schema (`capabilities.json`)

Every capability is one entry (shown as YAML for readability; the live registry is `capabilities.json`, same fields):

```yaml
classify:
  script:   scripts/classify.py
  invoke:   "python3 scripts/classify.py {ticker} --json"   # {placeholders} filled from args; {payload}=full args dict as shell-quoted JSON (SPEC 65)
  filter:   null                  # null = output already clean JSON; else filters/<name>.py
  desc:     "CONFIRMS/TRIGGERS/BREAKS per token vs its committed thesis"
  reserved_for: "Designer's default first call each session (board view)"
  contract:
    in:  { ticker: "str?  (omit = whole board)" }
    out: { ticker: str, verdict: enum[CONFIRMS,TRIGGERS,BREAKS], reason: str, thesis_present: bool }
  test:     tests/test_classify.py
  doc:      docs/classify.md
```

Required keys: `script, invoke, desc, contract{in,out}, test`. `filter` and `doc` required for any *noisy* capability.

---

## 5. What makes a capability (Coder rules)

Every capability obeys **Test-Driven Development + Documentation-as-Code**:

1. **Test first.** Before touching the script, write `tests/test_<cap>.py` that asserts the *out-contract*: "given input X, the JSON has fields Y, types correct, values in sane ranges." This is the "what should I see if this works" target — it makes failure legible.
2. **JSON contract I/O.** Input = the args dict; output = the `out` contract, nothing else on stdout. Diagnostics go to stderr.
3. **Doc-as-code.** `docs/<cap>.md` (or a structured top-of-file docstring) states purpose, contract, one example call+response, and gotchas. Updated in the same change as the code — never after.
4. **Deterministic.** No model calls inside a capability. If it needs judgment, it's not a capability — it's a Designer decision or a (rare) subagent.

---

## 6. Designer / Coder protocol

- **Designer** (this session): owns `ARCHITECTURE.md` + `CLAUDE.md`. Decides *which* capabilities exist and *what their contracts are*. Writes capability **tickets** (a `capabilities.yaml` entry + the expected test). Never writes noisy build code. Calls capabilities, reasons over clean JSON.
- **Coder** (separate session): given a ticket, writes the test, implements/JSON-ifies the script to pass it, writes the doc, returns the working capability. One capability per handoff. Holds the noisy build context so the Designer never inhales it.
- **Handoff format:** Designer → `{capability spec + test expectation}`; Coder → `{path, test result, doc path, one example call}`.

---

## 7. Migration order (grounded in the 2026-06-02 inventory) — **DONE 2026-06**

> All four phases below completed; kept for the record. Current structural work is queued as specs (§10), not phases.

**Phase 0 — orchestrator skeleton.** Build `orchestrator.py` + `capabilities.yaml` wrapping the *already-clean* high-use four: `classify`, `oi_sides`, `analyse`, and `regime_flip`. Prove the loop on `classify` (already JSON). No script changes. *This is the smallest, highest-leverage build.*

**Phase 1 — JSON-ify the heavy noisy CLIs** (the context-bloaters), in usage order:
`triage` (413L) → `regime_check` (281L) → `price_structure` (201L) → `liq_magnets` (242L) → `pull5` (185L). Each: TDD, add `--json`, register as a capability, retire its filter.

**Phase 2 — consolidate `onchain-flow` (13 → ~5).** `flows / safe_audit / watch_wallets / nonce_watch / activity_audit` overlap heavily (all poll wallets). Define one `wallet_state` capability with modes; fold the rest in. Keep `holders`, `xverify`, `moralis` as distinct data-source capabilities.

**Phase 3 — cleanup.** Delete the orphan `verify_irys_wallets`. Audit `bscscan` (216L, not in CLAUDE.md) — fold into `moralis` or delete.

**Out of scope for the rebuild:** the trading edges (`CLAUDE.md` §1/§6) — that's a separate Designer decision, not a structural one.

---

## 8. Invariants (do not drift)

1. Main agent reads JSON, never raw stdout.
2. Decision trees are scripts; agents are for open-ended judgment only.
3. No capability without a test and a doc.
4. No plugins (esp. superpowers) — context bloat, no quality gain.
5. Designer designs contracts; Coder implements them; they don't merge.
6. Every capability speaks the same `{ok, data, meta}` envelope.

**Known open violation (recorded 2026-08-07, roadmap Phase 3a):** `oi_sides` is still
`status: "delegated"` to `_oldrepo/scripts/oi_sides.py` — the last non-native entry in the
registry, and it sources the wash HARD veto on 5 of 7 `setup_score` setups. A native port
is the first item of the roadmap's detection phase; until it lands, the desk's most
differentiated detector (对敲 / double-open wash) depends on parts-bin code outside
`capabilities/` with no test in the suite. Do not add new consumers of the delegated path.

---

## 9. Definition of done (the rebuild)

`orchestrator.py` routes every routine desk action; the main agent's session-open is `orchestrator.py classify '{}'` (the board) — *not* a per-token re-analysis or a workflow. Noisy-17 → 0. `onchain-flow` 13 → ~5. Every live capability has a passing test + a current doc. Context per turn drops because nothing raw reaches the Designer.

---

## 10. Coder pipeline (dispatch → premerge → gated merge)

How a Designer ticket becomes merged code. Adopted 2026-06-10 (v2) after the v1 plumbing produced silent no-op dispatches, re-fire loops, and an unlogged failure.

**Spec lifecycle — the directory is the state.** One file per spec: `handoffs/specs/open/SPEC-N.md` → coder `git mv`s it to `in-review/` on its branch → merge lands the move → moved to `done/` at ✅. No marker-grep, no 120KB monolith the headless coder must inhale (§1 applies to the coder's context too). Legacy specs (≤44) keep their `(OPEN)`/`(IN-REVIEW)`/✅ markers in `phase2-onchain-analyser.md`.

**Dispatch (`ops/coder_dispatch.sh`, launchd WatchPaths on `handoffs/` + `handoffs/specs/open/`):**
- Detects OPEN specs **from main, never the working tree** — coder worktrees are cut from main, so an uncommitted spec is invisible to the coder. Filing a spec isn't done until it's committed; uncommitted OPEN specs are WARNed in `state/coder_dispatch.log`.
- **Batches** all unclaimed OPEN specs into one coder run (one branch, one review cycle).
- **Claims** (claim files under `~/Library/Application Support/crimedesk/claims/`, desk-local — deliberately OUTSIDE the repo: `~/Documents` is iCloud-synced and sync duplicated claim files mid-run 2026-06-10): written at dispatch; block re-dispatch while main still shows the spec OPEN pending merge. Self-clean once the spec stops being OPEN on main. A failed run's claims persist deliberately (no retry storms) — `rm` the claim to re-dispatch. Claims also serve as manual **holds** (e.g. sequencing a spec behind another's merge).
- **Single-flight lock** with stale-pid recovery (a killed coder never wedges dispatch).
- The coder runs headless, model-pinned, in an isolated `coder/auto-*` worktree; commits to its branch only. **SPEC-193 (amended 2026-09-07):** the invocation is `claude --disable-slash-commands --strict-mcp-config --model … --max-budget-usd "$CODER_MAX_BUDGET_USD" -p …`. `--bare` was tried and removed: it is incompatible with subscription (OAuth) auth — the coder died `Not logged in` on its first live fire — so the CLAUDE.md/MEMORY.md prefix savings it bought are forgone; the two remaining flags drop skills and unlisted MCP servers, which is the auth-neutral half. The coder's own guardrails live in `GOAL-coder.md`, read as a file regardless. `--max-budget-usd` (default $25, override via `CODER_MAX_BUDGET_USD`) is a real backstop against an open-ended run (confirmed in `claude --help`). `--max-turns`/`CODER_MAX_TURNS` (default 200) is carried per this spec's literal acceptance criterion but is a **NO-OP** on the installed CLI (v2.1.258 has no turn-limiting flag at all — an unrecognized option is silently ignored, not an error); `--max-budget-usd` is the backstop that actually works today. `ops/coder_dispatch.sh --dry-run SPEC-N [SPEC-M …]` prints the assembled argv for the given spec id(s) without detecting OPEN specs, touching claims/locks, or creating a worktree — a pure smoke test of the invocation shape.
- **SPEC-125: auto-retries ONCE on a transient API disconnect** (`config/coder_dispatch.json` `transient_signatures`, e.g. "Connection closed mid-response") when the failed attempt's branch has **zero commits** (nothing to lose) — fresh worktree, after `retry_backoff_s` (default 120s). A branch with commits (partial work) or a non-matching failure never retries; claims persist exactly as before a final failure. Absorbs the single-transient-fault MTTR that used to be measured in days (a human noticing + `rm`ing the claim).
- **SPEC-133: false-finish guard.** rc=0 is not delivery — four 2026-07-29 runs exited clean with a branch that had **zero commits ahead of main** and dispatch still logged "coder finished ... run premerge". An empty branch premerges PASS trivially (the merge result IS main), so that false log was the only signal the orchestrator had, caught only by hand-diffing every branch. Before logging "finished", dispatch now checks, per completed run: (1) `git rev-list --count main..<branch>` ≥ 1, and (2) every spec in the batch has its `handoffs/REVIEW-REQUEST-<ID>.md` committed on the branch (the standing coder loop requires one per spec, done or blocked — its absence means the loop died mid-spec even if some unrelated commit landed). Either check failing logs a `WARN` instead of "finished", skips the "run premerge" instruction, and leaves the claim in place — same no-auto-retry, human-inspects contract as an ordinary coder failure.

**The gate:**
1. `ops/premerge.sh <branch>` — the mechanical floor: throwaway worktree, merge the branch, run the full suite **on the merge result** (individually-green branches can be jointly broken). Conflict or red suite → no merge. **SPEC-124:** the suite runs with a hard timeout (`PREMERGE_SUITE_TIMEOUT_S`, default 1200s) that kills the suite's whole process group (not just the direct child — an orphaned grandchild was the confirmed wedge hazard) and writes straight to a log file (`state/premerge-<branch>-<ts>.log`, no buffered command-substitution capture) — a verdict line (PASS/FAIL/MERGE CONFLICT/TIMEOUT/ERR) prints on every exit path, so a caller grepping for `PREMERGE:` never waits forever.
2. Orchestrator reads `handoffs/REVIEW-REQUEST-*` — judgment only (approach, contract fit), since mechanics are already proven.
3. **Merge policy (user directive 2026-06-22, supersedes the original P0/P1-human-merged tiers): the user does NOT review — the ORCHESTRATOR merges every branch that clears the premerge floor, no approval ask.** The floor is the protection: clean merge + suite green except failures confirmed identical on clean `main`; any *new* failure blocks the merge and earns a fix-ticket. (Mirrors CLAUDE.md §0 and `memory/feedback` — recorded here too so this file's process-precedence can't resurrect the old tiers.)

**Invariant recap for this pipeline:** dispatch is automatic; the merge is gated by the premerge floor and executed by the orchestrator (never blind through a red floor); the coder never touches main; specs touching the same capability are sequenced via holds, not parallel branches.
