#!/usr/bin/env bash
# coder_dispatch.sh v2 — EVENT-DRIVEN coder trigger (GATED).
#
# Fired by launchd WatchPaths on handoffs/ AND handoffs/specs/open/ (see
# ops/com.crimedesk.coder-dispatch.plist). Detects OPEN specs **from main** — the coder
# worktree is cut from main, so a spec that exists only in a working tree is invisible
# to the coder (this silently no-op'd the 2026-06-10 01:55 dispatch). Uncommitted OPEN
# specs are WARNed about, never dispatched.
#
# Spec sources (both scanned, deduped):
#   new:    handoffs/specs/open/SPEC-N.md     — one file per spec; the directory IS the state
#   legacy: "## SPEC N (... OPEN)" headers in handoffs/*.md
#
# v2 changes vs v1:
#   - CLAIMS ($CLAIMS/SPEC-N, in ~/Library/Application Support/crimedesk): written at dispatch, on the desk side. A claimed spec is
#     never re-dispatched even though main still shows it OPEN until the human merges. Claims
#     self-clean once the spec stops being OPEN on main (merge landed / spec retired).
#     A claim left by a FAILED coder run is NOT auto-cleared — that is deliberate (no retry
#     storms): inspect the log, then rm the claim file to allow re-dispatch.
#   - BATCH: dispatches ALL unclaimed OPEN specs in one coder run (matches actual desk
#     practice: 33-35, 39-43, 44-50 were batches), one branch, one review cycle.
#   - STALE-LOCK RECOVERY: a lock whose pid is dead (coder killed mid-run) is removed,
#     not honored forever.
#   - no `set -e`: v1's set -e killed the script on a nonzero coder exit BEFORE the
#     "coder finished" log line — failures vanished without a trace. Errors are handled
#     explicitly; a failed run logs ERR + fires a notification.
#   - SPEC-70 (v2.1): the WatchPaths fire usually beats the orchestrator's commit by
#     seconds. Working-tree-only specs get a poll-retry window (RETRY_N × RETRY_SLEEP,
#     default 6×10s) before the WARN; the plist additionally watches .git/refs/heads/main
#     so the commit itself re-triggers. Env seams for the sandboxed tests
#     (tests/test_coder_dispatch_retry.py): CRIMEDESK_RUNDIR, CLAUDE_BIN, NO_NOTIFY,
#     RETRY_N, RETRY_SLEEP.
#   - SPEC-125 (v2.2): the coder invocation auto-retries ONCE on a transient API disconnect
#     (config/coder_dispatch.json transient_signatures, e.g. "Connection closed mid-response")
#     when the failed attempt's branch has ZERO commits (nothing to lose) — a fresh worktree,
#     after retry_backoff_s (default 120s). Commits on the failed branch, or a non-matching
#     failure, never retry — claims persist exactly as before. Env seams for the sandboxed
#     tests (tests/test_coder_dispatch_retry.py): CODER_RETRY_BACKOFF_S, CODER_MAX_RETRIES.
#
# After the coder finishes: run `ops/premerge.sh <branch>` (clean merge + green suite on the
# merge RESULT is the mechanical floor), review handoffs/REVIEW-REQUEST-*, then merge.
#
# DRYRUN=1 → detect + log only, do not launch the coder (smoke-test the wiring).
set -uo pipefail
DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "$DIR"
mkdir -p state worktrees

LOG="state/coder_dispatch.log"
# Lock + claims live OUTSIDE the repo: ~/Documents is iCloud-synced, and iCloud
# materialized duplicate claim files ("SPEC-45 2") mid-run on 2026-06-10 —
# concurrency-critical state must not sit in a synced folder.
# CRIMEDESK_RUNDIR override = test seam (SPEC-70 sandbox), defaults to production.
RUNDIR="${CRIMEDESK_RUNDIR:-$HOME/Library/Application Support/crimedesk}"
mkdir -p "$RUNDIR/claims"
LOCK="$RUNDIR/coder.lock"
CLAIMS="$RUNDIR/claims"
# SPEC-70: WatchPaths fires on the spec WRITE; the orchestrator's commit lands moments
# later. Poll-retry before WARN-exiting (6 × 10s default; env knobs are the test seam).
RETRY_N="${RETRY_N:-6}"
RETRY_SLEEP="${RETRY_SLEEP:-10}"
ts() { date -u +%FT%TZ; }
log() { echo "$(ts) $*" >> "$LOG"; }
notify() { [ "${NO_NOTIFY:-0}" = "1" ] && return 0
  osascript -e "display notification \"$1\" with title \"crime-desk coder\"" >/dev/null 2>&1 || true; }

