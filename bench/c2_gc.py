#!/usr/bin/env python3
"""C1 (1.0.3 runtime check): GC log and per-thread activity of an idle Pi after one answer, and the first collection of `pi -p`.

  c1_idle.py --bench DIR --mode idle --seconds 300 --rounds 2 --build rel=... --build cand=...
  c1_idle.py --bench DIR --mode headless --runs 5 --build rel=... --build cand=...

idle: TUI, one prompt, BUN_JSC_logGC=1 to a file. From the answer on, every second: new collection lines (Eden/Full, with the
time after the answer), each thread's CPU time (Linux /proc schedstat; macOS proc_pidinfo PROC_PIDTHREADINFO). A thread is
"active" in a second when its CPU time grew. Reports collections, the seconds each thread was active, process instructions
(macOS) and CPU per second over the window.
headless: `pi -p` with BUN_JSC_logGC=1: the first collection's "bytes allowed" and how many collections ran."""

import argparse
import contextlib
import ctypes
import json
import os
import re
import statistics
import subprocess
import sys
import tempfile
import time

ap = argparse.ArgumentParser()
ap.add_argument("--bench", required=True)
ap.add_argument("--mode", choices=["idle", "headless", "interactive"], required=True)
ap.add_argument("--build", action="append", required=True)
ap.add_argument("--seconds", type=float, default=150)
ap.add_argument("--rounds", type=int, default=1)
ap.add_argument("--runs", type=int, default=5)
ap.add_argument("--json")
args = ap.parse_args()
sys.path.insert(0, args.bench)
from harness import DONE, MACOS, MODEL_ARGS, PROMPT, Tty, _rusage_macos, cpu_ms, done, fake_model, memory_mb, parse_builds, pi_env, pi_home, workdir  # noqa: E402

GC_LINE = re.compile(rb"=> (\w+Collection)")
ALLOWED = re.compile(rb"bytes allocated this cycle: (\d+) exceed bytes allowed: (\d+)")

if MACOS:
    libproc = ctypes.CDLL("/usr/lib/libproc.dylib")

    class ThreadInfo(ctypes.Structure):
        _fields_ = [("user", ctypes.c_uint64), ("system", ctypes.c_uint64), ("cpu_usage", ctypes.c_int32),
                    ("policy", ctypes.c_int32), ("run_state", ctypes.c_int32), ("flags", ctypes.c_int32),
                    ("sleep_time", ctypes.c_int32), ("curpri", ctypes.c_int32), ("priority", ctypes.c_int32),
                    ("maxpriority", ctypes.c_int32), ("name", ctypes.c_char * 64)]


