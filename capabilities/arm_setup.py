#!/usr/bin/env python3
"""arm_setup.py — one-command parameterized watch-builder (operationalize goal, item 2).

Replaces the bespoke per-token watch scripts (bill_short_watch / h_blowoff_short_v2 / esports_scalp
/ lab_short_manage / portal_fade …) with one tool that encodes the playbook defaults:
  - held-break trigger = N consecutive 5m CLOSES beyond the level (not a wick) [feedback: chronic
    re-squeeze coins chop the first-break]
  - self-arming = track the running peak/trough, arm the breakdown only AFTER a rollover (>=ARM_PCT
    off the extreme, no new extreme); a fresh extreme disarms (can't chase the pump)
  - funding-guard = for a SHORT, if funding re-deepens past the threshold (squeeze reloading) →
    DISARM/warn [PORTAL/LAB lesson]; for a cooled fade, the reload past ~-0.30%/4h is the exit
  - TP ladder banked in order; deepest TP exits; TPs should sit just INSIDE the magnet (caller's job)
  - invalidation = price reclaims/breaks back through the level → CUT
  - time-stop by setup type: blowoff_top=24h · stage5=120h · squeeze_fuel=72h · scalp=8h
Emits the same marked lines the Monitors grep for (🔴 ENTRY / 🎯 TP / 🚨 INVALIDATED / ⚠ FUNDING-GUARD
/ ↟ NEW-EXTREME / ✅ ARMED / ⏰ TIME-STOP).

Usage (run it; nohup to background — it prints the launch + Monitor lines):
  arm_setup.py <TICKER> --dir short|long \
     --trigger now | break:<px> | held-break:<px> | self-arming \
     --stop <px> --tp <px,px,...> [--inval <px>] [--funding-guard <rate>] \
     [--setup blowoff_top|stage5|squeeze_fuel|scalp] [--poll <sec>] [--note "scout 2-3x"]
  arm_setup.py BILL --dir short --trigger held-break:0.0804 --stop 0.0989 --tp 0.0672,0.0639 --setup squeeze_fuel
  arm_setup.py --plan ...   # validate + print the plan + the launch/Monitor commands, don't run
"""
import sys, json, time, argparse, urllib.request, os
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from regime_flip import to_4h, bybit_interval_min, DEEP_NEG   # SPEC 8/11 — normalized %/4h + the §5 line

TIME_STOP = {"blowoff_top": 24, "stage5": 120, "squeeze_fuel": 72, "scalp": 8}

VETO_4H = DEEP_NEG   # -0.30 %/4h — §5 hard pre-ENTRY short veto (mirror of the engine veto)


def as_4h_threshold(v):
    """Interpret a CLI funding threshold as %/4h. Auto-scale an obviously-raw decimal
    (e.g. -0.003) to %/4h so `--funding-guard -0.30` means -0.30%/4h, not raw (SPEC 8)."""
    if v is None:
        return None
    return v * 100 if abs(v) < 0.02 else v


def funding_veto(short, funding_4h, veto_4h=VETO_4H):
    """True = suppress a SHORT ENTRY: deep-neg funding ≤ the §5 line (carry + squeeze fuel).
    Longs are never funding-vetoed here."""
    return bool(short and funding_4h is not None and funding_4h <= veto_4h)


def decide_entry(trig, short, funding_4h, veto_4h=VETO_4H):
    """The ENTRY gate applied in EVERY trigger mode. Returns (fire, veto_msg).
    A bad alert is suppressed here regardless of how `trig` was reached (held-break,
    break, self-arming, now) — the §5 veto is a hard PRE-entry gate, not a post-arm disarm."""
    if not trig:
        return (False, None)
    if funding_veto(short, funding_4h, veto_4h):
        f = "n/a" if funding_4h is None else f"{funding_4h:+.3f}%/4h"
        return (False, f"⛔ FUNDING-VETO — SHORT ENTRY suppressed: funding {f} ≤ {veto_4h:+.2f}%/4h "
                       "(§5 deep-neg: you'd pay carry into crowded shorts = squeeze fuel). No 🔴 ENTRY.")
    return (True, None)

