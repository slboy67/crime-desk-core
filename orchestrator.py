#!/usr/bin/env python3
"""Orchestrator — the desk switchboard. See ARCHITECTURE.md.

  python3 orchestrator.py <capability> '<json-args>'

ALWAYS prints exactly one JSON object to stdout:
  {"ok":true, "data":<...>, "meta":{"capability","args","ms"}}
  {"ok":false,"error":"..."}

The main agent reads this JSON and NEVER the underlying script's raw stdout.
Registry is capabilities.json (dependency-free; YAML avoided — no pyyaml on host).
"""
import sys, json, time, re, shlex, subprocess, importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parent
REGISTRY = ROOT / "capabilities.json"


def load_caps():
    return json.loads(REGISTRY.read_text())


def fill(invoke, args):
    """Build the command from an invoke template.

    - `[ ... {key} ... ]` is an OPTIONAL group: kept (brackets stripped, {key}
      filled) only when every {key} inside it is present & non-null in args;
      dropped entirely otherwise. Use for flag+value pairs, e.g. `[--days {days}]`.
    - bare `{key}` is filled with str(args[key]); a missing/null bare key → "".
    - dict/list values are passed as shell-quoted JSON (SPEC 48: a nested thesis
      block must survive the shell=True invoke path — str(dict) is Python repr,
      not JSON, and unquoted spaces would split the argv).
    - `{payload}` (SPEC 65) is the FULL args dict serialized to shell-quoted JSON
      — for caps whose script takes one positional JSON arg (setup_score/workup/
      replay). Always present (empty args → `{}`), so it never drops to "".
    """
    def sval(v):
        return shlex.quote(json.dumps(v)) if isinstance(v, (dict, list)) else str(v)

    def resolve(key):
        if key == "payload":
            return shlex.quote(json.dumps(args))
        v = args.get(key)
        return None if v is None else sval(v)

    def group(m):
        inner = m.group(1)
        keys = re.findall(r"\{(\w+)\}", inner)
        if all(resolve(k) is not None for k in keys):
            return re.sub(r"\{(\w+)\}", lambda mm: resolve(mm.group(1)), inner)
        return ""
    invoke = re.sub(r"\[([^\[\]]*)\]", group, invoke)

    def repl(m):
        v = resolve(m.group(1))
        return "" if v is None else v
    # collapse any double spaces left by dropped optional tokens
    return re.sub(r"\s{2,}", " ", re.sub(r"\{(\w+)\}", repl, invoke)).strip()


def apply_aliases(spec, args):
    """SPEC-89: map desk-natural arg keys to the capability's canonical ones before fill().

    A capability may declare `"aliases": {"ticker": "token", "wallet": "address"}` — for each
    alias→canonical pair, if the alias key is present and the canonical isn't, copy it across.
    This lets `verify_wallet '{"ticker":"BLESS","wallet":"0x.."}'` (the names the desk uses
    everywhere) fill `<address> --token <TKR>` without breaking the documented address/token
    form (the SPEC-89 `--token: expected one argument` wiring bug)."""
    out = dict(args)
    for alias, canonical in (spec.get("aliases") or {}).items():
        if alias in out and canonical not in out:
            out[canonical] = out[alias]
    return out


def run_filter(name, raw, args):
    fp = ROOT / "filters" / (name + ".py")
    spec = importlib.util.spec_from_file_location(name, fp)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.run(raw, args)


def emit(obj):
    print(json.dumps(obj))


def main():
    if len(sys.argv) < 2:
        emit({"ok": False, "error": "usage: orchestrator.py <capability> '<json-args>'"})
        return 1
    cap = sys.argv[1]
    raw_args = sys.argv[2] if len(sys.argv) > 2 and sys.argv[2].strip() else "{}"
    try:
        args = json.loads(raw_args)
        if not isinstance(args, dict):
            raise ValueError("args must be a JSON object")
    except Exception as e:
        emit({"ok": False, "error": f"bad args: {e}"})
        return 1

    try:
        caps = {k: v for k, v in load_caps().items() if not k.startswith("_")}
    except Exception as e:
        emit({"ok": False, "error": f"cannot read capabilities.json: {e}"})
        return 1
    if cap not in caps:
        emit({"ok": False, "error": f"unknown capability: {cap}", "known": sorted(caps)})
        return 1

    spec = caps[cap]
    args = apply_aliases(spec, args)   # SPEC-89: desk-natural keys → canonical before fill
    cmd = fill(spec["invoke"], args)
    t0 = time.time()
    try:
        proc = subprocess.run(cmd, shell=True, cwd=str(ROOT),
                              capture_output=True, text=True,
                              timeout=spec.get("timeout", 180))
    except subprocess.TimeoutExpired:
        emit({"ok": False, "error": f"timeout running {cap}"})
        return 1

    raw = proc.stdout
    try:
        data = run_filter(spec["filter"], raw, args) if spec.get("filter") else json.loads(raw)
    except Exception as e:
        emit({"ok": False, "error": f"{cap} output not parseable: {e}",
              "raw_head": raw[:300], "stderr_head": proc.stderr[:300]})
        return 1

    ms = int((time.time() - t0) * 1000)
    emit({"ok": True, "data": data, "meta": {"capability": cap, "args": args, "ms": ms}})
    return 0


if __name__ == "__main__":
    sys.exit(main())