def threads(pid):
    """{thread key: (name, cpu ns)}"""
    out = {}
    if MACOS:
        ids = (ctypes.c_uint64 * 512)()
        n = libproc.proc_pidinfo(pid, 6, 0, ids, ctypes.sizeof(ids))
        for i in range(max(n, 0) // 8):
            ti = ThreadInfo()
            if libproc.proc_pidinfo(pid, 5, ctypes.c_uint64(ids[i]), ctypes.byref(ti), ctypes.sizeof(ti)) > 0:
                name = ti.name.decode(errors="replace") or "main?"
                out[ids[i]] = (name, ti.user + ti.system)
        return out
    with contextlib.suppress(OSError):
        for tid in os.listdir(f"/proc/{pid}/task"):
            with contextlib.suppress(OSError):
                name = open(f"/proc/{pid}/task/{tid}/comm").read().strip()
                out[tid] = (name, int(open(f"/proc/{pid}/task/{tid}/schedstat").read().split()[0]))
    return out


def instr(pid):
    if not MACOS:
        return None
    ri = _rusage_macos(pid)
    return ri[29] if ri else None


def idle_run(build, seconds):
    with fake_model() as port, pi_home(port) as home, workdir() as cwd:
        log = tempfile.mktemp(prefix="c1-gclog-")
        tty = Tty([*build.argv, "--no-session", *MODEL_ARGS], pi_env(home, {"BUN_JSC_logGC": "1"}), cwd, stderr=log)
        deadline = time.perf_counter() + 120
        assert tty.wait_for("fake-model", 0, deadline), "the TUI did not start"
        start = len(tty.buf)
        tty.send(PROMPT.encode() + b"\r")
        assert tty.wait_for(done(1), start, deadline), "no answer"
        t0 = time.perf_counter()
        pos = os.path.getsize(log)
        pre = open(log, "rb").read()
        gcs_before = [m.group(1).decode() for m in GC_LINE.finditer(pre)]
        gcs = []
        active = {}
        prev = threads(tty.pid)
        names = {k: v[0] for k, v in prev.items()}
        cpu0, i0 = cpu_ms(tty.pid), instr(tty.pid)
        cpu_at2 = i_at2 = None
        nxt = t0 + 1
        while True:
            now = time.perf_counter()
            if now >= t0 + seconds:
                break
            tty.pump(max(0.0, min(0.2, nxt - now)))
            if time.perf_counter() < nxt:
                continue
            sec = round(nxt - t0)
            nxt += 1
            with open(log, "rb") as f:
                f.seek(pos)
                data = f.read()
            pos += len(data)
            for m in GC_LINE.finditer(data):
                gcs.append((sec, m.group(1).decode()))
            cur = threads(tty.pid)
            for k, (name, ns) in cur.items():
                names[k] = name
                if ns > prev.get(k, (name, 0))[1]:
                    active.setdefault(k, []).append(sec)
            for k in prev.keys() - cur.keys():
                active.setdefault(k, []).append(f"exit@{sec}")
            prev = cur
            if sec == 2:
                cpu_at2, i_at2 = cpu_ms(tty.pid), instr(tty.pid)
        cpu1, i1, t1 = cpu_ms(tty.pid), instr(tty.pid), time.perf_counter()
        mem = memory_mb(tty.pid)
        tty.quit()
        os.unlink(log)
    win = t1 - (t0 + 2)
    r = {"build": build.name, "seconds": seconds, "gcs_during_answer": gcs_before, "gcs": gcs,
         "full_collections": sum(1 for _, k in gcs if k == "FullCollection"),
         "cpu_ms_per_s_2s_on": round((cpu1 - cpu_at2) / win, 4) if cpu_at2 is not None else None,
         "instr_k_per_s_2s_on": round((i1 - i_at2) / win / 1e3, 1) if i_at2 is not None and i1 else None,
         "own_mb_end": mem.get("own"),
         "threads": {f"{names[k]}#{str(k)[-4:]}": v for k, v in active.items()}}
    return r


def interactive_run(build, prompts=5):
    with fake_model() as port, pi_home(port) as home, workdir() as cwd:
        log = tempfile.mktemp(prefix="c2-gclog-")
        tty = Tty([*build.argv, "--no-session", *MODEL_ARGS], pi_env(home, {"BUN_JSC_logGC": "1"}), cwd, stderr=log)
        deadline = time.perf_counter() + 120
        ok = tty.wait_for("fake-model", 0, deadline)
        for n in range(1, prompts + 1):
            start = len(tty.buf)
            tty.send(PROMPT.encode() + b"\r")
            ok = ok and tty.wait_for(done(n), start, deadline)
            tty.settle(0.03, deadline)
        tty.quit()
        data = open(log, "rb").read()
        os.unlink(log)
    first = ALLOWED.search(data)
    kinds = [m.group(1).decode() for m in GC_LINE.finditer(data)]
    return {"build": build.name, "ok": bool(ok), "first_allocated": int(first.group(1)) if first else None,
            "first_allowed": int(first.group(2)) if first else None, "collections": kinds,
            "allowed_all": [int(m.group(2)) for m in ALLOWED.finditer(data)]}


def headless_run(build):
    with fake_model() as port, pi_home(port) as home, workdir() as cwd:
        p = subprocess.run([*build.argv, "-p", "--no-session", *MODEL_ARGS, PROMPT], env=pi_env(home, {"BUN_JSC_logGC": "1"}),
                           cwd=cwd, capture_output=True, stdin=subprocess.DEVNULL, timeout=120)
    ok = DONE.encode() in p.stdout
    first = ALLOWED.search(p.stderr)
    kinds = [m.group(1).decode() for m in GC_LINE.finditer(p.stderr)]
    return {"build": build.name, "ok": ok, "first_allocated": int(first.group(1)) if first else None,
            "first_allowed": int(first.group(2)) if first else None, "collections": kinds}


builds = parse_builds(args.build)
results = []
if args.mode == "idle":
    for rnd in range(args.rounds):
        for b in builds:
            r = idle_run(b, args.seconds)
            r["round"] = rnd + 1
            results.append(r)
            print(json.dumps(r), flush=True)
else:
    for rnd in range(args.runs):
        for b in builds:
            r = headless_run(b) if args.mode == "headless" else interactive_run(b)
            results.append(r)
            print(json.dumps(r), flush=True)
    print("\nbuild        first collection: allowed / allocated (median)   collections per run")
    for b in builds:
        rs = [r for r in results if r["build"] == b.name]
        al = [r["first_allowed"] for r in rs if r["first_allowed"]]
        ac = [r["first_allocated"] for r in rs if r["first_allocated"]]
        print(f"{b.name:12s} {statistics.median(al) if al else '-':>12} / {statistics.median(ac) if ac else '-':>12}   "
              f"{sorted(set(len(r['collections']) for r in rs))}  ok={all(r['ok'] for r in rs)}")
if args.json:
    with open(args.json, "a") as f:
        for r in results:
            f.write(json.dumps(r) + "\n")