# launchd's default PATH (/usr/bin:/bin:/usr/sbin:/sbin) has no claude — resolve it
# explicitly (rc=127 'command not found' killed the first launchd-fired batch, 2026-06-10).
# Env override = test seam.
CLAUDE_BIN="${CLAUDE_BIN:-$(command -v claude 2>/dev/null || true)}"
if [ -z "$CLAUDE_BIN" ]; then
  for p in "$HOME/.local/bin/claude" /opt/homebrew/bin/claude /usr/local/bin/claude; do
    if [ -x "$p" ]; then CLAUDE_BIN="$p"; break; fi
  done
fi
if [ -z "$CLAUDE_BIN" ]; then
  log "ERR: claude binary not found (PATH=$PATH, no known install location) — cannot dispatch"
  exit 1
fi

# SPEC-193: every coder request otherwise carries the ORCHESTRATOR's CLAUDE.md (40 KB,
# opens "You are the ORCHESTRATOR session — NOT the coder"), MEMORY.md, skills, hooks
# and every MCP server — ~15-19k tokens/request the coder never needs (its guardrails
# live in GOAL-coder.md, which it reads as a file regardless). `--bare` (docs:
# code.claude.com/docs/en/cli-reference — "skip auto-discovery of hooks, skills, custom
# commands, subagents, plugins, MCP servers, auto memory, and CLAUDE.md … Claude has
# access to Bash, file read, and file edit tools") drops that whole prefix.
# `--max-budget-usd` is a REAL backstop against an open-ended run (the SPEC-125/133/143
# failure modes) — confirmed in `claude --help` (v2.1.258): "Maximum dollar amount to
# spend on API calls (only works with --print)", which this invocation already is (-p).
# `--max-turns` does NOT exist in this CLI version — verified live: `claude --help`
# has no turn/iteration/limit flag at all, and passing the unrecognized `--max-turns N`
# is silently ignored (commander.js here doesn't error on unknown options; `claude
# --max-turns 5 --help` still printed ordinary help, exit 0) rather than failing loud.
# It is INCLUDED BELOW ANYWAY per this ticket's literal acceptance criterion (the
# dry-run argv must contain it) but is currently a NO-OP, not a real turn cap — see
# REVIEW-REQUEST-SPEC-193.md for the finding. `--max-budget-usd` is the one backstop
# that actually works today.
CODER_MODEL="${CODER_MODEL:-claude-sonnet-5}"
CODER_MAX_TURNS="${CODER_MAX_TURNS:-200}"   # NO-OP today — see comment above
CODER_MAX_BUDGET_USD="${CODER_MAX_BUDGET_USD:-25}"

_coder_prompt() {  # $1 = branch name for THIS attempt (the prompt names its own branch); reads $todo
  printf 'You are the CODER session for the crime desk, running headless (-p) in an isolated git worktree on branch %s. Read handoffs/GOAL-coder.md first and follow it exactly: it is the standing per-spec loop (test-first, commit before the full suite, the tailed foreground suite run, the REVIEW-REQUEST and the in-review move) and the hard guardrails. Your batch, in order: %s. For each spec ID the ticket is handoffs/specs/open/<ID>.md if that file exists, otherwise its '"'"'## SPEC <n>'"'"' section in handoffs/phase2-onchain-analyser.md. Commit to THIS branch only, spec id in the message; never switch to or merge into main. After the last spec: STOP — the orchestrator runs ops/premerge.sh on this branch and merges.' "$1" "$todo"
}

