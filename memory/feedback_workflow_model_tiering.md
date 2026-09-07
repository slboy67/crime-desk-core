---
name: workflow-model-tiering
description: "When running multi-agent Workflows, tier the model per agent — use Sonnet/Haiku for simple fan-out work and reserve Opus for the genuinely complex step. Don't let every subagent inherit Opus by default."
metadata: 
  node_type: memory
  type: feedback
  originSessionId: 5e80cb56-db4e-4bb8-80db-b6b0f5b1d7f5
---

When authoring a `Workflow`, set the `model` option per `agent()` call deliberately — do NOT let every agent inherit the Opus main-loop model by default, because that burns tokens on work a cheaper model does just as well.

**Why:** 2026-05-29 the playbook-self-audit workflow ran all 49 agents on Opus 4.8 → ~3.0M output tokens. The user flagged it: "you were running every task in opus 4.8 which burns tokens — use Sonnet or other models where a smarter model isn't needed, run Opus 4.8 when the task is complex."

**How to apply:**
- **Finders / readers / extractors** (read files, return structured findings) → **Sonnet** (`model: 'sonnet'`). Capable enough; this is the bulk of agents in a fan-out.
- **Verifiers / classifiers** (confirm one claim against one file, yes/no refute) → **Sonnet**, or **Haiku** for truly mechanical checks.
- **Synthesis / lead-reasoning step** (reason over ALL findings, resolve conflicts, produce the plan) → **Opus**. Usually one agent.
- Net: in a typical find→verify→synthesize workflow, ~95% of agents go Sonnet, one goes Opus. Same quality outcome, fraction of the cost.
- Rule of thumb: if the agent's job is "fetch/locate/extract/confirm," cheap model; if it's "judge/synthesize/decide across many inputs," Opus. When genuinely unsure, the Workflow default guidance is to omit `model` — but the user's standing preference here is to actively down-tier the simple steps.

Related: [[feedback_engine_first_architecture]]
