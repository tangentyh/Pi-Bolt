#!/usr/bin/env python3
"""W12: rows for JIT builds. build spec: name=/path/to/pi[@VAR=v,VAR2=v]. Interleaved rounds.
Per interactive session (with the jit-hot.ts extension unless --no-plugin): ready ms + instr at ready, /hot x4 loop ms,
one prompt (5 model turns, tool calls) CPU ms + instr, 3 s idle CPU, threads, exit instr/cycles, peak footprint, ctx switches.
Then one `pi -p` run: instr, cycles, user+sys, footprint."""
import argparse, json, os, re, shutil, statistics, subprocess, sys, time
from pathlib import Path
sys.path.insert(0, sys.argv.pop(1))  # bench dir
from harness import (MODEL_ARGS, PROMPT, Tty, _rusage_macos, cpu_ms, done, fake_model, peak_footprint_mb, pi_env, pi_home,
                     workdir)
import harness

EXT = Path(__file__).resolve().parent / "w12-jit-hot.ts"


def instr(pid):
    if not harness.MACOS:
        return None
    ri = _rusage_macos(pid)
    return ri[29] / 1e6 if ri else None


def threads(pid):
    if not harness.MACOS:
        return len(os.listdir(f"/proc/{pid}/task"))
    out = subprocess.run(["ps", "-M", "-p", str(pid)], capture_output=True, text=True).stdout
    return max(0, len(out.strip().splitlines()) - 1)


def session(argv, extra, plugin):
    r = {}
    with fake_model() as port, pi_home(port) as home, workdir() as cwd:
        if plugin:
            (home / "extensions").mkdir()
            shutil.copy(EXT, home / "extensions" / EXT.name)
        t0 = time.perf_counter()
        tty = Tty([*argv, "--no-session", *MODEL_ARGS], pi_env(home, extra), cwd)
        if not tty.wait_for("fake-model", 0, t0 + 60):
            raise RuntimeError("never ready")
        r["ready_ms"] = round((time.perf_counter() - t0) * 1000, 1)
        r["ready_instr_m"] = instr(tty.pid)
        tty.settle(0.3, time.perf_counter() + 5)
        if plugin:
            hot = []
            for k in range(4):
                start = len(tty.buf)
                tty.send(b"/hot\r")
                if not tty.wait_for(" updates ", start, time.perf_counter() + 60):
                    raise RuntimeError("/hot did not answer")
                tty.settle(0.15, time.perf_counter() + 3)
                m = re.findall(r"hot \d+ words \(([\d.]+) ms\) load ([\d.]+) ms", tty.screen_text(start))
                hot.append(float(m[-1][0]))
                r["load_ms"] = float(m[-1][1])
            r["hot_first_ms"], r["hot_steady_ms"] = hot[0], statistics.median(hot[1:])
        c0, i0 = cpu_ms(tty.pid), instr(tty.pid)
        start = len(tty.buf)
        tty.send(PROMPT.encode() + b"\r")
        if not tty.wait_for(done(1), start, time.perf_counter() + 120):
            raise RuntimeError("prompt did not finish")
        tty.settle(0.3, time.perf_counter() + 5)
        r["turn_cpu_ms"] = round(cpu_ms(tty.pid) - c0, 1)
        r["turn_instr_m"] = round(instr(tty.pid) - i0, 1) if i0 is not None else None
        c1 = cpu_ms(tty.pid)
        time.sleep(3)
        r["idle3s_cpu_ms"] = round(cpu_ms(tty.pid) - c1, 2)
        r["threads"] = threads(tty.pid)
        status, ru = tty.quit()
        r["exit_instr_m"] = getattr(tty, "exit_counters", {}).get("instr_m")
        r["exit_cycles_m"] = getattr(tty, "exit_counters", {}).get("cycles_m")
        r["peak_fp_mb"] = getattr(tty, "peak_footprint_mb", None)
        r["cpu_total_ms"] = round((ru.ru_utime + ru.ru_stime) * 1000, 1)
        r["vcsw"], r["ivcsw"] = ru.ru_nvcsw, ru.ru_nivcsw
    return r


def print_mode(argv, extra, plugin):
    with fake_model() as port, pi_home(port) as home, workdir() as cwd:
        if plugin:
            (home / "extensions").mkdir()
            shutil.copy(EXT, home / "extensions" / EXT.name)
        p = subprocess.Popen([*argv, "--no-session", *MODEL_ARGS, "-p", PROMPT], cwd=cwd, env=pi_env(home, extra),
                             stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        out = p.stdout.read()
        fp = peak_footprint_mb(p.pid)
        c = dict(harness.last_exit_counters) if harness.MACOS else {}
        _, status, ru = os.wait4(p.pid, 0)
        if b"Done: read all four files" not in out:
            raise RuntimeError("-p did not finish")
        return {"p_instr_m": c.get("instr_m"), "p_cycles_m": c.get("cycles_m"), "p_cpu_ms": round((ru.ru_utime + ru.ru_stime) * 1000, 1),
                "p_fp_mb": fp, "p_vcsw": ru.ru_nvcsw}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("builds", nargs="+")
    ap.add_argument("--runs", type=int, default=5)
    ap.add_argument("--no-plugin", action="store_true")
    ap.add_argument("--no-print", action="store_true")
    ap.add_argument("--out", required=True)
    a = ap.parse_args()
    specs = []
    for s in a.builds:
        name, rest = s.split("=", 1)
        cmd, _, envs = rest.partition("@")
        extra = dict(kv.split("=", 1) for kv in envs.split(",") if kv)
        specs.append((name, [cmd], extra))
    plugin = not a.no_plugin
    rows = {n: [] for n, _, _ in specs}
    for run in range(a.runs):
        order = specs if run % 2 == 0 else list(reversed(specs))
        for name, argv, extra in order:
            r = session(argv, extra, plugin)
            if not a.no_print:
                r.update(print_mode(argv, extra, plugin))
            r.update(build=name, run=run)
            rows[name].append(r)
            with open(a.out, "a") as f:
                f.write(json.dumps(r) + "\n")
            print(json.dumps(r), flush=True)
    keys = [k for k in rows[specs[0][0]][0] if k not in ("build", "run")]
    print("\nmedians")
    print("build".ljust(14) + "".join(k[:13].rjust(14) for k in keys))
    for name in rows:
        line = name.ljust(14)
        for k in keys:
            v = [r[k] for r in rows[name] if r.get(k) is not None]
            line += (f"{statistics.median(v):14.2f}" if v else " " * 14)
        print(line)


if __name__ == "__main__":
    main()