def fetch(url, t=8):
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "arm/1.0"}), timeout=t) as r:
            return json.loads(r.read().decode())
    except Exception:
        return None

def ticker(sym):
    d = fetch(f"https://api.bybit.com/v5/market/tickers?category=linear&symbol={sym}")
    try:
        x = d["result"]["list"][0]
        return float(x["lastPrice"]), float(x["fundingRate"])
    except (TypeError, KeyError, IndexError):
        return None, None

def closes(sym, n=3):
    d = fetch(f"https://api.bybit.com/v5/market/kline?category=linear&symbol={sym}&interval=5&limit={n}")
    try:
        return [float(k[4]) for k in d["result"]["list"]][:n]  # newest first
    except (TypeError, KeyError):
        return []

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("ticker")
    ap.add_argument("--dir", choices=["short", "long"], required=True)
    ap.add_argument("--trigger", required=True, help="now | break:<px> | held-break:<px> | self-arming")
    ap.add_argument("--stop", type=float, required=True)
    ap.add_argument("--tp", default="", help="comma list, in fire order (deepest last exits)")
    ap.add_argument("--inval", type=float, default=None, help="reclaim/break-back level = cut (default: stop)")
    ap.add_argument("--funding-guard", type=float, default=None, help="raw rate; short: re-deepen past = disarm")
    ap.add_argument("--setup", default="scalp", choices=list(TIME_STOP))
    ap.add_argument("--arm-pct", type=float, default=0.07, help="self-arming: rollover %% off extreme to arm")
    ap.add_argument("--poll", type=int, default=90)
    ap.add_argument("--note", default="")
    ap.add_argument("--plan", action="store_true", help="validate + print plan, don't run")
    a = ap.parse_args()

    sym = f"{a.ticker.upper()}USDT"
    short = a.dir == "short"
    tps = [float(x) for x in a.tp.split(",") if x.strip()]
    inval = a.inval if a.inval is not None else a.stop
    horizon = TIME_STOP[a.setup]
    px0, fr0 = ticker(sym)
    if px0 is None:
        print(f"no perp ticker for {sym}"); return

    tmode, tlevel = (a.trigger.split(":") + [None])[:2]
    tlevel = float(tlevel) if tlevel else None

    interval_min = bybit_interval_min(sym)          # SPEC 8: normalize funding to %/4h
    guard_4h = as_4h_threshold(a.funding_guard)     # disarm threshold in %/4h
    f4_0 = to_4h(fr0 * 100, interval_min) if fr0 is not None else None

    print(f"🟡 arm_setup {a.ticker.upper()} {a.dir.upper()} · trigger={a.trigger} · stop ${a.stop} · "
          f"TP {tps} · inval ${inval} · {a.setup}({horizon}h)" + (f" · {a.note}" if a.note else ""))
    f4s = f"{f4_0:+.3f}%/4h" if f4_0 is not None else "n/a"
    print(f"   live: ${px0}  funding {f4s} (raw {fr0*100:.4f}%/{int(interval_min/60)}h)")
    if short:
        print(f"   §5 pre-ENTRY veto: SHORT blocked while funding ≤ {VETO_4H:+.2f}%/4h")
    if a.funding_guard is not None:
        print(f"   funding-guard: {'short — re-deepen past' if short else 'reload past'} {guard_4h:+.3f}%/4h = disarm/cut")
    if a.plan:
        log = f"/tmp/arm_{a.ticker.lower()}_{a.dir}.log"
        print(f"\n   launch:  nohup python3 scripts/arm_setup.py {' '.join(sys.argv[1:]).replace(' --plan','')} > {log} 2>&1 &")
        print(f"   monitor: tail -n 0 -f {log} | grep --line-buffered -E '🔴 ENTRY|🎯|INVALIDATED|FUNDING-GUARD|TIME-STOP'")
        return

    # crossed helpers (direction-aware)
    def beyond(px, lvl):    # past the trigger level in the trade's favor (short: below; long: above)
        return px < lvl if short else px > lvl
    def against(px, lvl):   # reclaim back through (inval): short: above; long: below
        return px >= lvl if short else px <= lvl

    start = time.time(); armed = (tmode == "now"); fired = False
    extreme = px0  # for self-arming: peak(short) / trough(long)
    tp_done = set(); below = 0; veto_active = False

    while True:
        if time.time() - start >= horizon * 3600:
            print(f"⏰ TIME-STOP {time.strftime('%H:%M:%S')} — {a.ticker.upper()} {a.setup} {horizon}h reached. Manage manually."); return
        px, fr = ticker(sym)
        if px is None:
            time.sleep(a.poll); continue
        f4 = to_4h(fr * 100, interval_min) if fr is not None else None

        # invalidation (any time once in/armed)
        if (armed or fired) and against(px, inval):
            print(f"🚨 INVALIDATED {time.strftime('%H:%M:%S')} — {sym} ${px} reclaimed ${inval}. CUT."); return

        # funding-guard (short: re-deepen past threshold = squeeze reload) — normalized %/4h
        if guard_4h is not None and short and f4 is not None and f4 <= guard_4h:
            print(f"⚠ FUNDING-GUARD {time.strftime('%H:%M:%S')} — funding {f4:+.3f}%/4h past {guard_4h:+.3f}%/4h (squeeze reloading). Disarm/cut. (px ${px})")
            if not fired:
                armed = False
            time.sleep(a.poll); continue

        # TP ladder (deepest first; last exits)
        for i, tp in enumerate(sorted(tps, reverse=not short)):  # short: descending; long: ascending
            is_last = (tp == (min(tps) if short else max(tps)))
            if i not in tp_done and beyond(px, tp):
                bank = "BANK rest / exit" if is_last else ("BANK 33-50%, stop→entry" if i == 0 else "bank a third, trail")
                print(f"🎯 TP{i+1} {time.strftime('%H:%M:%S')} — {sym} ${px} hit ${tp}. {bank}.")
                tp_done.add(i)
                if is_last:
                    return

        if not fired:
            # self-arming: track extreme, arm on rollover, disarm on new extreme
            if tmode == "now":
                trig = True
            elif tmode == "self-arming":
                new_ext = px > extreme if short else px < extreme
                if new_ext:
                    if armed: print(f"↟ NEW-EXTREME {time.strftime('%H:%M:%S')} — ${px}; disarming (move not done).")
                    extreme = px; armed = False; time.sleep(a.poll); continue
                arm_lvl = extreme * (1 - a.arm_pct) if short else extreme * (1 + a.arm_pct)
                if not armed and (px <= arm_lvl if short else px >= arm_lvl):
                    armed = True
                    print(f"✅ ARMED {time.strftime('%H:%M:%S')} — rolled over {a.arm_pct*100:.0f}% off ${extreme} (px ${px}). Watching break of ${tlevel or a.stop}.")
                trig = armed and tlevel and beyond(px, tlevel)
            elif tmode == "held-break":
                cl = closes(sym, 2)
                held = len(cl) == 2 and all(beyond(c, tlevel) for c in cl)
                trig = held
            else:  # break: single touch
                trig = tlevel and beyond(px, tlevel)

            # §5 HARD pre-ENTRY funding veto — applied in EVERY mode (SPEC 8)
            fire, veto_msg = decide_entry(bool(trig), short, f4)
            if fire:
                fired = True
                fs = f"{f4:+.3f}%/4h" if f4 is not None else "n/a"
                print(f"🔴 ENTRY {time.strftime('%H:%M:%S')} — {a.dir.upper()} {sym} @ ${px} "
                      f"(trigger={tmode} {tlevel if tlevel else ''}, funding {fs}). Stop ${a.stop}. TP {tps}. {a.note}")
            elif veto_msg:
                if not veto_active:    # log the veto once per continuous deep-neg period (don't spam)
                    print(f"{veto_msg} (px ${px}, {time.strftime('%H:%M:%S')})")
                    veto_active = True
            if not (trig and funding_veto(short, f4)):
                veto_active = False    # funding cooled (or no trig) → re-arm the one-shot veto log
        time.sleep(a.poll)

if __name__ == "__main__":
    main()