# --dry-run SPEC-N [SPEC-M ...]: print the assembled `claude` argv for the given spec(s)
# WITHOUT detecting OPEN specs on main, touching claims/locks, or creating a worktree —
# a pure "what would this run look like" smoke test (SPEC-193 acceptance).
if [ "${1:-}" = "--dry-run" ]; then
  shift
  todo="$*"
  if [ -z "$todo" ]; then
    echo "usage: coder_dispatch.sh --dry-run SPEC-N [SPEC-M ...]" >&2
    exit 1
  fi
  argv=("$CLAUDE_BIN" --disable-slash-commands --strict-mcp-config --model "$CODER_MODEL" --max-turns "$CODER_MAX_TURNS" \
       --max-budget-usd "$CODER_MAX_BUDGET_USD" -p "$(_coder_prompt "coder/auto-DRYRUN")")
  printf '%s\n' "${argv[@]}"
  exit 0
fi

# --- single-flight, with stale-lock recovery ---
check_lock() {
  if [ -f "$LOCK" ]; then
    lockpid="$(awk '{print $1}' "$LOCK" 2>/dev/null || true)"
    if [ -n "$lockpid" ] && [ "$lockpid" != "$$" ] && kill -0 "$lockpid" 2>/dev/null; then
      log "skip: coder already running (lock held by pid $lockpid)"
      exit 0
    fi
    if [ -n "$lockpid" ] && [ "$lockpid" != "$$" ]; then
      log "WARN: stale lock (pid ${lockpid:-?} dead — coder was killed mid-run) — removing lock and continuing"
      rm -f "$LOCK"
    fi
  fi
}
check_lock

# --- detect OPEN specs from MAIN (never the working tree) ---
detect_open_main() {
  open_new="$(git ls-tree -r --name-only main -- handoffs/specs/open/ 2>/dev/null | grep -oE 'SPEC-[0-9]+' | sort -uV || true)"
  # ':(exclude)handoffs/specs' — git pathspec 'handoffs/*.md' matches RECURSIVELY, so without the
  # exclude, done/ ticket files whose bodies keep their original '## SPEC N ((OPEN))' header
  # re-dispatch as zombies (batch-of-12 incident 2026-07-02; masked for weeks by stale claims).
  # For file-based tickets the specs/ directory IS the state — never read headers from it.
  open_legacy="$(git grep -hE '^##[[:space:]]+SPEC[[:space:]].*OPEN' main -- 'handoffs/*.md' ':(exclude)handoffs/specs' 2>/dev/null | grep -oE 'SPEC[[:space:]]+[0-9]+' | tr ' ' '-' | sort -uV || true)"
  open_all="$(printf '%s\n%s\n' "$open_new" "$open_legacy" | grep . | sort -uV || true)"
}
detect_open_main

# Working-tree-only specs: the orchestrator filed but hasn't committed YET. SPEC-70: this
# is usually the WatchPaths-vs-commit race (the fire beats the commit by seconds — 3
# occurrences in 24h, each needing a manual kick), so poll-retry for the commit to land
# before WARN-exiting; proceed the moment the spec appears on main.
wt_new="$(ls handoffs/specs/open 2>/dev/null | grep -oE 'SPEC-[0-9]+' | sort -uV || true)"
wt_legacy="$(grep -hE '^##[[:space:]]+SPEC[[:space:]].*OPEN' handoffs/*.md 2>/dev/null | grep -oE 'SPEC[[:space:]]+[0-9]+' | tr ' ' '-' | sort -uV || true)"
pending_wt() {
  pending=""
  for id in $wt_new $wt_legacy; do
    echo "$open_all" | grep -qx "$id" || pending="$pending $id"
  done
  pending="${pending# }"
}
pending_wt
if [ -n "$pending" ]; then
  log "retry: [$pending] OPEN in the working tree but not on main yet — waiting up to $((RETRY_N * RETRY_SLEEP))s for the commit to land (SPEC-70)"
  i=0
  while [ -n "$pending" ] && [ "$i" -lt "$RETRY_N" ]; do
    sleep "$RETRY_SLEEP"
    i=$((i + 1))
    detect_open_main
    pending_wt
  done
  if [ -z "$pending" ]; then
    log "retry: commit landed after ${i} poll(s) — proceeding"
  fi
  # Genuinely uncommitted after the window — the original WARN stands.
  for id in $pending; do
    log "WARN: $id is OPEN in the working tree but NOT on main — dispatch reads main; commit the spec to dispatch it (retry window of $((RETRY_N * RETRY_SLEEP))s exhausted)"
  done
  # While we slept, a commit-triggered fire (refs/heads/main is now in WatchPaths) may
  # have dispatched this batch already — re-check the lock before proceeding.
  check_lock
fi

# --- claim cleanup: a claim whose spec is no longer OPEN on main (merged/retired) is spent ---
for c in "$CLAIMS"/SPEC-*; do
  [ -e "$c" ] || continue
  cid="$(basename "$c")"
  if ! echo "$open_all" | grep -qx "$cid"; then
    log "claim cleanup: $cid no longer OPEN on main — clearing claim"
    rm -f "$c"
  fi
done

# --- batch = every OPEN spec without a live claim ---
todo=""
for id in $open_all; do
  [ -f "$CLAIMS/$id" ] || todo="$todo $id"
done
todo="${todo# }"
[ -z "$todo" ] && exit 0   # nothing unclaimed — silent (this fires on every handoffs write)

echo "$$ $(ts) $todo" > "$LOCK"
trap 'rm -f "$LOCK"' EXIT
n=$(echo "$todo" | wc -w | tr -d ' ')
log "DISPATCH: OPEN [$todo] → launching coder (gated, batch of $n)"

if [ "${DRYRUN:-0}" = "1" ]; then
  log "DRYRUN: would launch coder for [$todo] (skipped)"
  notify "DRYRUN: would dispatch coder for ${todo}"
  exit 0
fi

# --- isolate: git worktree + branch, so the running desk's tree is never disturbed ---
stamp="$(date -u +%Y%m%d-%H%M%S)"
branch="coder/auto-${stamp}"
wt="$DIR/worktrees/${branch//\//-}"
git worktree add -b "$branch" "$wt" main >> "$LOG" 2>&1 || { log "ERR: worktree add failed"; exit 1; }

# claim the batch (desk-side state — survives until the merge flips main)
for id in $todo; do
  printf 'branch=%s ts=%s\n' "$branch" "$(ts)" > "$CLAIMS/$id"
done

# The worktree only contains git-TRACKED files; symlink in the gitignored runtime deps
# (capabilities shell into _oldrepo; Moralis-backed caps need config/secrets.json).
# state/ is deliberately NOT shared — a coder test run can't pollute live surveillance state.
for dep in _oldrepo config/secrets.json; do
  if [ -e "$DIR/$dep" ] && [ ! -e "$wt/$dep" ]; then
    ln -s "$DIR/$dep" "$wt/$dep" 2>>"$LOG" && log "linked $dep into worktree" \
      || log "WARN: could not symlink $dep into worktree (live verification of dependent caps may fail)"
  fi
done

notify "coder dispatched for ${todo} on ${branch}"

# --- run the coder headless in the isolated worktree (GATED: branch-only, no merge) ---
# model/max-turns/max-budget pinned above (NO --bare: it disables OAuth — memory reference_claude_bare_disables_oauth_coder_needs_api_key; --disable-slash-commands + --strict-mcp-config give the auth-neutral half of the prefix savings) (before the --dry-run early-exit) —
# headless/unattended needs a stable endpoint, not the session default. Default = Sonnet
# (cheap; the premerge floor + literal-DoD specs are the quality gate, not the model).
# Escalate per-batch for architectural specs: CODER_MODEL=claude-fable-5 bash ops/coder_dispatch.sh

# --- SPEC-125: auto-retry ONCE on a transient API disconnect ---
# Two consecutive batches died mid-flight on the identical signature ("API Error: Connection
# closed mid-response", rc=1) with ZERO commits — pure loss, and because claims persist on
# failure (by design, loop protection) each stall then sat for DAYS waiting for a human to
# notice, inspect, and `rm` the claim file. This absorbs a single transient fault only:
# commits on the failed branch (partial work) or a non-matching failure never retry.
CODER_DISPATCH_CFG="$DIR/config/coder_dispatch.json"
_cfg_json() {
  CONFIG_FILE="$CODER_DISPATCH_CFG" python3 -c '
import json, os
DEFAULTS = {
    "transient_signatures": ["Connection closed mid-response", "overloaded_error",
                             "529", "terminated mid-response"],
    "retry_backoff_s": 120,
    "max_retries": 1,
}
try:
    d = json.load(open(os.environ["CONFIG_FILE"]))
except Exception:
    d = {}
out = dict(DEFAULTS)
out.update({k: v for k, v in d.items() if k in DEFAULTS})
print(json.dumps(out))
'
}
CFG_JSON="$(_cfg_json)"
_cfg_get() { printf '%s' "$CFG_JSON" | python3 -c "import json,sys; print(json.load(sys.stdin)['$1'])"; }
RETRY_BACKOFF_S="${CODER_RETRY_BACKOFF_S:-$(_cfg_get retry_backoff_s)}"
MAX_RETRIES="${CODER_MAX_RETRIES:-$(_cfg_get max_retries)}"
TOTAL_ATTEMPTS=$((MAX_RETRIES + 1))

# does the LAST run's own output tail match a known transient-API signature?
_is_transient_failure() {  # $1 = path to this run's own output (not the accumulated $LOG)
  CFG_JSON="$CFG_JSON" RUNLOG="$1" python3 -c '
import json, os
sigs = json.loads(os.environ["CFG_JSON"])["transient_signatures"]
try:
    tail = open(os.environ["RUNLOG"], errors="replace").read()[-4000:]
except Exception:
    raise SystemExit(1)
raise SystemExit(0 if any(s in tail for s in sigs) else 1)
'
}

attempt=1
cur_branch="$branch"
cur_wt="$wt"
rc=0
while :; do
  runlog="$DIR/state/coder_run_${cur_branch//\//-}.attempt${attempt}.log"
  (
    cd "$cur_wt"
    "$CLAUDE_BIN" --disable-slash-commands --strict-mcp-config --model "$CODER_MODEL" --max-turns "$CODER_MAX_TURNS" \
      --max-budget-usd "$CODER_MAX_BUDGET_USD" -p "$(_coder_prompt "$cur_branch")"
  ) >"$runlog" 2>&1
  rc=$?
  cat "$runlog" >> "$LOG"
  [ "$rc" -eq 0 ] && break

  n_commits="$(git -C "$cur_wt" rev-list --count "main..$cur_branch" 2>/dev/null || echo 1)"
  if [ "$attempt" -lt "$TOTAL_ATTEMPTS" ] && [ "${n_commits:-1}" -eq 0 ] && _is_transient_failure "$runlog"; then
    log "RETRY (transient API failure): [$todo] attempt $((attempt + 1))/$TOTAL_ATTEMPTS"
    # zero commits = nothing to lose — discard the failed worktree/branch, back off, cut fresh
    git worktree remove --force "$cur_wt" >>"$LOG" 2>&1 || true
    git branch -D "$cur_branch" >>"$LOG" 2>&1 || true
    sleep "$RETRY_BACKOFF_S"
    stamp="$(date -u +%Y%m%d-%H%M%S)"
    cur_branch="coder/auto-${stamp}"
    cur_wt="$DIR/worktrees/${cur_branch//\//-}"
    git worktree add -b "$cur_branch" "$cur_wt" main >> "$LOG" 2>&1 || { log "ERR: retry worktree add failed"; rc=1; break; }
    for dep in _oldrepo config/secrets.json; do
      if [ -e "$DIR/$dep" ] && [ ! -e "$cur_wt/$dep" ]; then
        ln -s "$DIR/$dep" "$cur_wt/$dep" 2>>"$LOG" && log "linked $dep into worktree" \
          || log "WARN: could not symlink $dep into worktree (live verification of dependent caps may fail)"
      fi
    done
    # re-point claims at the fresh branch — same spec IDs, batch identical (loop-protection
    # semantics unchanged: a claim always names the CURRENT attempt's branch)
    for id in $todo; do
      printf 'branch=%s ts=%s\n' "$cur_branch" "$(ts)" > "$CLAIMS/$id"
    done
    attempt=$((attempt + 1))
    continue
  fi
  break
done
branch="$cur_branch"
wt="$cur_wt"

# SPEC-143 hardening: a coder session that dies mid-turn can leave finished work sitting
# uncommitted in the worktree (coder/auto-20260807-014321 held 357 insertions of finished
# work — guard + 166-line test + review request — recovered only because an orchestrator
# happened to notice). Recovery must not depend on that. Whatever the coder's exit code,
# if the worktree is dirty when it exits, commit everything as one WIP commit right here —
# this does NOT make the run report "finished" (the SPEC-133 checks below still require a
# REVIEW-REQUEST per spec), it only guarantees the work survives to be inspected/finished
# by hand or the next dispatch.
if [ -n "$(git -C "$wt" status --porcelain 2>/dev/null)" ]; then
  git -C "$wt" add -A >>"$LOG" 2>&1
  if git -C "$wt" -c user.email="coder@crime-desk.local" -c user.name="crime-desk coder" \
      commit -qm "WIP: recovered — coder exited (rc=$rc) with uncommitted changes on $branch" \
      >>"$LOG" 2>&1; then
    log "SPEC-143: committed WIP: recovered on $branch (dirty worktree at coder exit, rc=$rc)"
  else
    log "ERR: SPEC-143 WIP auto-commit failed on $branch — worktree left dirty, inspect by hand"
  fi
fi

if [ "$rc" -eq 0 ]; then
  # SPEC-133: rc=0 is not delivery — four 2026-07-29 runs exited clean with an EMPTY
  # branch and still logged "finished" (an empty branch premerges PASS trivially, so the
  # false log was the only signal lost). Verify delivery before saying so.
  n_commits="$(git -C "$wt" rev-list --count "main..$branch" 2>/dev/null || echo 0)"
  missing_reviews=""
  for id in $todo; do
    git -C "$wt" cat-file -e "$branch:handoffs/REVIEW-REQUEST-${id}.md" 2>/dev/null \
      || missing_reviews="$missing_reviews $id"
  done
  missing_reviews="${missing_reviews# }"
  if [ "${n_commits:-0}" -eq 0 ]; then
    log "WARN: coder exited rc=0 for [$todo] on $branch but the branch has ZERO commits ahead of main (SPEC-133 false-finish guard) — NOT ready for premerge; claims persist, inspect $LOG then 'rm \"$CLAIMS/<SPEC-N>\"' to allow re-dispatch"
    notify "coder EMPTY-FINISH for ${todo} on ${branch} — 0 commits despite rc=0, see log"
  elif [ -n "$missing_reviews" ]; then
    log "WARN: coder exited rc=0 for [$todo] on $branch ($n_commits commit(s)) but missing REVIEW-REQUEST for [$missing_reviews] (SPEC-133 false-finish guard) — NOT ready for premerge; claims persist, inspect $LOG then 'rm \"$CLAIMS/<SPEC-N>\"' to allow re-dispatch"
    notify "coder INCOMPLETE-FINISH for ${todo} on ${branch} — missing REVIEW-REQUEST for ${missing_reviews}"
  else
    log "coder finished [$todo] on $branch (rc=0) — run 'ops/premerge.sh $branch', review handoffs/REVIEW-REQUEST-*, then merge"
    notify "coder DONE for ${todo} on ${branch} — premerge + review"
  fi
else
  log "ERR: coder FAILED [$todo] on $branch (rc=$rc) — inspect $LOG; claims persist (no further auto-retry): 'rm \"$CLAIMS/<SPEC-N>\"' to allow re-dispatch"
  notify "coder FAILED (rc=${rc}) for ${todo} — see state/coder_dispatch.log"
fi
exit 0
